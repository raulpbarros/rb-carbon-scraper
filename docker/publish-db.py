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
        target.unlink()

    target.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(source) as conn:
        # A snapshot read, so a concurrent sync neither blocks this nor leaks
        # a half-written transaction into the copy.
        conn.execute("VACUUM INTO ?", (str(target),))

    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as check:
        rows = check.execute(
            "SELECT registry, COUNT(*) FROM projects GROUP BY registry ORDER BY registry"
        ).fetchall()

    print(f"Wrote {target} ({_megabytes(target)}) from {source}.")
    for registry, count in rows:
        print(f"  {registry:<12} {count:>7,} projects")
    if not rows:
        print("  (no projects — the database is empty)")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    force = "--force" in argv
    target = Path(args[0]) if args else Path("/publish/verra.db")
    return publish(target, force=force)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
