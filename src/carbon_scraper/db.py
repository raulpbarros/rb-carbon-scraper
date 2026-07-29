"""SQLite storage — the source of truth.

Every write is an idempotent upsert keyed on the registry's own identifiers,
so re-running a partial sync repairs it instead of duplicating rows.

One database holds every registry. Registries number their projects
independently — Verra project 1890 and Gold Standard project 1890 are
different projects — so `registry` is part of the primary key of every table.

Records arrive here already normalised: each adapter in `registries/` maps its
own API field names onto the column names below, so this module knows nothing
about any particular registry's JSON. Raw API payloads are kept in
`raw_snapshots` so derivation rules can be changed and re-applied without
re-scraping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime, timezone
from typing import Any

from . import settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    registry              TEXT NOT NULL,   -- VERRA | GOLD_STANDARD
    project_id            INTEGER NOT NULL,-- the registry's own numeric id
    external_id           TEXT,            -- human-facing ref: VCS1234 / GS7495
    project_name          TEXT,
    standard_name         TEXT,
    status                TEXT,
    sectoral_scope        TEXT,        -- Tipo Macro de Projeto
    methodologies         TEXT,
    afolu_names           TEXT,
    project_size          TEXT,
    proponents            TEXT,
    region_name           TEXT,        -- Continent, where the registry states it
    country_name          TEXT,
    country_code          TEXT,        -- ISO-2, used to derive Continent
    state_province        TEXT,
    city                  TEXT,
    area                  REAL,
    credit_period         TEXT,
    credit_period_term    TEXT,
    credit_period_start   TEXT,
    credit_period_end     TEXT,
    project_start_date    TEXT,
    project_end_date      TEXT,
    avg_annual_vol_vcu    REAL,        -- Yearly Ex Ante
    exante_quantity       REAL,
    units_issued          REAL,
    additional_certification TEXT,
    detail_url            TEXT,        -- registry page for this project
    extra                 TEXT,        -- JSON: registry-specific leftovers
    scraped_at            TEXT NOT NULL,
    PRIMARY KEY (registry, project_id)
);

CREATE TABLE IF NOT EXISTS credit_events (
    registry     TEXT NOT NULL,
    entity_id    INTEGER NOT NULL,
    resource     TEXT NOT NULL,        -- Verra: issuances|holdings|retirements|
                                       -- cancellations. Gold Standard: credits
    project_id   INTEGER,
    quantity     REAL,
    vintage      TEXT,
    unit_type    TEXT,
    status       TEXT,                 -- Gold Standard: ISSUED | RETIRED
    event_date   TEXT,
    beneficiary  TEXT,                 -- retirements: beneficial owner, if public
    reason       TEXT,                 -- retirements: retirement reason
    additional_certification TEXT,
    serial_no    TEXT,
    scraped_at   TEXT NOT NULL,
    PRIMARY KEY (registry, resource, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_credit_events_project
    ON credit_events (registry, project_id, resource);

-- Exact per-project totals fetched straight from the API's SUM aggregate.
-- Authoritative: takes precedence over summing credit_events rows, because
-- some ledgers (Verra retirements) cannot be paged completely. See
-- registries/verra/api.py.
CREATE TABLE IF NOT EXISTS credit_totals (
    registry    TEXT NOT NULL,
    project_id  INTEGER NOT NULL,
    resource    TEXT NOT NULL,
    quantity    REAL,
    event_count INTEGER,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (registry, project_id, resource)
);

CREATE TABLE IF NOT EXISTS project_derived (
    registry    TEXT NOT NULL,
    project_id  INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    value       TEXT,
    rule_name   TEXT,                  -- which rule produced this; keep it traceable
    derived_at  TEXT NOT NULL,
    PRIMARY KEY (registry, project_id, column_name)
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    registry   TEXT NOT NULL,
    resource   TEXT NOT NULL,
    entity_id  INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    PRIMARY KEY (registry, resource, entity_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    registry    TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    counts      TEXT,
    error       TEXT
);
"""

# Columns an adapter may set on a project row. Anything else it wants to keep
# goes into `extra` as JSON, and the full payload always lands in raw_snapshots.
PROJECT_FIELDS = (
    "project_id",
    "external_id",
    "project_name",
    "standard_name",
    "status",
    "sectoral_scope",
    "methodologies",
    "afolu_names",
    "project_size",
    "proponents",
    "region_name",
    "country_name",
    "country_code",
    "state_province",
    "city",
    "area",
    "credit_period",
    "credit_period_term",
    "credit_period_start",
    "credit_period_end",
    "project_start_date",
    "project_end_date",
    "avg_annual_vol_vcu",
    "exante_quantity",
    "units_issued",
    "additional_certification",
    "detail_url",
    "extra",
)

CREDIT_EVENT_FIELDS = (
    "entity_id",
    "project_id",
    "quantity",
    "vintage",
    "unit_type",
    "status",
    "event_date",
    "beneficiary",
    "reason",
    "additional_certification",
    "serial_no",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Any = None) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(path or settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Any = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# -- migration -------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a single-registry database up to the multi-registry schema.

    SQLite cannot alter a primary key, so each affected table is rebuilt and
    its rows copied across with `registry='VERRA'` — everything scraped before
    this change came from Verra. Data is preserved; no re-scrape is needed.
    Runs on every connect and is a no-op once done.
    """
    if not _table_exists(conn, "projects"):
        return
    if "registry" in _columns(conn, "projects"):
        return

    log.info("Migrating database to the multi-registry schema (backfilling VERRA).")
    old_project_columns = _columns(conn, "projects")
    # `vcs_project_id` became the registry-neutral `external_id`.
    external = "vcs_project_id" if "vcs_project_id" in old_project_columns else "NULL"
    carried = [
        c for c in PROJECT_FIELDS if c in old_project_columns and c != "external_id"
    ]

    legacy = {
        "projects": carried + ["scraped_at"],
        "credit_events": ["resource", *CREDIT_EVENT_FIELDS, "scraped_at"],
        "credit_totals": ["project_id", "resource", "quantity", "event_count", "fetched_at"],
        "project_derived": ["project_id", "column_name", "value", "rule_name", "derived_at"],
        "raw_snapshots": ["resource", "entity_id", "payload", "scraped_at"],
    }
    available = {
        table: [c for c in cols if c in _columns(conn, table)]
        for table, cols in legacy.items()
    }

    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_credit_events_project;
        ALTER TABLE projects        RENAME TO projects_pre_registry;
        ALTER TABLE credit_events   RENAME TO credit_events_pre_registry;
        ALTER TABLE credit_totals   RENAME TO credit_totals_pre_registry;
        ALTER TABLE project_derived RENAME TO project_derived_pre_registry;
        ALTER TABLE raw_snapshots   RENAME TO raw_snapshots_pre_registry;
        """
    )
    conn.executescript(SCHEMA)

    cols = ", ".join(available["projects"])
    conn.execute(
        f"INSERT INTO projects (registry, external_id, {cols}) "
        f"SELECT 'VERRA', {external}, {cols} FROM projects_pre_registry"
    )
    for table in ("credit_events", "credit_totals", "project_derived", "raw_snapshots"):
        cols = ", ".join(available[table])
        conn.execute(
            f"INSERT INTO {table} (registry, {cols}) "
            f"SELECT 'VERRA', {cols} FROM {table}_pre_registry"
        )

    if "registry" not in _columns(conn, "runs"):
        conn.execute("ALTER TABLE runs ADD COLUMN registry TEXT")

    moved = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    conn.executescript(
        """
        DROP TABLE projects_pre_registry;
        DROP TABLE credit_events_pre_registry;
        DROP TABLE credit_totals_pre_registry;
        DROP TABLE project_derived_pre_registry;
        DROP TABLE raw_snapshots_pre_registry;
        """
    )
    conn.commit()
    log.info("Migration complete: %s projects carried over as VERRA.", moved)


# -- writes ----------------------------------------------------------------

def upsert_projects(
    conn: sqlite3.Connection,
    registry: str,
    records: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    *,
    keep_raw: bool = True,
) -> int:
    """Upsert normalised project rows.

    `records` yields (normalised_row, raw_payload) pairs — the adapter has
    already translated its registry's field names into `PROJECT_FIELDS`.
    """
    stamp = now()
    columns = ["registry", *PROJECT_FIELDS, "scraped_at"]
    placeholders = ", ".join("?" * len(columns))
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in columns if c not in ("registry", "project_id")
    )
    sql = (
        f"INSERT INTO projects ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(registry, project_id) DO UPDATE SET {updates}"
    )

    rows: list[tuple[Any, ...]] = []
    raws: list[tuple[Any, ...]] = []
    count = 0
    for row, raw in records:
        project_id = int_or_none(row.get("project_id"))
        if project_id is None:
            continue
        values = [_clean(row.get(field)) for field in PROJECT_FIELDS]
        rows.append((registry, *values, stamp))
        if keep_raw:
            raws.append((registry, "project", project_id, json.dumps(raw), stamp))
        count += 1
        if len(rows) >= 500:
            _flush(conn, sql, rows, raws)
            rows, raws = [], []
    _flush(conn, sql, rows, raws)
    return count


def upsert_credit_events(
    conn: sqlite3.Connection,
    registry: str,
    resource: str,
    records: Iterable[dict[str, Any]],
) -> int:
    """Upsert normalised credit rows. Keys are `CREDIT_EVENT_FIELDS`."""
    stamp = now()
    columns = ["registry", "resource", *CREDIT_EVENT_FIELDS, "scraped_at"]
    placeholders = ", ".join("?" * len(columns))
    updates = ", ".join(
        f"{c}=excluded.{c}"
        for c in columns
        if c not in ("registry", "resource", "entity_id")
    )
    sql = (
        f"INSERT INTO credit_events ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(registry, resource, entity_id) DO UPDATE SET {updates}"
    )

    rows: list[tuple[Any, ...]] = []
    count = 0
    for record in records:
        entity_id = int_or_none(record.get("entity_id"))
        if entity_id is None:
            continue
        values = [_clean(record.get(field)) for field in CREDIT_EVENT_FIELDS]
        rows.append((registry, resource, *values, stamp))
        count += 1
        if len(rows) >= 1000:
            conn.executemany(sql, rows)
            rows = []
    if rows:
        conn.executemany(sql, rows)
    return count


def replace_derived(
    conn: sqlite3.Connection, rows: Iterable[tuple[str, int, str, Any, str]]
) -> int:
    stamp = now()
    payload = [
        (reg, pid, col, _clean(val), rule, stamp) for reg, pid, col, val, rule in rows
    ]
    conn.executemany(
        """
        INSERT INTO project_derived
            (registry, project_id, column_name, value, rule_name, derived_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(registry, project_id, column_name) DO UPDATE SET
            value=excluded.value, rule_name=excluded.rule_name, derived_at=excluded.derived_at
        """,
        payload,
    )
    return len(payload)


def clear_derived(conn: sqlite3.Connection, registry: str | None = None) -> None:
    if registry:
        conn.execute("DELETE FROM project_derived WHERE registry=?", (registry,))
    else:
        conn.execute("DELETE FROM project_derived")


# -- run log ---------------------------------------------------------------

def start_run(conn: sqlite3.Connection, command: str, registry: str | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (command, registry, started_at) VALUES (?, ?, ?)",
        (command, registry, now()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    ok: bool,
    counts: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, ok=?, counts=?, error=? WHERE run_id=?",
        (now(), 1 if ok else 0, json.dumps(counts or {}), error, run_id),
    )
    conn.commit()


# -- reads -----------------------------------------------------------------

Key = tuple[str, int]


def upsert_credit_totals(
    conn: sqlite3.Connection,
    registry: str,
    resource: str,
    rows: Iterable[tuple[int, float, Any]],
) -> int:
    stamp = now()
    payload = [(registry, pid, resource, qty, count, stamp) for pid, qty, count in rows]
    conn.executemany(
        """
        INSERT INTO credit_totals
            (registry, project_id, resource, quantity, event_count, fetched_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(registry, project_id, resource) DO UPDATE SET
            quantity=excluded.quantity, event_count=excluded.event_count,
            fetched_at=excluded.fetched_at
        """,
        payload,
    )
    return len(payload)


def credit_totals(conn: sqlite3.Connection) -> dict[Key, dict[str, float]]:
    """Per-project credit buckets, keyed by (registry, project_id).

    Three sources, in increasing order of authority:

    1. Verra's ledgers, each of which *is* a bucket — sum the rows per ledger.
    2. Gold Standard's single credit stream, where a block's `status` is its
       *current* state: every block was issued, so `issuances` is the sum of
       all of them, and `retirements` / `cancellations` are the subsets whose
       status says so. Summing only `status='ISSUED'` would report zero issued
       credits for any project that has since retired them.
    3. Exact API-sourced totals in `credit_totals`, which override everything
       above — some Verra ledgers cannot be paged completely, so their rows
       undercount. See registries/verra/api.py.
    """
    totals: dict[Key, dict[str, float]] = {}

    for row in conn.execute(
        """
        SELECT registry, project_id, resource, SUM(COALESCE(quantity, 0)) AS total
        FROM credit_events
        WHERE project_id IS NOT NULL AND resource != 'credits'
        GROUP BY registry, project_id, resource
        """
    ):
        key = (row["registry"], row["project_id"])
        totals.setdefault(key, {})[row["resource"]] = float(row["total"] or 0.0)

    for row in conn.execute(
        """
        SELECT registry, project_id,
               SUM(COALESCE(quantity, 0)) AS issued,
               SUM(CASE WHEN status='RETIRED'   THEN COALESCE(quantity,0) ELSE 0 END) AS retired,
               SUM(CASE WHEN status='CANCELLED' THEN COALESCE(quantity,0) ELSE 0 END) AS cancelled
        FROM credit_events
        WHERE project_id IS NOT NULL AND resource = 'credits'
        GROUP BY registry, project_id
        """
    ):
        key = (row["registry"], row["project_id"])
        totals.setdefault(key, {}).update(
            {
                "issuances": float(row["issued"] or 0.0),
                "retirements": float(row["retired"] or 0.0),
                "cancellations": float(row["cancelled"] or 0.0),
            }
        )

    for row in conn.execute(
        "SELECT registry, project_id, resource, quantity FROM credit_totals "
        "WHERE quantity IS NOT NULL"
    ):
        key = (row["registry"], row["project_id"])
        totals.setdefault(key, {})[row["resource"]] = float(row["quantity"])
    return totals


def projects_with_credits(conn: sqlite3.Connection, registry: str) -> list[int]:
    """Projects that appear in any ledger — the only ones worth totalling."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT project_id FROM credit_events "
            "WHERE registry=? AND project_id IS NOT NULL ORDER BY project_id",
            (registry,),
        )
    ]


def retired_by_beneficiary(conn: sqlite3.Connection) -> dict[Key, float]:
    """Retirements naming a third-party beneficiary — the 'sold' reading."""
    return {
        (row["registry"], row["project_id"]): float(row["total"] or 0.0)
        for row in conn.execute(
            """
            SELECT registry, project_id, SUM(COALESCE(quantity, 0)) AS total
            FROM credit_events
            WHERE resource = 'retirements'
              AND project_id IS NOT NULL
              AND beneficiary IS NOT NULL AND TRIM(beneficiary) != ''
            GROUP BY registry, project_id
            """
        )
    }


def certifications_by_project(conn: sqlite3.Connection) -> dict[Key, str]:
    """Additional certifications, which only ever appear on unit records."""
    result: dict[Key, list[str]] = {}
    for row in conn.execute(
        """
        SELECT DISTINCT registry, project_id, additional_certification
        FROM credit_events
        WHERE project_id IS NOT NULL
          AND additional_certification IS NOT NULL
          AND TRIM(additional_certification) != ''
        """
    ):
        result.setdefault((row["registry"], row["project_id"]), []).append(
            row["additional_certification"]
        )
    return {key: "; ".join(sorted(set(values))) for key, values in result.items()}


RegistryFilter = "str | Sequence[str] | None"


def registry_clause(
    registry: str | Sequence[str] | None, column: str = "registry"
) -> tuple[str, list[str]]:
    """Build a WHERE fragment for a registry filter. Returns (sql, params).

    Three inputs, three different meanings, and the middle one is the trap:

    * `None` — every registry. The fragment is empty.
    * a name, or a sequence of names — those registries.
    * an **empty sequence** — *no* registries, so the fragment is a
      contradiction rather than nothing.

    An empty selection has to mean zero rows. Falling back to "everything"
    would turn a GUI with no boxes ticked into a full export of the database,
    which is the same class of silent superset as an ignored API filter: no
    error, plausible-looking output, wrong scope.
    """
    if registry is None:
        return "", []
    names = [registry] if isinstance(registry, str) else list(registry)
    if not names:
        return f" WHERE {column} IS NULL AND {column} IS NOT NULL", []
    placeholders = ",".join("?" for _ in names)
    return f" WHERE {column} IN ({placeholders})", names


def all_projects(
    conn: sqlite3.Connection, registry: str | Sequence[str] | None = None
) -> list[sqlite3.Row]:
    """Project rows for one registry, several, or every one.

    A sequence is what the GUI passes: its checkboxes select registries for
    both buttons, so the export has to honour the same selection the scrape
    does.
    """
    where, params = registry_clause(registry)
    return list(
        conn.execute(
            f"SELECT * FROM projects{where} ORDER BY registry, project_id", params
        )
    )


def derived_map(conn: sqlite3.Connection) -> dict[Key, dict[str, str]]:
    result: dict[Key, dict[str, str]] = {}
    for row in conn.execute(
        "SELECT registry, project_id, column_name, value FROM project_derived"
    ):
        result.setdefault((row["registry"], row["project_id"]), {})[
            row["column_name"]
        ] = row["value"]
    return result


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in conn.execute(
        "SELECT registry, COUNT(*) AS n FROM projects GROUP BY registry ORDER BY registry"
    ):
        out[f"{row['registry']} projects"] = int(row["n"])
    for row in conn.execute(
        "SELECT registry, resource, COUNT(*) AS n FROM credit_events "
        "GROUP BY registry, resource ORDER BY registry, resource"
    ):
        out[f"{row['registry']} {row['resource']}"] = int(row["n"])
    out["derived"] = _scalar(conn, "SELECT COUNT(*) FROM project_derived")
    return out


def last_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,))
    )


def registry_summary(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per-registry: how much is stored, and when it last arrived.

    What the GUI puts beside each checkbox, so the user can see that a
    registry has never been scraped before pressing a button that takes hours.
    Keyed by the stored registry identifier; a registry with no rows is still
    present, with `projects: 0` and `last_sync: None`, because "nothing here
    yet" is the fact worth showing.

    `last_sync_seconds` is how long that run took. It is the honest estimate
    for the next one — better than any table of guesses, because it was
    measured on this machine, against this cache, over this network.
    """
    summary: dict[str, dict[str, Any]] = {
        name: {
            "projects": 0,
            "credit_events": 0,
            "last_sync": None,
            "last_sync_ok": None,
            "last_sync_seconds": None,
        }
        for name in settings.REGISTRY_LABELS
    }

    def slot(name: str) -> dict[str, Any]:
        return summary.setdefault(
            name,
            {
                "projects": 0,
                "credit_events": 0,
                "last_sync": None,
                "last_sync_ok": None,
                "last_sync_seconds": None,
            },
        )

    for row in conn.execute(
        "SELECT registry, COUNT(*) AS n FROM projects GROUP BY registry"
    ):
        slot(row["registry"])["projects"] = int(row["n"])
    for row in conn.execute(
        "SELECT registry, COUNT(*) AS n FROM credit_events GROUP BY registry"
    ):
        slot(row["registry"])["credit_events"] = int(row["n"])

    # Most recent finished sync per registry. A run still in flight (or killed)
    # has finished_at NULL and is deliberately skipped: it is not evidence that
    # the data arrived.
    for row in conn.execute(
        "SELECT registry, started_at, finished_at, ok FROM runs "
        "WHERE command='sync' AND registry IS NOT NULL AND finished_at IS NOT NULL "
        "ORDER BY run_id DESC"
    ):
        entry = slot(row["registry"])
        if entry["last_sync"] is not None:
            continue
        entry["last_sync"] = row["finished_at"]
        entry["last_sync_ok"] = bool(row["ok"])
        entry["last_sync_seconds"] = _elapsed(row["started_at"], row["finished_at"])
    return summary


def _elapsed(started: Any, finished: Any) -> float | None:
    """Seconds between two stored timestamps, or None if either is unreadable."""
    try:
        begin = datetime.fromisoformat(str(started))
        end = datetime.fromisoformat(str(finished))
    except (TypeError, ValueError):
        return None
    return max((end - begin).total_seconds(), 0.0)


# -- helpers ---------------------------------------------------------------

def _flush(
    conn: sqlite3.Connection,
    sql: str,
    rows: list[tuple[Any, ...]],
    raws: list[tuple[Any, ...]],
) -> None:
    if rows:
        conn.executemany(sql, rows)
    if raws:
        conn.executemany(
            """
            INSERT INTO raw_snapshots (registry, resource, entity_id, payload, scraped_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(registry, resource, entity_id) DO UPDATE SET
                payload=excluded.payload, scraped_at=excluded.scraped_at
            """,
            raws,
        )


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v not in (None, "")) or None
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, str):
        return value.strip() or None
    return value


def first(record: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = record.get(field)
        if value:
            return value
    return None


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
