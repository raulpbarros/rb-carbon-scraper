"""The one-way schema migration. No network.

Verra data was scraped before the database learned about registries, and a
full re-scrape costs ~2.5 hours. The migration therefore has to carry every
row across, not just create the new columns.
"""

from __future__ import annotations

import sqlite3

import pytest

from carbon_scraper import db, settings

# The schema exactly as it stood before Gold Standard was added.
OLD_SCHEMA = """
CREATE TABLE projects (
    project_id            INTEGER PRIMARY KEY,
    vcs_project_id        TEXT,
    project_name          TEXT,
    standard_name         TEXT,
    status                TEXT,
    sectoral_scope        TEXT,
    methodologies         TEXT,
    region_name           TEXT,
    country_name          TEXT,
    avg_annual_vol_vcu    REAL,
    scraped_at            TEXT NOT NULL
);
CREATE TABLE credit_events (
    entity_id    INTEGER NOT NULL,
    resource     TEXT NOT NULL,
    project_id   INTEGER,
    quantity     REAL,
    beneficiary  TEXT,
    scraped_at   TEXT NOT NULL,
    PRIMARY KEY (resource, entity_id)
);
CREATE TABLE credit_totals (
    project_id  INTEGER NOT NULL,
    resource    TEXT NOT NULL,
    quantity    REAL,
    event_count INTEGER,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, resource)
);
CREATE TABLE project_derived (
    project_id  INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    value       TEXT,
    rule_name   TEXT,
    derived_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, column_name)
);
CREATE TABLE raw_snapshots (
    resource   TEXT NOT NULL,
    entity_id  INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    PRIMARY KEY (resource, entity_id)
);
CREATE TABLE runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    counts      TEXT,
    error       TEXT
);
"""


@pytest.fixture()
def legacy_db(tmp_path):
    path = tmp_path / "verra.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO projects (project_id, vcs_project_id, project_name, "
        "region_name, avg_annual_vol_vcu, scraped_at) VALUES (1728, '1728', "
        "'Bundled Wind Power Project', 'Asia', 1000, '2026-07-27T00:00:00')"
    )
    conn.execute(
        "INSERT INTO credit_events (entity_id, resource, project_id, quantity, "
        "beneficiary, scraped_at) VALUES (9, 'retirements', 1728, 150, 'Acme', "
        "'2026-07-27T00:00:00')"
    )
    conn.execute(
        "INSERT INTO credit_totals VALUES (1728, 'retirements', 150, 1, "
        "'2026-07-27T00:00:00')"
    )
    conn.execute(
        "INSERT INTO project_derived VALUES (1728, 'Bioma', 'Cerrado', "
        "'br-cerrado', '2026-07-27T00:00:00')"
    )
    conn.execute(
        "INSERT INTO raw_snapshots VALUES ('project', 1728, '{}', "
        "'2026-07-27T00:00:00')"
    )
    conn.commit()
    conn.close()
    return path


def test_existing_verra_data_survives(legacy_db):
    conn = db.connect(legacy_db)
    try:
        projects = db.all_projects(conn)
        assert len(projects) == 1
        row = dict(projects[0])
        assert row["registry"] == settings.VERRA
        assert row["project_name"] == "Bundled Wind Power Project"
        # vcs_project_id became the registry-neutral external_id.
        assert row["external_id"] == "1728"
        assert row["region_name"] == "Asia"

        totals = db.credit_totals(conn)[(settings.VERRA, 1728)]
        assert totals["retirements"] == 150
        assert db.derived_map(conn)[(settings.VERRA, 1728)]["Bioma"] == "Cerrado"
        assert db.retired_by_beneficiary(conn)[(settings.VERRA, 1728)] == 150
    finally:
        conn.close()


def test_migration_is_idempotent(legacy_db):
    """It runs on every connect; the second pass must be a no-op."""
    for _ in range(3):
        conn = db.connect(legacy_db)
        assert len(db.all_projects(conn)) == 1
        conn.close()

    conn = sqlite3.connect(legacy_db)
    leftovers = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%_pre_registry'"
        )
    ]
    conn.close()
    assert leftovers == []


def test_an_interrupted_migration_is_resumed_not_declared_done(legacy_db, monkeypatch):
    """`executescript` COMMITs, so the rename is durable before any row moves.

    Crash there and `projects` already has its `registry` column: a guard on
    that column calls the migration finished and leaves every row of the
    user's database orphaned in `*_pre_registry`, with nothing raised. The
    resume point has to be the renamed tables themselves.
    """
    def die_after_the_rename(conn):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(db, "_copy_pre_registry_rows", die_after_the_rename)
    with pytest.raises(KeyboardInterrupt):
        db.connect(legacy_db)

    conn = sqlite3.connect(legacy_db)
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM projects_pre_registry").fetchone()[0] == 1
    )
    conn.close()

    # The next connect finds the renamed tables and finishes the job.
    monkeypatch.undo()
    conn = db.connect(legacy_db)
    try:
        projects = db.all_projects(conn)
        assert len(projects) == 1
        assert dict(projects[0])["project_name"] == "Bundled Wind Power Project"
        assert db.credit_totals(conn)[(settings.VERRA, 1728)]["retirements"] == 150
    finally:
        conn.close()

    leftovers = sqlite3.connect(legacy_db).execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%_pre_registry'"
    ).fetchall()
    assert leftovers == []


def test_a_failed_copy_leaves_the_original_tables_for_the_retry(legacy_db, monkeypatch):
    """A half-copy must roll back, not be committed as the new truth."""
    real_columns = db._columns

    def explode(conn, table):
        if table == "runs":
            raise sqlite3.OperationalError("disk I/O error")
        return real_columns(conn, table)

    monkeypatch.setattr(db, "_columns", explode)
    with pytest.raises(sqlite3.OperationalError):
        db.connect(legacy_db)
    monkeypatch.undo()

    conn = sqlite3.connect(legacy_db)
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    conn.close()

    conn = db.connect(legacy_db)
    try:
        assert len(db.all_projects(conn)) == 1
    finally:
        conn.close()


def test_gold_standard_can_reuse_a_verra_project_id(legacy_db):
    """Registries number independently — id 1728 exists in both, distinctly."""
    conn = db.connect(legacy_db)
    try:
        db.upsert_projects(
            conn,
            settings.GOLD_STANDARD,
            [({"project_id": 1728, "project_name": "A different project"}, {})],
        )
        rows = {r["registry"]: r["project_name"] for r in db.all_projects(conn)}
        assert rows[settings.VERRA] == "Bundled Wind Power Project"
        assert rows[settings.GOLD_STANDARD] == "A different project"
    finally:
        conn.close()
