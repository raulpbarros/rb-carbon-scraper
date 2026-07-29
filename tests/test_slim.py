"""The database the installer ships. No network.

Two things have to hold, and the second is the one that bites:

1. A slim database produces exactly the sheet the full one does, even though
   `credit_events` is gone.
2. The figures it carries are a **stand-in**, and a real scrape must beat
   them. An install that re-scraped a registry and kept reporting the
   installer's numbers would look completely normal — right shape, plausible
   values, silently frozen at whenever the installer was cut.
"""

from __future__ import annotations

import sqlite3

import pytest

from carbon_scraper import db, excel, settings
from carbon_scraper.registries.verra import api as verra

VERRA = settings.VERRA
GS = settings.GOLD_STANDARD

PROJECT = {
    "projectId": 1728,
    "vcsProjectId": "1728",
    "projectName": "Bundled Wind Power Project",
    "standardName": "Verified Carbon Standard",
    "status": "Registered",
    "sectoralScope": "Energy industries (renewable/non-renewable sources)",
    "regionName": "Asia",
    "countryName": "India",
}


def build_full(path) -> sqlite3.Connection:
    """A miniature of the real database: projects, ledgers, exact totals."""
    conn = db.connect(path)
    db.upsert_projects(conn, VERRA, [(verra.normalize_project(PROJECT), PROJECT)])
    db.upsert_credit_events(
        conn,
        VERRA,
        "issuances",
        [verra.normalize_credit({"entityId": 9, "projectId": 1728, "holdingQuantity": 500})],
    )
    db.upsert_credit_events(
        conn,
        VERRA,
        "retirements",
        [
            verra.normalize_credit(
                {
                    "entityId": 1,
                    "projectId": 1728,
                    "holdingQuantity": 100,
                    "beneficialOwner": "Acme",
                }
            ),
            verra.normalize_credit(
                {"entityId": 2, "projectId": 1728, "holdingQuantity": 50}
            ),
        ],
    )
    db.upsert_credit_events(
        conn,
        VERRA,
        "holdings",
        [
            verra.normalize_credit(
                {
                    "entityId": 5,
                    "projectId": 1728,
                    "holdingQuantity": 350,
                    "additionalCertification": "CORSIA - Pilot Phase",
                }
            )
        ],
    )
    # The exact server-side total, which outranks the summed rows.
    db.upsert_credit_totals(conn, VERRA, "retirements", [(1728, 4321.0, 9)])
    db.replace_derived(conn, [(VERRA, 1728, "Bioma", "Não Florestal", "non_forest")])
    db.finish_run(conn, db.start_run(conn, "sync", VERRA), ok=True)
    conn.commit()
    return conn


@pytest.fixture()
def full(tmp_path):
    conn = build_full(tmp_path / "full.db")
    yield conn
    conn.close()


def slim_of(full_conn, tmp_path, name="slim.db"):
    counts = db.export_slim(full_conn, tmp_path / name)
    return db.connect(tmp_path / name), counts


# -- what it keeps and what it drops ---------------------------------------


def test_bulk_tables_are_dropped(full, tmp_path):
    slim, counts = slim_of(full, tmp_path)
    try:
        assert db._scalar(slim, "SELECT COUNT(*) FROM credit_events") == 0
        assert db._scalar(slim, "SELECT COUNT(*) FROM raw_snapshots") == 0
        assert counts["projects"] == 1
        assert counts["project_derived"] == 1
        assert counts["runs"] == 1
    finally:
        slim.close()


def test_the_spreadsheet_is_unchanged(full, tmp_path):
    """The whole point: same sheet, without the rows it was built from."""
    slim, _ = slim_of(full, tmp_path)
    try:
        assert excel.build_rows(full) == excel.build_rows(slim)
    finally:
        slim.close()


def test_exact_registry_totals_still_outrank_the_seed(full, tmp_path):
    """4,321 came from the API's SUM; the 150 in the rows undercounts."""
    slim, _ = slim_of(full, tmp_path)
    try:
        assert db.credit_totals(slim)[(VERRA, 1728)]["retirements"] == 4321.0
        assert db.credit_totals(slim)[(VERRA, 1728)]["issuances"] == 500.0
    finally:
        slim.close()


def test_certifications_survive_the_loss_of_unit_rows(full, tmp_path):
    """Verra states this only on unit records, and those are gone."""
    slim, _ = slim_of(full, tmp_path)
    try:
        _, rows = excel.build_rows(slim)
        assert rows[0]["Additional Certification"] == "CORSIA - Pilot Phase"
    finally:
        slim.close()


def test_the_beneficiary_split_survives_too(full, tmp_path):
    """`sold_equals_retired: false` must still be a config edit, not a re-scrape."""
    slim, _ = slim_of(full, tmp_path)
    try:
        assert db.retired_by_beneficiary(slim)[(VERRA, 1728)] == 100.0
    finally:
        slim.close()


def test_it_is_smaller(full, tmp_path):
    """The reason the phase exists: the bulk tables are the whole weight.

    Padded with raw snapshots on purpose. At the real scale that table plus
    `credit_events` is ~220 MB of a 225 MB file; a four-row fixture would make
    the comparison meaningless, since an empty SQLite file is not free either.
    """
    full.executemany(
        "INSERT INTO raw_snapshots (registry, resource, entity_id, payload, scraped_at) "
        "VALUES (?,?,?,?,?)",
        [(VERRA, "project", n, "x" * 2048, db.now()) for n in range(10_000, 12_000)],
    )
    full.commit()
    full.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = db._database_file(full).stat().st_size

    _, counts = slim_of(full, tmp_path)
    assert counts["bytes"] * 10 < before


def test_no_journal_is_left_beside_it(full, tmp_path):
    """The installer ships one file; a stray -wal reads as crash recovery."""
    db.export_slim(full, tmp_path / "shipped.db")
    assert not (tmp_path / "shipped.db-wal").exists()
    assert not (tmp_path / "shipped.db-shm").exists()


# -- the seed is a stand-in, not an answer ---------------------------------


def test_a_scraped_ledger_beats_the_seed(full, tmp_path):
    """The trap: an install that re-scrapes must stop reporting shipped numbers."""
    slim, _ = slim_of(full, tmp_path)
    try:
        # A later scrape finds the project issued more than the installer knew.
        db.upsert_credit_events(
            slim,
            VERRA,
            "issuances",
            [
                verra.normalize_credit(
                    {"entityId": 9, "projectId": 1728, "holdingQuantity": 900}
                )
            ],
        )
        assert db.credit_totals(slim)[(VERRA, 1728)]["issuances"] == 900.0
    finally:
        slim.close()


def test_a_live_beneficiary_sum_beats_the_seed(full, tmp_path):
    slim, _ = slim_of(full, tmp_path)
    try:
        db.upsert_credit_events(
            slim,
            VERRA,
            "retirements",
            [
                verra.normalize_credit(
                    {
                        "entityId": 1,
                        "projectId": 1728,
                        "holdingQuantity": 700,
                        "beneficialOwner": "Acme",
                    }
                )
            ],
        )
        assert db.retired_by_beneficiary(slim)[(VERRA, 1728)] == 700.0
    finally:
        slim.close()


def test_clear_seed_totals_leaves_registry_totals_alone(full, tmp_path):
    """A completed scrape retires the seed. It must not take the exact totals."""
    slim, _ = slim_of(full, tmp_path)
    try:
        dropped = db.clear_seed_totals(slim, VERRA)
        assert dropped > 0
        remaining = list(
            slim.execute("SELECT resource, source FROM credit_totals WHERE registry=?", (VERRA,))
        )
        assert [(r["resource"], r["source"]) for r in remaining] == [
            ("retirements", db.REGISTRY_SOURCE)
        ]
    finally:
        slim.close()


def test_clear_seed_totals_is_per_registry(full, tmp_path):
    slim, _ = slim_of(full, tmp_path)
    try:
        db.upsert_credit_totals(
            slim, GS, "issuances", [(77, 12.0, 1)], source=db.SEED_SOURCE
        )
        db.clear_seed_totals(slim, VERRA)
        assert db.credit_totals(slim)[(GS, 77)]["issuances"] == 12.0
    finally:
        slim.close()


def test_a_migrated_column_order_is_copied_by_name(tmp_path):
    """`ALTER TABLE ADD COLUMN` appends, so a migrated table is out of order.

    `runs.registry` is third in SCHEMA and last in any database that came
    through `migrate()`. A positional copy shifts every value one place along;
    here that would file the timestamps as the registry name. It happened, and
    it only surfaced because `started_at` is NOT NULL — with one more nullable
    column it would have produced a shipped database full of plausible
    nonsense.
    """
    path = tmp_path / "migrated.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.executescript(
        """
        DROP TABLE runs;
        CREATE TABLE runs (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            command     TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            ok          INTEGER,
            counts      TEXT,
            error       TEXT
        );
        ALTER TABLE runs ADD COLUMN registry TEXT;
        """
    )
    conn.execute(
        "INSERT INTO runs (command, started_at, finished_at, ok, registry) "
        "VALUES ('sync', '2026-07-28T10:00:00', '2026-07-28T12:00:00', 1, ?)",
        (VERRA,),
    )
    conn.commit()
    conn.close()

    source = db.connect(path)
    try:
        db.export_slim(source, tmp_path / "shipped.db")
    finally:
        source.close()

    shipped = db.connect(tmp_path / "shipped.db")
    try:
        row = shipped.execute("SELECT * FROM runs").fetchone()
        assert row["registry"] == VERRA
        assert row["started_at"] == "2026-07-28T10:00:00"
        # And the GUI's captions, which read `runs`, still find the sync.
        assert db.registry_summary(shipped)[VERRA]["last_sync"] == "2026-07-28T12:00:00"
    finally:
        shipped.close()


# -- guards ----------------------------------------------------------------


def test_it_refuses_to_overwrite_by_default(full, tmp_path):
    (tmp_path / "slim.db").write_bytes(b"a delivery")
    with pytest.raises(FileExistsError):
        db.export_slim(full, tmp_path / "slim.db")
    assert (tmp_path / "slim.db").read_bytes() == b"a delivery"


def test_it_refuses_to_slim_onto_itself(full, tmp_path):
    with pytest.raises(ValueError):
        db.export_slim(full, tmp_path / "full.db", overwrite=True)


def test_slimming_a_slim_database_is_a_no_op(full, tmp_path):
    """Idempotent, so a build script can be re-run without thinking about it."""
    slim, _ = slim_of(full, tmp_path)
    try:
        twice, _ = slim_of(slim, tmp_path, name="twice.db")
        try:
            assert excel.build_rows(slim) == excel.build_rows(twice)
        finally:
            twice.close()
    finally:
        slim.close()


# -- first-run seeding -----------------------------------------------------


def test_the_shipped_database_is_adopted_on_first_run(full, tmp_path, monkeypatch):
    seed = tmp_path / "seed" / "carbon-seed.db"
    db.export_slim(full, seed)

    target = tmp_path / "home" / "data" / "verra.db"
    monkeypatch.setattr(settings, "SEED_DB", seed)
    monkeypatch.setattr(settings, "DB_PATH", target)

    settings.seed_database()
    assert target.exists()

    conn = db.connect(target)
    try:
        assert db.counts(conn)[f"{VERRA} projects"] == 1
    finally:
        conn.close()


def test_the_shipped_database_never_replaces_the_users(tmp_path, monkeypatch):
    """An upgrade must not restore installer data over a scrape they waited for."""
    seed = tmp_path / "seed.db"
    seed.write_bytes(b"the installer's copy")
    target = tmp_path / "verra.db"
    target.write_bytes(b"two hours of scraping")

    monkeypatch.setattr(settings, "SEED_DB", seed)
    monkeypatch.setattr(settings, "DB_PATH", target)
    settings.seed_database()

    assert target.read_bytes() == b"two hours of scraping"


def test_seeding_is_a_no_op_without_a_shipped_database(tmp_path, monkeypatch):
    """A development checkout carries no seed and must not grow one."""
    target = tmp_path / "verra.db"
    monkeypatch.setattr(settings, "SEED_DB", tmp_path / "absent.db")
    monkeypatch.setattr(settings, "DB_PATH", target)
    settings.seed_database()
    assert not target.exists()
