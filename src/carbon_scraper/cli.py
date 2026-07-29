"""Command line entrypoint.

    verra discover -r verra             re-capture an S&P API contract (Playwright)
    verra standards -r planvivo         an S&P registry's real standardId
    verra sync --limit 25               scrape one registry's projects + credits
    verra sync --registry all           scrape every registry
    verra totals                        EXACT per-project Verra retirement totals
    verra derive                        apply YAML rules; no network
    verra export                        write the next spreadsheet version; no network
    verra update                        sync + totals + derive; writes NO spreadsheet
    verra run                           sync + totals + derive + export
    verra status                        what is in the database
    verra coverage                      per-column fill rate, to spot gaps
    verra slim-db                       the database the installer ships

`--registry` accepts `verra`, `gs`, `cercarbono`, `planvivo` or `all` (default
`all` where it makes sense). `sync`, `derive` and `export` are separate so that
fixing a classification rule never means re-scraping a registry.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db, excel, pipeline, settings

app = typer.Typer(add_completion=False, help="Scrape carbon registries into SQLite.")
console = Console()

REGISTRY_OPTION = typer.Option(
    "all", "--registry", "-r", help="verra | gs | cercarbono | planvivo | all"
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


class ConsoleSink:
    """Renders pipeline progress as a rich status line plus printed messages."""

    def __init__(self) -> None:
        self._status = None
        self._key: tuple[str, str] | None = None

    def message(self, text: str) -> None:
        self._stop()
        console.print(text)

    def stage(self, registry: str, resource: str, done: int, total: int | None) -> None:
        key = (registry, resource)
        if key != self._key:
            self._stop()
            name = pipeline.label(registry)
            self._status = console.status(f"{name}: scraping {resource}…")
            self._status.start()
            self._key = key
        name = pipeline.label(registry)
        of = f"/{total:,}" if total else ""
        self._status.update(f"{name} {resource}: {done:,}{of}")

    def _stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
            self._key = None

    def __enter__(self) -> ConsoleSink:
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop()


@app.command()
def discover(
    registry: str = typer.Option(
        "verra", "--registry", "-r", help="Which S&P registry to capture: verra | planvivo"
    ),
    headless: bool = typer.Option(True, help="Run the browser headless."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Watch a real S&P registry page and write down the API contract it uses.

    S&P (Platts) registries only — Verra and Plan Vivo today. Gold Standard and
    Cercarbono are plain REST and were contracted by hand; there is nothing for
    a browser to observe there.

    This is the diagnostic, not the data path. Run it once up front and again
    whenever a scraper breaks.
    """
    _setup_logging(verbose)
    from .registries.platts import discovery

    result = discovery.discover(registry, headless=headless)
    markdown_path, _ = discovery.save_contract(result)
    console.print(f"[green]Captured {len(result.calls)} API calls.[/green]")
    for call in sorted(result.calls, key=lambda c: c.role):
        console.print(f"  {call.role:16} {call.method:5} {call.status} {call.url}")
    console.print(f"Contract written to {markdown_path}")


@app.command()
def standards(
    registry: str = typer.Option(
        "verra", "--registry", "-r", help="An S&P registry code or name."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """List an S&P registry's standards and their real `standardId`.

    The cheap half of `discover`, and the first call to make when adding a
    registry on this platform: it is unauthenticated, answers in one GET, and
    is the only published source of the `standardid` header. Guessing that
    value is not survivable — a wrong-but-plausible one returns HTTP 200 with
    `totalEntities: 0`, which looks exactly like an empty registry.

    `publicReportExport` in the output says which ledgers the registry exposes
    publicly, which is how a new adapter's ledger set is decided.
    """
    _setup_logging(verbose)
    from .http_client import RegistryClient
    from .registries.platts import api as platts
    from .registries.platts import discovery

    cls = discovery.target(registry)
    with RegistryClient() as client:
        found = platts.standards_by_registry(
            client, cls.registry_code, config_url=cls.config_url
        )

    table = Table(title=f"{cls.registry_code} standards")
    for column in ("standardId", "acronym", "name", "public reports"):
        table.add_column(column)
    for standard in found:
        exports = standard.get("publicReportExport") or {}
        table.add_row(
            str(standard.get("id")),
            str(standard.get("metaData") or ""),
            str(standard.get("name") or ""),
            ", ".join(sorted(k for k, v in exports.items() if v)) or "—",
        )
    console.print(table)


@app.command()
def sync(
    registry: str = REGISTRY_OPTION,
    limit: Optional[int] = typer.Option(
        None, help="Stop after N records per resource. Use for smoke tests."
    ),
    projects_only: bool = typer.Option(False, help="Skip the credit ledgers."),
    refresh: bool = typer.Option(False, help="Ignore the local response cache."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scrape a registry into SQLite. Safe to re-run; writes are upserts."""
    _setup_logging(verbose)
    with ConsoleSink() as sink:
        pipeline.sync(
            registry,
            limit=limit,
            projects_only=projects_only,
            refresh=refresh,
            sink=sink,
        )
    console.print("[bold green]Sync complete.[/bold green] Next: verra derive")


def derive_cmd(
    registry: str = REGISTRY_OPTION,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Apply config/derivation/*.yaml. No network."""
    _setup_logging(verbose)
    with ConsoleSink() as sink:
        written = pipeline.derive_all(registry, sink=sink)
    console.print(f"[green]Wrote {written} derived values.[/green] Next: verra export")


@app.command()
def export(
    registry: str = REGISTRY_OPTION,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Write the next version of the spreadsheet from SQLite. No network.

    The previous delivery is never overwritten — each run writes `_vN+1`.
    """
    _setup_logging(verbose)
    with ConsoleSink() as sink:
        path, count, _ = pipeline.export(registry, sink=sink)
    console.print(f"[bold green]Wrote {count} rows to {path}[/bold green]")


@app.command()
def update(
    registry: str = REGISTRY_OPTION,
    limit: Optional[int] = typer.Option(None, help="Stop after N records per resource."),
    skip_totals: bool = typer.Option(False, help="Skip the exact-totals pass."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """sync + totals + derive. A refresh, NOT a delivery — no spreadsheet.

    The same thing the window's `Update registry data` button does, and
    separate from `run` for the same reason: an export burns a version number,
    and versions are what the business was sent. Use `verra export` when you
    have decided to send one.
    """
    _setup_logging(verbose)
    with ConsoleSink() as sink:
        written = pipeline.update_all(
            registry, limit=limit, skip_totals=skip_totals, sink=sink
        )
    console.print(
        f"[bold green]Data updated.[/bold green] {written:,} derived values. "
        "Next: verra export"
    )


@app.command()
def run(
    registry: str = REGISTRY_OPTION,
    limit: Optional[int] = typer.Option(None, help="Stop after N records per resource."),
    skip_totals: bool = typer.Option(False, help="Skip the exact-totals pass."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """sync + totals + derive + export. The whole pipeline."""
    _setup_logging(verbose)
    with ConsoleSink() as sink:
        path, count, _ = pipeline.run_all(
            registry, limit=limit, skip_totals=skip_totals, sink=sink
        )
    console.print(f"[bold green]Wrote {count} rows to {path}[/bold green]")


@app.command()
def status() -> None:
    """Row counts and recent runs."""
    with db.session() as conn:
        table = Table(title="Database")
        table.add_column("Table")
        table.add_column("Rows", justify="right")
        for name, value in db.counts(conn).items():
            table.add_row(name, f"{value:,}")
        console.print(table)

        runs = Table(title="Recent runs")
        for column in ("id", "command", "registry", "started", "result", "counts", "error"):
            runs.add_column(column)
        for row in db.last_runs(conn):
            # ok is NULL while a run is still going, and stays NULL if the
            # process was killed. Do not report either as a failure.
            if row["finished_at"] is None:
                result = "[yellow]running / interrupted[/yellow]"
            elif row["ok"]:
                result = "[green]ok[/green]"
            else:
                result = "[red]failed[/red]"
            runs.add_row(
                str(row["run_id"]),
                row["command"],
                row["registry"] or "",
                str(row["started_at"])[:19],
                result,
                (row["counts"] or "")[:48],
                (row["error"] or "")[:48],
            )
        console.print(runs)


@app.command()
def totals(
    resource: str = typer.Option(
        "retirements", help="Ledger to total. Default is the one that needs it."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch EXACT per-project Verra totals straight from the API's SUM aggregate.

    Verra only. Needed because `retirements` (305k rows) cannot be paged
    completely: the API silently ignores unsupported filters and returns the
    whole index, so partitioned paging both duplicates and drops records. One
    request per project is slow but exact, and immune to that trap.

    Gold Standard needs no equivalent — its whole credit stream pages cleanly.
    Cercarbono publishes its own per-project totals, which `sync` already
    stores through the adapter's `iter_credit_totals`. Plan Vivo is on the same
    platform as Verra but three orders of magnitude smaller: every ledger pages
    in a single request, so there is nothing for this command to fix.
    """
    _setup_logging(verbose)
    with ConsoleSink() as sink:
        written, local, grand = pipeline.totals(resource=resource, sink=sink)

    console.print(f"[green]Wrote {written} project totals.[/green]")
    delta = local - grand
    verdict = "[green]MATCH[/green]" if abs(delta) < 1 else f"[red]off by {delta:,.0f}[/red]"
    console.print(
        f"Cross-check: sum of per-project totals {local:,.0f} vs "
        f"registry-wide total {grand:,.0f} — {verdict}"
    )


@app.command()
def cache(
    clear: bool = typer.Option(False, "--clear", help="Delete every cached response."),
) -> None:
    """Inspect or clear the on-disk response cache.

    A full sync caches roughly 1 GB of API responses so that repeated runs do
    not re-hit the registries. Clear it once you are happy with the data.
    """
    files = list(settings.CACHE_DIR.glob("*.json"))
    size_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
    console.print(f"{len(files):,} cached responses, {size_mb:,.0f} MB in {settings.CACHE_DIR}")
    if clear:
        for path in files:
            path.unlink(missing_ok=True)
        console.print("[green]Cache cleared.[/green]")


@app.command(name="slim-db")
def slim_db(
    out: Optional[str] = typer.Option(
        None, "--out", "-o", help="Where to write it. Default: dist/seed/carbon-seed.db"
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing file."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Write the database the installer ships. No network.

    Keeps `projects`, `credit_totals`, `project_derived` and `runs`; drops
    `credit_events` and `raw_snapshots`, which are most of the ~225 MB and
    which the spreadsheet never reads directly. The aggregates it *does* need
    from them are materialised first, so the slim database produces the same
    sheet — see db.export_slim.

    The result is a delivery artifact, not a working copy. `verra sync` against
    an installed one fills the ledgers back in registry by registry, and every
    seeded figure is outranked the moment real rows arrive.
    """
    _setup_logging(verbose)
    target = Path(out) if out else settings.USER_ROOT / "dist" / "seed" / "carbon-seed.db"
    with db.session() as conn:
        source_bytes = settings.DB_PATH.stat().st_size if settings.DB_PATH.exists() else 0
        counts = db.export_slim(conn, target, overwrite=force)

    table = Table(title=f"Slim database → {target}")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    for name in db.SLIM_TABLES:
        table.add_row(name, f"{counts[name]:,}")
    table.add_row("credit_totals seeded", f"{counts['seeded_totals']:,}")
    console.print(table)

    megabytes = counts["bytes"] / (1024 * 1024)
    if source_bytes:
        shrink = 100 - (counts["bytes"] * 100 // source_bytes)
        console.print(
            f"[green]{megabytes:,.1f} MB[/green] from "
            f"{source_bytes / (1024 * 1024):,.1f} MB — {shrink}% smaller."
        )
    else:
        console.print(f"[green]{megabytes:,.1f} MB[/green]")


@app.command()
def coverage(registry: str = REGISTRY_OPTION) -> None:
    """How full each output column actually is — where the gaps are."""
    with db.session() as conn:
        report = excel.coverage_report(conn, pipeline.only(registry))
    table = Table(title="Column coverage")
    table.add_column("Column")
    table.add_column("Filled", justify="right")
    table.add_column("%", justify="right")
    for column, filled, total in report:
        pct = (filled * 100 // total) if total else 0
        colour = "green" if pct >= 90 else "yellow" if pct >= 40 else "red"
        table.add_row(column, f"{filled:,}/{total:,}", f"[{colour}]{pct}%[/{colour}]")
    console.print(table)


# `derive` is a module name in this package, so the command function is named
# derive_cmd; expose it to the CLI under the name users expect.
app.command(name="derive")(derive_cmd)


if __name__ == "__main__":
    app()
