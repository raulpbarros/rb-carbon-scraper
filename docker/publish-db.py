"""Copy the container's scraped database back into the Windows checkout.

Why this is not `cp`. The database runs in WAL mode (`db.py`:
`PRAGMA journal_mode=WAL`), so the committed state is spread across
`verra.db`, `verra.db-wal` and `verra.db-shm`. Copying the first file alone
produces something that opens without complaint and is missing every
transaction still in the WAL — a stale database that reports itself as fine,
which is the shape of failure this codebase keeps meeting.

`VACUUM INTO` writes a single consistent, checkpointed, compacted file from a
read snapshot, so it is also safe to run while a sync is in progress.

This is not `verra slim-db`. That produces the ~21 MB delivery artifact the
installer carries, with the bulk ledgers dropped. This produces the full
working database, ledgers included, so the checkout can carry on scraping.

    docker compose run --rm publish                    -> data/verra.db
    docker compose run --rm publish /publish/verra.db --force

The target is never replaced without `--force`: it is the database the
Windows app and `build.ps1` read, and a two-hour scrape is not recoverable
from an accidental overwrite.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from carbon_scraper import settings


def _megabytes(path: Path) -> str:
    return f"{path.stat().st_size / 1_048_576:.1f} MB"


def publish(target: Path, *, force: bool) -> int:
    source = settings.DB_PATH
    if not source.exists():
        print(f"No database at {source}. Run a sync first.", file=sys.stderr)
        return 1

    if target.exists():
        if not force:
            print(
                f"{target} already exists ({_megabytes(target)}).\n"
                "Refusing to replace it — that is the database the Windows app "
                "and build.ps1 read.\n"
                "Re-run with --force if you mean to overwrite it.",
                file=sys.stderr,
            )
            return 1
        print(f"Replacing {target} ({_megabytes(target)}).")

    target.parent.mkdir(parents=True, exist_ok=True)

    # `VACUUM INTO` refuses an existing file, so the old target has to go —
    # but deleting it first means a full disk or a permission error leaves
    # the operator with neither database, which is the outcome the --force
    # guard exists to prevent. Vacuum to a sibling and swap it in.
    staging = target.with_name(target.name + ".new")
    staging.unlink(missing_ok=True)
    conn = sqlite3.connect(source)
    try:
        # A snapshot read, so a concurrent sync neither blocks this nor leaks
        # a half-written transaction into the copy.
        conn.execute("VACUUM INTO ?", (str(staging),))
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    os.replace(staging, target)

    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        rows = check.execute(
            "SELECT registry, COUNT(*) FROM projects GROUP BY registry ORDER BY registry"
        ).fetchall()
    finally:
        check.close()

    print(f"Wrote {target} ({_megabytes(target)}) from {source}.")
    for registry, count in rows:
        print(f"  {registry:<12} {count:>7,} projects")
    if not rows:
        print("  (no projects — the database is empty)")
    return 0


def main(argv: list[str]) -> int:
    # Unknown flags are rejected rather than dropped: `-force` silently
    # meaning "not --force" would refuse to publish and read as a bug in the
    # tool. There is only one flag, so this stays a two-line parser.
    flags = [a for a in argv if a.startswith("-")]
    unknown = [a for a in flags if a != "--force"]
    if unknown:
        print(f"Unknown option(s): {' '.join(unknown)}. Only --force is accepted.",
              file=sys.stderr)
        return 2
    args = [a for a in argv if not a.startswith("-")]
    target = Path(args[0]) if args else Path("/publish/verra.db")
    return publish(target, force="--force" in flags)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
