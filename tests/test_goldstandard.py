"""Gold Standard adapter: normalisation and credit accounting. No network.

Runs against `tests/fixtures/gs-*.json`, captured from the live API on
2026-07-28.
"""

from __future__ import annotations

import json

import pytest

from carbon_scraper import db, excel, settings
from carbon_scraper.registries.goldstandard import api as gs

FIXTURES = settings.FIXTURES_DIR


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def projects():
    return _load("gs-projects.json")


@pytest.fixture(scope="module")
def credits():
    return _load("gs-credits.json")


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def test_page_size_caps_match_what_the_api_actually_allows():
    """Measured limits. `projects` clamps silently at 150; `credits` 403s above 25."""
    assert settings.GS_PROJECT_PAGE_SIZE == 150
    assert settings.GS_CREDIT_PAGE_SIZE == 25


def test_project_normalises_onto_the_shared_columns(projects):
    row = gs.normalize_project(projects[0])
    assert set(row) <= set(db.PROJECT_FIELDS)
    assert row["project_id"] == int(projects[0]["id"])
    assert row["project_name"] == projects[0]["name"]
    assert row["country_name"] == projects[0]["country"]
    assert row["country_code"] == projects[0]["country_code"]


def test_external_id_comes_from_sustaincert_not_the_name():
    """A project's name can carry its parent programme's GS number.

    The fixture project is named 'GS23711- ...' but is itself GS23718, so
    parsing the name would mislabel it.
    """
    record = {"id": "5672", "sustaincert_id": 23718, "name": "GS23711- Mozambique VPA1"}
    assert gs.external_id(record) == "GS23718"
    assert gs.external_id({"id": "1"}) is None


def test_tipo_macro_is_the_raw_gold_standard_type(projects):
    """Agreed with the user: carry `type` through untranslated.

    Verra's sectoral scopes and Gold Standard's types are different
    vocabularies; relating them is a later decision, not a silent mapping.
    """
    for record in projects:
        assert gs.normalize_project(record)["sectoral_scope"] == record["type"]


def test_city_is_never_invented(projects):
    """Gold Standard publishes no city. The cell stays blank."""
    for record in projects:
        assert gs.normalize_project(record).get("city") is None


def test_credit_normalisation_links_to_its_embedded_project(credits):
    row = gs.normalize_credit(credits[0])
    assert set(row) <= set(db.CREDIT_EVENT_FIELDS)
    assert row["project_id"] == int(credits[0]["project"]["id"])
    assert row["quantity"] == credits[0]["number_of_credits"]
    assert row["status"] in ("ISSUED", "RETIRED")


def test_credit_labels_do_not_become_an_additional_certification(credits):
    """`labels` is the product class (EMISSION_REDUCTION), not a co-certification."""
    for record in credits:
        assert gs.normalize_credit(record)["additional_certification"] is None


def test_issued_total_counts_retired_blocks_too(conn):
    """A block's status is its CURRENT state, not an event type.

    A block issued and later retired reads RETIRED, so summing only
    status='ISSUED' would report zero issued credits for any project that has
    since retired them.
    """
    db.upsert_projects(
        conn,
        settings.GOLD_STANDARD,
        [({"project_id": 1890, "project_name": "Cookstoves", "country_code": "KE"}, {})],
    )
    db.upsert_credit_events(
        conn,
        settings.GOLD_STANDARD,
        gs.CREDITS,
        [
            {"entity_id": 1, "project_id": 1890, "quantity": 100, "status": "ISSUED"},
            {"entity_id": 2, "project_id": 1890, "quantity": 250, "status": "RETIRED"},
        ],
    )

    totals = db.credit_totals(conn)[(settings.GOLD_STANDARD, 1890)]
    assert totals["issuances"] == 350
    assert totals["retirements"] == 250
    assert totals["cancellations"] == 0


def test_continent_is_derived_from_the_country_code(conn):
    """Gold Standard publishes no region, so Continent comes from ISO-2 rules."""
    from carbon_scraper import derive

    rulesets = derive.load_rulesets()
    values = dict(
        (column, value)
        for column, value, _rule in derive.derive_for_project(
            {"country_code": "KE"}, rulesets
        )
    )
    assert values["Continent"] == "Africa"


def test_norway_survives_the_yaml_boolean_trap():
    """YAML 1.1 reads a bare `NO` as false; the codes must stay quoted."""
    from carbon_scraper import derive

    rulesets = derive.load_rulesets()
    values = dict(
        (column, value)
        for column, value, _rule in derive.derive_for_project(
            {"country_code": "NO"}, rulesets
        )
    )
    assert values["Continent"] == "Europe"


def test_both_registries_share_one_sheet(conn):
    """One deliverable, `Standard` and `Registry` separating the two sources."""
    db.upsert_projects(
        conn,
        settings.GOLD_STANDARD,
        [
            (
                {
                    "project_id": 1890,
                    "external_id": "GS7495",
                    "project_name": "Cookstoves",
                    "standard_name": "Gold Standard for the Global Goals",
                    "sectoral_scope": "Energy Efficiency - Domestic",
                },
                {},
            )
        ],
    )
    db.upsert_projects(
        conn,
        settings.VERRA,
        [({"project_id": 1890, "external_id": "1890", "project_name": "Wind"}, {})],
    )

    _, rows = excel.build_rows(conn)
    # Same numeric id in both registries must stay two distinct rows.
    assert len(rows) == 2
    assert {r["Registry"] for r in rows} == {"Verra VCS", "Gold Standard"}
    gs_row = next(r for r in rows if r["Registry"] == "Gold Standard")
    assert gs_row["Project ID"] == "GS7495"
    assert gs_row["Standard"] == "Gold Standard for the Global Goals"
    assert gs_row["Project URL"].startswith(settings.GS_SITE)
