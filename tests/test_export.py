"""Export and storage behaviour. No network — an in-memory database only."""

from __future__ import annotations

import pytest

from carbon_scraper import db, excel, settings
from carbon_scraper.registries.verra import api as verra

VERRA = settings.VERRA

PROJECT = {
    "projectId": 1728,
    "vcsProjectId": "1728",
    "projectName": "Bundled Wind Power Project",
    "standardName": "Verified Carbon Standard",
    "status": "Registered",
    "sectoralScope": "Energy industries (renewable/non-renewable sources)",
    "methodologies": "ACM0002 (Version NA)",
    "regionName": "Asia",
    "countryName": "India",
    "stateProvince": "Telangana",
    "city": "Hyderabad",
    "avgAnnualVolVcu": 1000,
    "creditPeriodStartDate": "2015-12-30T00:00:00",
    "creditPeriodEndDate": "2025-12-29T00:00:00",
}


def add_project(conn, record=PROJECT):
    """Store one Verra project the way the adapter would."""
    return db.upsert_projects(
        conn, VERRA, [(verra.normalize_project(record), record)]
    )


def add_events(conn, resource, records):
    return db.upsert_credit_events(
        conn, VERRA, resource, [verra.normalize_credit(r) for r in records]
    )


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def test_requested_columns_are_the_schema():
    """The Excel layout must come from the asset file, not from code."""
    fields = settings.read_requested_fields()
    assert fields[0] == "Project ID"
    assert "Total Credits Sold" in fields
    # The file has a trailing space on 'Continent '; it must be normalised.
    assert "Continent" in fields
    assert all(f == f.strip() for f in fields)


def test_export_columns_match_fields_asked_in_order(conn):
    add_project(conn)
    columns, _ = excel.build_rows(conn)
    requested = settings.read_requested_fields()
    assert columns[: len(requested)] == requested
    assert columns[len(requested):] == excel.EXTRA_COLUMNS


def test_upserts_are_idempotent(conn):
    add_project(conn)
    add_project(conn)
    assert db.counts(conn)[f"{VERRA} projects"] == 1


def test_credit_totals_aggregate_per_project(conn):
    add_project(conn)
    add_events(
        conn,
        "retirements",
        [
            {"entityId": 1, "projectId": 1728, "holdingQuantity": 100, "beneficialOwner": "Acme"},
            {"entityId": 2, "projectId": 1728, "holdingQuantity": 50, "beneficialOwner": ""},
        ],
    )
    add_events(
        conn, "issuances", [{"entityId": 9, "projectId": 1728, "holdingQuantity": 500}]
    )

    totals = db.credit_totals(conn)[(VERRA, 1728)]
    assert totals["retirements"] == 150
    assert totals["issuances"] == 500
    # Only the retirement naming a beneficiary counts under the narrow reading.
    assert db.retired_by_beneficiary(conn)[(VERRA, 1728)] == 100


def test_sold_equals_retired_by_default(conn):
    add_project(conn)
    add_events(
        conn,
        "retirements",
        [{"entityId": 1, "projectId": 1728, "holdingQuantity": 100, "beneficialOwner": "Acme"},
         {"entityId": 2, "projectId": 1728, "holdingQuantity": 50}],
    )
    _, rows = excel.build_rows(conn)
    row = rows[0]
    # The agreed business reading: retired VCUs are treated as sold, so these
    # two columns hold the same number. Config can flip this.
    assert row["Total Credits Sold"] == row["Total Credits Retired"] == 150


def test_additional_certification_comes_from_unit_records(conn):
    """Verra leaves this null on projects; it only appears on unit rows."""
    add_project(conn)
    add_events(
        conn,
        "holdings",
        [{"entityId": 5, "projectId": 1728, "additionalCertification": "CORSIA – Pilot Phase"}],
    )
    _, rows = excel.build_rows(conn)
    assert rows[0]["Additional Certification"] == "CORSIA – Pilot Phase"


def test_unavailable_values_stay_blank(conn):
    """No ledger data must mean empty cells, never zeroes or invented numbers."""
    add_project(conn)
    _, rows = excel.build_rows(conn)
    row = rows[0]
    for column in ("Total Credits Issued", "Total Credits Sold", "Total Credits Cancelled"):
        assert row[column] is None
    # Derivation has not run, so classified columns are blank too.
    assert row["Tipo Micro de Projeto"] is None


def test_tipo_macro_is_verras_sectoral_scope_not_derived(conn):
    add_project(conn)
    _, rows = excel.build_rows(conn)
    assert rows[0]["Tipo Macro de Projeto"] == PROJECT["sectoralScope"]


def test_verra_continent_uses_the_registrys_own_region(conn):
    """Verra states a region; the country-code rules must not override it."""
    add_project(conn)
    _, rows = excel.build_rows(conn)
    assert rows[0]["Continent"] == "Asia"


def test_writes_a_readable_workbook(conn, tmp_path):
    from openpyxl import load_workbook

    add_project(conn)
    path, count, _previous = excel.write_xlsx(conn, tmp_path / "out.xlsx")
    assert count == 1

    sheet = load_workbook(path).active
    header = [c.value for c in sheet[1]]
    assert header[:4] == ["Project ID", "Project Name", "Standard", "Tipo Macro de Projeto"]
    assert sheet.freeze_panes == "A2"


# -- versioned deliveries --------------------------------------------------

def test_first_export_is_v1(tmp_path):
    base = tmp_path / "carbon-projects.xlsx"
    path, previous = excel.next_version_path(base)
    assert path.name == "carbon-projects_v1.xlsx"
    assert previous is None


def test_each_export_writes_the_next_version_and_keeps_the_last(tmp_path):
    """A delivery already sent to the business is never overwritten."""
    base = tmp_path / "carbon-projects.xlsx"
    first, _ = excel.next_version_path(base)
    first.write_bytes(b"v1")

    second, previous = excel.next_version_path(base)
    assert second.name == "carbon-projects_v2.xlsx"
    assert previous == first
    assert first.read_bytes() == b"v1"


def test_an_unversioned_delivery_is_adopted_as_v1(tmp_path):
    """Existing sheets predate the rule; keep them as the start of history."""
    base = tmp_path / "carbon-projects.xlsx"
    legacy = tmp_path / "verra-projects.xlsx"
    legacy.write_bytes(b"delivered")

    path, previous = excel.next_version_path(base)
    assert path.name == "carbon-projects_v2.xlsx"
    assert previous is not None and previous.name == "carbon-projects_v1.xlsx"
    assert previous.read_bytes() == b"delivered"


def test_version_numbers_do_not_sort_as_text(tmp_path):
    """v10 must follow v9, not v1."""
    base = tmp_path / "carbon-projects.xlsx"
    for n in range(1, 10):
        (tmp_path / f"carbon-projects_v{n}.xlsx").write_bytes(b"x")
    path, previous = excel.next_version_path(base)
    assert path.name == "carbon-projects_v10.xlsx"
    assert previous.name == "carbon-projects_v9.xlsx"
