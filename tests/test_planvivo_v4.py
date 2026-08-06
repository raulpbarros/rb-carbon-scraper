"""Plan Vivo V4 on the legacy Markit view: parsing, filtering, merging. No network.

Runs against `tests/fixtures/planvivo-v4-*.html` and
`tests/fixtures/markit-off-standard-retirements.html`, captured from the live
public view on 2026-07-29.

The fixtures are the real pages, chosen for what they contain rather than for
being tidy. Page 2 is the interesting one: it carries a project merged across
two `<tr>`s with a `rowspan`, the malformed rows this view emits where a
`style` attribute has swallowed a data payload, and a grouped project whose
sub-project rows share one `master-project.jsp` id. The off-standard fixture is
a Social Carbon request that came back full of rows reading "No Established
Standard" — proof that `standardId` is a request rather than a filter, and the
reason every row is checked against the standard it claims.
"""

from __future__ import annotations

import pytest

from carbon_scraper import db, settings
from carbon_scraper.http_client import RetryableStatus
from carbon_scraper.registries import ADAPTERS, adapter_class, adapter_classes
from carbon_scraper.registries import tables
from carbon_scraper.registries.markit import api as markit
from carbon_scraper.registries.planvivo import api as pv5
from carbon_scraper.registries.planvivo.v4 import PlanVivoV4API

FIXTURES = settings.FIXTURES_DIR

PAGE1 = (FIXTURES / "planvivo-v4-projects-page1.html").read_text(encoding="utf-8")
PAGE2 = (FIXTURES / "planvivo-v4-projects-page2.html").read_text(encoding="utf-8")
ISSUANCES = (FIXTURES / "planvivo-v4-issuances.html").read_text(encoding="utf-8")
RETIREMENTS = (FIXTURES / "planvivo-v4-retirements.html").read_text(encoding="utf-8")
DETAIL = (FIXTURES / "planvivo-v4-detail.html").read_text(encoding="utf-8")
OFF_STANDARD = (FIXTURES / "markit-off-standard-retirements.html").read_text(
    encoding="utf-8"
)

#: Grouped project: two sub-project rows, one `master-project.jsp` id.
SCOLEL_TE = 95
#: Renders twice, each row followed by a malformed continuation row.
SOFALA = 100000000000169


class FakeClient:
    """Serves pages from fixtures. Records every request."""

    def __init__(self, pages=None, detail=DETAIL):
        self.calls = []
        self._pages = pages or {}
        self._detail = detail

    def get_text(self, url, *, params=None, headers=None, **kwargs):
        params = params or {}
        self.calls.append((url, dict(params)))
        if url == markit.DETAIL or url == markit.MASTER_DETAIL:
            return self._detail
        key = (params.get("entity"), int(params.get("start", 0)))
        # Anything past the end is an empty table — which is the only
        # end-of-list this view states honestly.
        return self._pages.get(key, EMPTY_PAGE)

    def close(self):
        pass


#: A page with the headings and no rows, exactly as the live view answers past
#: the end of a feed — including the Next link it never disables.
EMPTY_PAGE = """
<html><body>
<table class="table">
  <tr><th>Name</th><th>Category</th><th>Standard Name</th><th>Project Type</th>
      <th>Status</th><th>PIUs Listed</th><th>Validator</th><th>Developer</th>
      <th>Country</th><th>Details</th></tr>
</table>
<ul class="pager"><li id="page_no"></li>
<li id="next_pager" class="next"><a href="?start=999">Next</a></li></ul>
</body></html>
"""


def projects_client():
    return FakeClient(
        {
            ("project", 0): PAGE1,
            ("project", 15): PAGE2,
            ("issuance", 0): ISSUANCES,
            ("retirement", 0): RETIREMENTS,
        }
    )


@pytest.fixture()
def adapter():
    return PlanVivoV4API(projects_client())




# -- the table parser ------------------------------------------------------


def test_headings_are_read_from_the_page():
    table = tables.results_table(PAGE1)
    assert table.headings[:4] == ["Name", "Category", "Standard Name", "Project Type"]


NESTED = """
<html><body>
<table class="layout">
  <tr><td>
    <table class="results">
      <tr><th>Name</th><th>Country</th><th>Details</th></tr>
      <tr><td>Sofala</td><td>Mozambique</td>
          <td><a href="project.jsp?project_id=7">View</a></td></tr>
    </table>
  </td></tr>
</table>
</body></html>
"""


def test_a_nested_table_does_not_pollute_the_one_around_it():
    """A layout table wrapping the results is what `results_table` is for.

    Flattened into the outer table, the inner `<th>`s append to the outer
    headings and the inner `<tr>`s to its rows — which shifts the
    heading -> index mapping for every later row and produces real values
    under the wrong column names. Every fixture here holds exactly one
    table, which is why that was invisible.
    """
    parsed = tables.parse_tables(NESTED)
    assert len(parsed) == 2

    results = tables.results_table(NESTED)
    assert results.headings == ["Name", "Country", "Details"]
    assert len(results.rows) == 1
    assert results.rows[0].get("Name") == "Sofala"
    assert results.rows[0].get("Country") == "Mozambique"


def test_a_merged_cell_carries_down_instead_of_shifting_columns():
    """A rowspan continuation must not slide Category into the name column.

    Read positionally, the row after a merged one puts "Carbon" where the
    project name belongs: a real id with a real-looking name and nothing to
    say it is wrong.
    """
    table = tables.results_table(PAGE2)
    for row in table.rows:
        assert row.values.get("Name") != "Carbon"
        assert row.values.get("Category") in (None, "", "Carbon")


def test_a_carried_link_is_not_the_rows_own():
    """Own links are what separate a record from the tail of a merged one."""
    table = tables.results_table(PAGE2)
    carried = [r for r in table.rows if r.links and not r.own_links]
    assert carried, "page 2 is the fixture because it has merged rows"


def test_paging_stops_on_a_short_page_not_on_the_pager(adapter):
    """The Next link is never disabled, including past the end of the feed.

    Both fixture pages hold a full PAGE_SIZE of *records* — page 2 parses to
    more `<tr>`s than that, because two of them are the malformed rows this
    view emits, and to exactly 15 records — so paging correctly asks for a
    third, which comes back empty and ends it. Trusting the pager instead
    would have walked to MAX_PAGES.
    """
    adapter.rows(markit.PROJECTS)
    starts = [
        int(params.get("start", 0))
        for url, params in adapter.client.calls
        if params.get("entity") == "project"
    ]
    assert starts == [0, 15, 30]

    page2 = tables.results_table(PAGE2)
    assert len(page2.rows) > markit.PAGE_SIZE
    assert sum(1 for r in page2.rows if markit.is_record(r)) == markit.PAGE_SIZE


class ExplodingPastTheEnd(FakeClient):
    """A feed whose length is an exact multiple of the page size.

    The real view answers `start=5040` on a 5,034-row feed with HTTP 500, so
    a feed that ends exactly on a page boundary reaches its end only by
    asking for a page that does not exist.
    """

    def get_text(self, url, *, params=None, headers=None, **kwargs):
        params = params or {}
        if int(params.get("start", 0)) > 0 and url == markit.INDEX:
            raise RetryableStatus("500 from mer.markit.com")
        return super().get_text(url, params=params, headers=headers, **kwargs)


def test_a_500_past_the_end_is_the_end_not_a_failure():
    client = ExplodingPastTheEnd({("project", 0): PAGE1})
    adapter = PlanVivoV4API(client)
    assert len(adapter.rows(markit.PROJECTS)) == markit.PAGE_SIZE


def test_a_500_on_the_very_first_page_still_fails():
    """An empty registry and a broken one must not look the same."""

    class BrokenFromTheStart(FakeClient):
        def get_text(self, url, *, params=None, headers=None, **kwargs):
            raise RetryableStatus("500 from mer.markit.com")

    with pytest.raises(RetryableStatus):
        PlanVivoV4API(BrokenFromTheStart()).rows(markit.PROJECTS)


# -- what is and is not a record -------------------------------------------


def test_malformed_style_attribute_rows_are_not_records(adapter):
    """The view emits <tr>s whose style attribute swallowed a data payload.

    They parse into plausible values in the wrong columns, so the count has
    to be exact: page 2 carries 17 `<tr>`s of which 2 are these, and a bound
    like `kept < parsed` passes when *any* row anywhere is dropped.
    """
    assert len(tables.results_table(PAGE1).rows) == 15
    assert len(tables.results_table(PAGE2).rows) == 17
    assert sum(1 for r in tables.results_table(PAGE2).rows if markit.is_record(r)) == 15

    # 32 `<tr>`s -> 30 records (the 2 malformed ones gone) -> 29 kept, because
    # Sofala renders twice identically and a repeat is one project.
    kept = adapter.rows(markit.PROJECTS)
    assert len(kept) == 29

    # Recognised structurally, not by pattern-matching the payload: a record
    # carries its own "View" link rather than one inherited via a rowspan.
    assert all(tables.link_targets(row, own_only=True) for row in kept)


def test_rows_of_another_standard_are_dropped():
    """`standardId` is a request, not a filter.

    Asking this view for Social Carbon returns rows whose Standard column
    reads "No Established Standard". Whatever the parameter does, it does not
    mean "only this standard".
    """
    client = FakeClient({("retirement", 0): OFF_STANDARD})
    adapter = PlanVivoV4API(client)
    assert adapter.rows(markit.RETIREMENTS) == []

    off = tables.results_table(OFF_STANDARD)
    assert off.rows, "the fixture is only useful if it has rows to reject"


def test_the_standard_filter_is_not_a_no_op(adapter):
    """Guard the guard: with no expected standard, the rows come through."""
    adapter.standard_names = ()
    assert adapter.rows(markit.PROJECTS)


def test_rejecting_every_row_is_an_error_not_an_empty_registry(caplog):
    """A renamed Standard column empties the feed AND its expected total.

    `count()` reads this same list, so the sync then reports
    "0 (registry reports 0)" — an empty registry that reconciles perfectly,
    which is the exact failure shape this codebase keeps meeting.
    """
    client = FakeClient({("retirement", 0): OFF_STANDARD})
    adapter = PlanVivoV4API(client)
    with caplog.at_level("ERROR"):
        assert adapter.rows(markit.RETIREMENTS) == []
    assert "every one of" in caplog.text
    assert "Standard column missing or renamed" in caplog.text
    # It names what the feed actually said, which is how the cause is found.
    assert "No Established Standard" in caplog.text


def test_dropping_some_rows_is_not_an_error(adapter, caplog):
    """The ordinary case — a mixed feed — stays at INFO."""
    with caplog.at_level("ERROR"):
        assert adapter.rows(markit.PROJECTS)
    assert "every one of" not in caplog.text


# -- progress reporting ----------------------------------------------------


def test_progress_is_cumulative_not_per_record(adapter, progress):
    """Both sinks read `done` as an absolute position against `total`.

    `gui/app.py` computes `done * 100 / total` and the CLI prints
    `{done}/{total}`, so an adapter reporting `1` per record pins the bar at
    "1 of 30" for the whole scrape and then jumps to 100% on `final()`. Every
    other adapter passes a running count; this one did not.
    """
    list(adapter.iter_projects(progress=progress))
    progress.assert_cumulative(adapter.project_total())


def test_credit_progress_is_cumulative_too(adapter, progress):
    list(adapter.iter_credits(markit.RETIREMENTS, progress=progress))
    progress.assert_cumulative(adapter.count(markit.RETIREMENTS))


# -- merging rows that share a project id ----------------------------------


def test_a_project_id_appearing_twice_becomes_one_project(adapter):
    """Two ways a project renders more than once, both ending in one row.

    Sofala publishes two rows identical in every column, each trailed by a
    malformed continuation: four `<tr>`s, one project. Scolel té publishes two
    genuinely different sub-project rows under one master id: two rows kept,
    one project stored. Either way `project_id` is half the primary key, so
    what must not happen is two records racing to own it.
    """
    parsed = tables.results_table(PAGE2).rows
    assert len([r for r in parsed if r.values.get("Name", "").startswith("Sofala")]) > 1

    grouped = adapter.project_rows()
    assert len(grouped[SOFALA]) == 1
    assert len(grouped[SCOLEL_TE]) == 2
    assert adapter.project_total() == len(grouped)
    assert adapter.project_total() < len(adapter.rows(markit.PROJECTS)) + 1


def test_merging_keeps_every_distinct_project_type(adapter):
    """Scolel té's two sub-projects carry different types; both are kept."""
    rows = adapter.project_rows()[SCOLEL_TE]
    types = {r.get("Project Type") for r in rows}
    assert len(types) > 1

    record = adapter.normalize_project(
        {"rows": [dict(r.values) for r in rows], "master": True}, SCOLEL_TE
    )
    for value in types:
        assert value in record["sectoral_scope"]
    assert record["extra"]["mergedRows"] == len(rows)


def test_a_grouped_project_links_to_the_master_page(adapter):
    record = adapter.normalize_project({"rows": [], "master": True}, SCOLEL_TE)
    assert "master-project.jsp" in record["detail_url"]


# -- normalisation ---------------------------------------------------------


def test_projects_only_use_known_columns(adapter):
    for record, _raw in adapter.iter_projects():
        assert set(record) <= set(db.PROJECT_FIELDS)


def test_credits_only_use_known_columns(adapter):
    for resource in (markit.ISSUANCES, markit.RETIREMENTS):
        for record in adapter.iter_credits(resource):
            assert set(record) <= set(db.CREDIT_EVENT_FIELDS)


def test_the_standard_name_says_which_era_a_row_is_from(adapter):
    """V4 and V5 share one registry, so the label is what tells them apart."""
    for record, _raw in adapter.iter_projects():
        assert record["standard_name"] == settings.PV4_STANDARD_LABEL
    assert settings.PV4_STANDARD_LABEL != "PV Climate"


def test_project_type_is_carried_through_untranslated(adapter):
    """This platform's own vocabulary, as every registry's is."""
    scopes = {
        record.get("sectoral_scope")
        for record, _raw in adapter.iter_projects()
        if record.get("sectoral_scope")
    }
    assert any("REDD" in s or "orest" in s for s in scopes)


@pytest.mark.parametrize(
    ("cell", "country", "state"),
    [
        ("Viet Nam, Kon Tum", "Viet Nam", "Kon Tum"),
        ("Bolivia, Plurinational State of, Cochabamba", "Bolivia, Plurinational State of", "Cochabamba"),
        ("Tanzania, United Republic of, Kagera", "Tanzania, United Republic of", "Kagera"),
        # An ISO name whose comma is its own, with no state after it. Split on
        # the last comma and this becomes the country "Korea" in the state
        # "Republic of" — a place the registry never published.
        ("Korea, Republic of", "Korea, Republic of", None),
        ("Fiji", "Fiji", None),
        ("", None, None),
    ],
)
def test_country_and_state_come_out_of_one_cell(cell, country, state):
    assert markit.split_country(cell) == (country, state)


def test_pending_issuance_units_are_not_issued_credits(adapter):
    """PIUs are a listing. Writing them to units_issued would state issuance."""
    for record, _raw in adapter.iter_projects():
        assert "units_issued" not in record


# -- credit events ---------------------------------------------------------


def test_event_ids_are_stable_across_runs(adapter):
    first = [e["entity_id"] for e in adapter.iter_credits(markit.RETIREMENTS)]
    second = [
        e["entity_id"]
        for e in PlanVivoV4API(projects_client()).iter_credits(markit.RETIREMENTS)
    ]
    assert first == second
    assert first, "the retirement fixture has rows"


def test_identical_rows_stay_two_events(adapter):
    """Two identical ledger rows are two events, not one.

    The key is hashed from the row's values because the view publishes no id,
    so an occurrence index is what keeps two identical rows from collapsing
    into one and under-reporting the ledger.
    """
    ids = [e["entity_id"] for e in adapter.iter_credits(markit.RETIREMENTS)]
    assert len(ids) == len(set(ids))


def test_a_markit_event_id_cannot_collide_with_an_sp_one():
    """Plan Vivo's ledgers hold rows from both systems under one registry."""
    assert markit.event_id("VERRA", "x") != markit.event_id(settings.PLAN_VIVO, "x")


def test_the_account_is_not_recorded_as_a_beneficiary(adapter):
    """The holder is not the named third party a retirement was made for.

    The view publishes both columns. `beneficiary` must come from `Beneficial
    Owner` alone: copying the `Account` across when it is empty would invent
    a beneficiary the registry never named.
    """
    table = tables.results_table(RETIREMENTS)
    assert {"Account", "Beneficial Owner"} <= set(table.headings)

    owners = {r.get("Beneficial Owner") for r in table.rows if r.get("Beneficial Owner")}
    beneficiaries = {e["beneficiary"] for e in adapter.iter_credits(markit.RETIREMENTS)}
    assert beneficiaries - {None} == owners
    assert "" not in beneficiaries


def test_an_account_never_leaks_into_the_beneficiary():
    """The fixture leaves `Account` blank throughout, so state it directly."""
    row = tables.Row(
        values={
            "Account": "Some Broker Ltd",
            "Beneficial Owner": "",
            "Retirement Quantity": "10",
            "Vintage": "2019",
        },
        links=[],
    )
    adapter = PlanVivoV4API(FakeClient())
    record = adapter.normalize_credit(row, markit.RETIREMENTS, {})
    assert record["beneficiary"] is None


# -- the registry it belongs to --------------------------------------------


def test_plan_vivo_has_two_adapters():
    """One registry, two systems. The reason ADAPTERS holds a tuple."""
    classes = adapter_classes(settings.PLAN_VIVO)
    assert classes == (pv5.PlanVivoAPI, PlanVivoV4API)
    assert adapter_class(settings.PLAN_VIVO) is pv5.PlanVivoAPI


def test_both_adapters_store_under_one_registry():
    for cls in adapter_classes(settings.PLAN_VIVO):
        assert cls.registry == settings.PLAN_VIVO


def test_every_registry_lists_at_least_one_adapter():
    for name, loaders in ADAPTERS.items():
        assert loaders, name
        for cls in adapter_classes(name):
            assert cls.registry == name


def test_the_ledger_names_are_verras_not_gold_standards():
    """`db.credit_totals()` branches on the exact string `credits`."""
    assert "credits" not in PlanVivoV4API.ledgers
    assert set(PlanVivoV4API.ledgers) == set(pv5.PlanVivoAPI.ledgers)


def test_rows_from_both_systems_land_in_one_registry(conn, adapter):
    db.upsert_projects(conn, settings.PLAN_VIVO, adapter.iter_projects())
    stored = db.all_projects(conn, settings.PLAN_VIVO)
    assert stored
    assert {row["registry"] for row in stored} == {settings.PLAN_VIVO}
    assert {row["standard_name"] for row in stored} == {settings.PV4_STANDARD_LABEL}
