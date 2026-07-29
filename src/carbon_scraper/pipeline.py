"""The pipeline itself, with no opinion about how progress is displayed.

`cli.py` and the GUI both drive this module. Neither drives the other: the CLI
commands carry Typer `OptionInfo` defaults, so calling them from a GUI works
only by accident and breaks the moment a signature changes.

Two seams make that possible:

* **`ProgressSink`** — where "scraping projects, 4,120 of 5,244" goes. The CLI
  passes a rich console sink, the GUI a queue sink, tests the null sink.
* **`cancel`** — a `threading.Event` threaded down to `http_client`, which
  checks it between requests and sleeps on it during retry backoff. Stopping a
  run is safe at any point: every database write is an idempotent upsert, so a
  cancelled sync is repaired by re-running it, not by starting over.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from . import db, derive, excel, registries, settings
from .registries import base

log = logging.getLogger(__name__)


# --- progress -------------------------------------------------------------


class ProgressSink(Protocol):
    """Where a long-running command reports what it is doing."""

    def message(self, text: str) -> None:
        """A completed step, worth a line of output."""

    def stage(self, registry: str, resource: str, done: int, total: int | None) -> None:
        """Progress within a step. Called often; see `_Throttle`."""


class NullSink:
    """Discards progress. The default, so nothing here requires a terminal."""

    def message(self, text: str) -> None:
        log.info(text)

    def stage(self, registry: str, resource: str, done: int, total: int | None) -> None:
        return None


class _Throttle:
    """Rate-limits `stage` calls down to something a display can keep up with.

    Adapters call their `progress` callback once **per record** — roughly
    183,000 times during a full Gold Standard credit sync. Throttling here
    rather than in each sink means no sink can forget to.
    """

    def __init__(self, sink: ProgressSink, registry: str, resource: str, total: int | None,
                 min_interval: float = 0.15) -> None:
        self._sink = sink
        self._registry = registry
        self._resource = resource
        self._total = total
        self._min_interval = min_interval
        self._last = 0.0

    def __call__(self, done: int) -> None:
        now = time.monotonic()
        if now - self._last < self._min_interval:
            return
        self._last = now
        self._sink.stage(self._registry, self._resource, done, self._total)

    def final(self, done: int) -> None:
        self._sink.stage(self._registry, self._resource, done, self._total)


# --- registry selection ---------------------------------------------------


#: What the CLI accepts as a registry argument, and what the GUI passes: a name,
#: the word `all`, or the sequence of ticked checkboxes.
Registries = "str | Sequence[str]"


def selected(registry: str | Sequence[str]) -> list[str]:
    """Expand `all` / an alias / a sequence into stored registry identifiers.

    Order follows `registries.ALL` rather than the caller's, so two front ends
    asking for the same set scrape it in the same order. Duplicates collapse.
    """
    if isinstance(registry, str):
        if registry.strip().lower() in ("all", "*"):
            return list(registries.ALL)
        return [registries.resolve(registry)]

    names = {registries.resolve(name) for name in registry}
    return [name for name in registries.ALL if name in names]


def only(registry: str | Sequence[str]) -> str | list[str] | None:
    """The registry filter for the read-only SQL paths.

    `None` means every registry, and is returned **only** for `all` — never as
    a fallback. An empty selection stays an empty list, which `db` reads as no
    rows: a front end with nothing ticked must not quietly export everything.
    """
    if isinstance(registry, str):
        if registry.strip().lower() in ("all", "*"):
            return None
        return registries.resolve(registry)
    return selected(registry)


def label(registry: str) -> str:
    return settings.REGISTRY_LABELS.get(registry, registry)


def _run_label(registry: str | Sequence[str]) -> str:
    """What goes in the `runs.registry` column for a whole-selection command.

    `sync` logs one run per registry and passes a concrete name; `derive` runs
    once over a selection, so it stores the selection itself. A list would not
    bind to SQLite at all.
    """
    if isinstance(registry, str):
        return registry
    names = selected(registry)
    return ",".join(names) if names else "none"


# --- sync -----------------------------------------------------------------


def sync(
    registry: str | Sequence[str] = "all",
    *,
    limit: int | None = None,
    projects_only: bool = False,
    refresh: bool = False,
    sink: ProgressSink | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, dict[str, int]]:
    """Scrape one or every registry. Returns per-registry record counts."""
    sink = sink or NullSink()
    results: dict[str, dict[str, int]] = {}
    # `refresh` lasts exactly as long as this call. http_client reads the
    # variable per request, so setting it here is enough — but the CLI exits
    # afterwards and the GUI does not, and a leaked value would silently
    # disable the cache for every later run in the same process.
    with _no_cache(refresh):
        for name in selected(registry):
            results[name] = sync_one(
                name, limit=limit, projects_only=projects_only, sink=sink, cancel=cancel
            )
    return results


@contextmanager
def _no_cache(active: bool) -> Iterator[None]:
    if not active:
        yield
        return
    previous = os.environ.get("VERRA_NO_CACHE")
    os.environ["VERRA_NO_CACHE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("VERRA_NO_CACHE", None)
        else:
            os.environ["VERRA_NO_CACHE"] = previous


def sync_one(
    registry: str,
    *,
    limit: int | None = None,
    projects_only: bool = False,
    sink: ProgressSink | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, int]:
    sink = sink or NullSink()
    name = label(registry)
    counts: dict[str, int] = {}

    with db.session() as conn:
        run_id = db.start_run(conn, "sync", registry)
        try:
            with registries.adapter(registry, cancel=cancel) as client:
                total = client.project_total()
                sink.message(f"{name} reports {total:,} projects.")

                progress = _Throttle(sink, registry, "projects", limit or total)
                counts["projects"] = db.upsert_projects(
                    conn,
                    registry,
                    client.iter_projects(max_records=limit, progress=progress),
                )
                progress.final(counts["projects"])
                conn.commit()
                sink.message(f"{name} projects: {counts['projects']:,}")

                if not projects_only:
                    for resource in client.ledgers:
                        expected = client.count(resource)
                        progress = _Throttle(sink, registry, resource, limit or expected)
                        counts[resource] = db.upsert_credit_events(
                            conn,
                            registry,
                            resource,
                            client.iter_credits(
                                resource, max_records=limit, progress=progress
                            ),
                        )
                        progress.final(counts[resource])

                        # A registry that publishes its own per-project totals
                        # gets them stored too: they outrank summed rows, and
                        # they are the number the business reconciles against.
                        stated = base.credit_totals_of(client, resource)
                        if stated is not None:
                            written = db.upsert_credit_totals(
                                conn, registry, resource, stated
                            )
                            if written:
                                sink.message(
                                    f"{name} {resource}: {written:,} project totals "
                                    f"stated by the registry."
                                )

                        conn.commit()
                        sink.message(
                            f"{name} {resource}: {counts[resource]:,} "
                            f"(registry reports {expected:,})"
                        )
            db.finish_run(conn, run_id, ok=True, counts=counts)
        except Exception as exc:  # noqa: BLE001 - record the failure, then surface it
            db.finish_run(conn, run_id, ok=False, counts=counts, error=str(exc))
            raise
    return counts


# --- exact Verra totals ---------------------------------------------------


def totals(
    *,
    resource: str = "retirements",
    sink: ProgressSink | None = None,
    cancel: threading.Event | None = None,
) -> tuple[int, float, float]:
    """Exact per-project Verra totals. Returns (written, local_sum, registry_sum).

    Verra only. `retirements` (305k rows) cannot be paged completely, because
    the API silently ignores unsupported filters and returns the whole index,
    so partitioned paging both duplicates and drops records. One request per
    project is slow but exact, and immune to that trap.
    """
    sink = sink or NullSink()
    from .registries.verra.api import VerraAPI

    with db.session() as conn:
        project_ids = db.projects_with_credits(conn, settings.VERRA)
        sink.message(
            f"Fetching exact {resource} totals for {len(project_ids):,} projects "
            f"with credits (~{len(project_ids) * 2 // 60} min)."
        )
        run_id = db.start_run(conn, f"totals:{resource}", settings.VERRA)
        try:
            with VerraAPI(cancel=cancel) as client:
                progress = _Throttle(sink, settings.VERRA, resource, len(project_ids))
                written = db.upsert_credit_totals(
                    conn,
                    settings.VERRA,
                    resource,
                    client.sum_by_project(resource, project_ids, progress=progress),
                )
                progress.final(written)
                conn.commit()
                grand = client.sum_field(resource, "holdingQuantity")
            db.finish_run(conn, run_id, ok=True, counts={resource: written})
        except Exception as exc:  # noqa: BLE001
            db.finish_run(conn, run_id, ok=False, error=str(exc))
            raise

        local = conn.execute(
            "SELECT SUM(quantity) FROM credit_totals WHERE registry=? AND resource=?",
            (settings.VERRA, resource),
        ).fetchone()[0] or 0.0

    return written, float(local), float(grand)


# --- derive / export ------------------------------------------------------


def derive_all(
    registry: str | Sequence[str] = "all", *, sink: ProgressSink | None = None
) -> int:
    """Apply config/derivation/*.yaml. No network."""
    sink = sink or NullSink()
    rulesets = derive.load_rulesets()
    sink.message(
        f"Loaded {len(rulesets)} ruleset(s): {[r.column for r in rulesets]}"
    )

    written = 0
    with db.session() as conn:
        run_id = db.start_run(conn, "derive", _run_label(registry))
        try:
            for name in selected(registry):
                db.clear_derived(conn, name)
                rows: list[tuple[str, int, str, object, str]] = []
                for project in db.all_projects(conn, name):
                    record = dict(project)
                    for column, value, rule_name in derive.derive_for_project(
                        record, rulesets
                    ):
                        rows.append((name, record["project_id"], column, value, rule_name))
                written += db.replace_derived(conn, rows)
            db.finish_run(conn, run_id, ok=True, counts={"derived": written})
        except Exception as exc:  # noqa: BLE001
            db.finish_run(conn, run_id, ok=False, error=str(exc))
            raise
    return written


def export(
    registry: str | Sequence[str] = "all",
    *,
    out_dir: Any = None,
    sink: ProgressSink | None = None,
) -> tuple[Path, int, Path | None]:
    """Write the NEXT version of the spreadsheet. Returns (path, rows, previous).

    `out_dir` moves the folder, never the versioning: the previous delivery is
    left byte-for-byte as it was sent. See excel.next_version_path().
    """
    sink = sink or NullSink()
    with db.session() as conn:
        path, count, previous = excel.write_xlsx(
            conn, registry=only(registry), out_dir=out_dir
        )
    if previous is not None:
        sink.message(f"Previous delivery kept at {previous.name}")
    sink.message(f"Wrote {count:,} rows to {path}")
    return path, count, previous


def run_all(
    registry: str | Sequence[str] = "all",
    *,
    limit: int | None = None,
    skip_totals: bool = False,
    out_dir: Any = None,
    sink: ProgressSink | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Path, int, Path | None]:
    """sync + totals + derive + export. The whole pipeline."""
    sink = sink or NullSink()
    sync(registry, limit=limit, sink=sink, cancel=cancel)
    if not skip_totals and settings.VERRA in selected(registry):
        # Without this Verra's credit columns undercount; see totals().
        totals(sink=sink, cancel=cancel)
    derive_all(registry, sink=sink)
    return export(registry, out_dir=out_dir, sink=sink)
