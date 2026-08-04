"""Gold Standard adapter: normalisation and credit accounting. No network.

Runs against `tests/fixtures/gs-*.json`, captured from the live API on
2026-07-28.
"""

from __future__ import annotations

import pytest

from carbon_scraper import db, excel, settings
from carbon_scraper.registries.goldstandard import api as gs

from conftest import load_fixture


@pytest.fixture(scope="module")
def projects():
    return load_fixture("gs-projects.json")


@pytest.fixture(scope="module")
def credits():
    return load_fixture("gs-credits.json")


class FakeClient:
    """Serves fixed-size pages and records the parameters it was asked for.

    Counts come back in **headers** here, not the body, which is what the
    reconciliation reads.
    """

    def __init__(self, pages, total, credit_total=None):
        #: [n_records_on_page_1, n_records_on_page_2, ...]
        self._pages = list(pages)
        self._total = total
        self._credit_total = credit_total
        self.calls: list[dict] = []

    def get_json_with_headers(self, url, *, params=None, headers=None, **kwargs):
        params = dict(params or {})
        self.calls.append(params)
        page = int(params["page"])
        size = int(params["size"])
        count = self._pages[page - 1] if page <= len(self._pages) else 0
        records = [{"id": (page - 1) * 1000 + i} for i in range(min(count, size))]
        response_headers = {gs.TOTAL_HEADER: str(self._total)}
        if self._credit_total is not None:
            response_headers[gs.CREDIT_TOTAL_HEADER] = str(self._credit_total)
        return records, response_headers

    def close(self):
        pass


# -- paging ----------------------------------------------------------------
#
# The 182,989-record half of this registry, and the three documented traps are
# all here rather than in normalisation.


def test_page_size_caps_match_what_the_api_actually_allows():
    """Measured limits. `projects` clamps silently at 150; `credits` 403s above 25."""
    assert settings.GS_PROJECT_PAGE_SIZE == 150
    assert settings.GS_CREDIT_PAGE_SIZE == 25


def test_each_resource_asks_for_its_own_measured_page_size():
    """A single shared page size is wrong in both directions.

    `projects` clamps 1000 to 150 with no error and no marker, so a naive
    loop that trusts its own arithmetic skips 85% of the registry.
    `credits` answers anything above 25 with a **403**, which reads like a
    block and is a response-size limit.
    """
    # The first call is `count()` asking for one row; the data pages follow.
    client = FakeClient(pages=[150, 10], total=160)
    list(gs.GoldStandardAPI(client).iter_raw(gs.PROJECT))
    assert client.calls[0]["size"] == 1
    assert {c["size"] for c in client.calls[1:]} == {settings.GS_PROJECT_PAGE_SIZE}

    client = FakeClient(pages=[25, 5], total=30)
    list(gs.GoldStandardAPI(client).iter_raw(gs.CREDITS))
    assert {c["size"] for c in client.calls[1:]} == {settings.GS_CREDIT_PAGE_SIZE}


def test_paging_stops_on_a_short_page_and_reconciles(caplog, progress):
    """Three pages, then stop — no request for a fourth."""
    client = FakeClient(pages=[150, 150, 40], total=340)
    with caplog.at_level("ERROR"):
        records = list(gs.GoldStandardAPI(client).iter_raw(gs.PROJECT, progress=progress))

    assert len(records) == 340
    # One count() call at size=1, then three pages.
    assert [call["page"] for call in client.calls] == [1, 1, 2, 3]
    assert "INCOMPLETE" not in caplog.text
    progress.assert_cumulative(340)


def test_paging_stops_on_an_empty_page_too():
    """A feed whose length is an exact multiple of the page size."""
    client = FakeClient(pages=[150, 150], total=300)
    records = list(gs.GoldStandardAPI(client).iter_raw(gs.PROJECT))
    assert len(records) == 300
    assert [call["page"] for call in client.calls] == [1, 1, 2, 3]


def test_a_short_read_is_reported_as_incomplete(caplog):
    """The registry's own header is the check. Never trust a row count just
    because the run finished without an exception."""
    client = FakeClient(pages=[150, 40], total=500)
    with caplog.at_level("ERROR"):
        records = list(gs.GoldStandardAPI(client).iter_raw(gs.PROJECT))

    assert len(records) == 190
    assert "INCOMPLETE" in caplog.text
    assert "190 of 500" in caplog.text


def test_limit_stops_early_without_claiming_a_short_read(caplog):
    """`--limit` is a smoke test, not a failed sync."""
    client = FakeClient(pages=[150, 150, 40], total=340)
    with caplog.at_level("ERROR"):
        records = list(
            gs.GoldStandardAPI(client).iter_raw(gs.PROJECT, max_records=25)
        )
    assert len(records) == 25
    assert "INCOMPLETE" not in caplog.text


def test_the_counts_come_from_headers_not_the_body():
    client = FakeClient(pages=[1], total=4141, credit_total=182_989)
    adapter = gs.GoldStandardAPI(client)
    assert adapter.project_total() == 4141
    assert adapter.credit_total() == 182_989


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
