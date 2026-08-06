"""ACR adapter and the GreenTrace platform: form posts, the clamp, the four-ledgers-in-one-URL. No network.

Runs against `tests/fixtures/acr-*.json`, captured from the live ICE
GreenTrace API on 2026-08-04. Twelve projects out of the live 994, chosen for
being awkward rather than representative:

* **ACR1187 and ACR1202** carry the multi-state form of
  `projectSiteLocState` — `US-FL, US-SC, US-GA` — where ISO 3166-2 codes and
  uppercase names share one field.
* **ACR1168 ("Israel") and ACR1215 ("Lower Saxony")** are the third form of
  the same field: an ordinary-case sub-national name outside the US.
* **ACR0838 (CA), ACR1203 (MX), ACR1215 (DE), ACR1168 (IL)** are the non-US
  projects, and none of them publishes a country *name* — nor does any US one.
* **ACR0885, ACR0628 and ACR0273** state a reserve quantity on the detail and
  own issuance events split across several blocks, which is where
  `issuanceQuantity` restates the parent's total.
* **ACR0119** appears in the retirement fixture and **not** in the project
  fixture, with three retirements that name no beneficiary. Both halves
  matter: a ledger row whose project is outside the slice, and the rows that
  prove `beneficiary` is not filled from the account name.

The credit fixtures keep one `registryLabels` entry per row instead of the
live ten. That list is why a live retirement page is 16 MB and the adapter
reads none of it.
"""

from __future__ import annotations

import copy

import httpx
import pytest

from carbon_scraper import db, derive, settings
from carbon_scraper import registries
from carbon_scraper.http_client import RetryableStatus
from carbon_scraper.registries import base
from carbon_scraper.registries.acr import api as acr
from carbon_scraper.registries.greentrace import api as greentrace
from carbon_scraper.registries.text import hashed_id

from conftest import RecordingProgress, load_fixture as _load

REGISTRY = settings.ACR

# What the fixtures' own `totalCount`s report.
PROJECTS = 12
COUNTS = {acr.ISSUANCES: 31, acr.RETIREMENTS: 21, acr.CANCELLATIONS: 7}

# The issuance fixture's 31 blocks belong to 20 issuance events, so summing
# `issuanceQuantity` per row restates the parents and nearly doubles the total.
ISSUED_UNITS = 1722469.0
PARENT_FIELD_UNITS = 3050565.0

# In the retirement fixture, as in the live ledger, only some rows name a third
# party. All 21 name an account.
RETIREMENTS_WITH_A_BENEFICIARY = 18


class FakeClient:
    """Serves GreenTrace's routes from fixtures, honouring `offset` and `max`.

    **It clamps `max` to two rows**, which is what the live platform does at
    2000 and says nothing about. A pager that trusted the row count it got
    back would stop after the first page here, which is the whole point.
    """

    CLAMP = 2

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self._details = _load("acr-details.json")
        self._reports = {
            "project-summaries": ("projects", _load("acr-projects.json")),
            "ISSUED": ("issuedCredits", _load("acr-issuances.json")),
            "RETIRED": ("retiredCredits", _load("acr-retirements.json")),
            "CANCELED": ("cancelledCredits", _load("acr-cancellations.json")),
        }
        #: Set to make a report claim more rows than it will hand over.
        self.overstate = 0

    def post_form(self, url, form_body, *, headers=None, **kwargs):
        assert url.endswith("/results"), url
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        self.posts.append((url, dict(form_body)))

        if "holding-summaries" in url:
            status = form_body.get("holdingStatus")
            # The live platform answers an unrecognised status with zero rows
            # under the `holdings` key — not with the whole index.
            if status not in self._reports:
                return {"datasets": {"holdings": {"rows": [], "totalCount": 0}}}
            key, body = self._reports[status]
        else:
            key, body = self._reports["project-summaries"]

        rows = body["datasets"][key]["rows"]
        total = body["datasets"][key]["totalCount"] + self.overstate
        offset = int(form_body.get("offset", 0))
        limit = min(int(form_body.get("max", self.CLAMP)), self.CLAMP)
        page = rows[offset : offset + limit]
        return {"datasets": {key: {"rows": page, "totalCount": total}}}

    def get_json(self, url, *, headers=None, **kwargs):
        self.gets.append(url)
        key = url.rsplit("/", 1)[1]
        if key not in self._details:
            raise AssertionError(f"no detail fixture for {key}")
        return self._details[key]

    def close(self):
        pass


@pytest.fixture()
def client():
    return FakeClient()


@pytest.fixture()
def adapter(client):
    return acr.ACRAPI(client)


@pytest.fixture()
def projects(adapter):
    return [row for row, _raw in adapter.iter_projects()]


@pytest.fixture()
def by_reference(projects):
    return {row["external_id"]: row for row in projects}


# -- the contract pipeline drives -----------------------------------------


def test_it_satisfies_the_adapter_protocol(adapter):
    assert isinstance(adapter, base.RegistryAdapter)


def test_progress_is_cumulative(adapter, progress):
    list(adapter.iter_projects(progress=progress))
    progress.assert_cumulative(PROJECTS)


def test_credit_progress_is_cumulative_across_pages(adapter):
    """The clamp makes every ledger multi-page; the bar must not restart."""
    progress = RecordingProgress()
    list(adapter.iter_credits(acr.RETIREMENTS, progress=progress))
    progress.assert_cumulative(COUNTS[acr.RETIREMENTS])


def test_registry_resolves_by_alias():
    for name in ("acr", "ACR", "american-carbon-registry", "greentrace"):
        assert registries.resolve(name) == REGISTRY
    assert registries.adapter_class(REGISTRY) is acr.ACRAPI


def test_every_project_row_uses_shared_column_names(projects):
    for row in projects:
        assert set(row) <= set(db.PROJECT_FIELDS)


def test_every_credit_row_uses_shared_column_names(adapter):
    for resource in acr.LEDGERS:
        for row in adapter.iter_credits(resource):
            assert set(row) <= set(db.CREDIT_EVENT_FIELDS)


# -- the platform: form posts, paging, the clamp ---------------------------


def test_the_criteria_go_in_a_form_body(adapter, client):
    """A query string and a JSON body both return the generic HTTP 500."""
    adapter.project_total()
    url, form = client.posts[0]
    assert url.endswith("/project-summaries/results")
    assert "?" not in url
    assert set(form) >= {"offset", "pageNumber", "max"}


def test_paging_advances_on_offset_not_on_the_row_count(adapter, client):
    """`max` is clamped in silence, so a full page and a clamped one look alike."""
    rows = list(adapter.iter_credits(acr.RETIREMENTS))
    assert len(rows) == COUNTS[acr.RETIREMENTS]
    offsets = [form["offset"] for _url, form in client.posts]
    assert offsets == list(range(0, COUNTS[acr.RETIREMENTS], FakeClient.CLAMP))


def test_the_page_number_describes_the_same_window_as_the_offset(adapter, client):
    """Both are sent, and which one the service reads is not published.

    So they must agree. Deriving the page number from the size we *asked* for
    does not: `max` is clamped in silence, so a requested 20000 against a
    delivered 2000 walks `offset` 0, 2000, 4000 while `pageNumber` stays at 1
    for the whole ledger. The fixture client clamps to 2 for the same reason
    the live one clamps to 2000.
    """
    list(adapter.iter_credits(acr.RETIREMENTS))
    forms = [f for _url, f in client.posts if "holdingStatus" in f]
    assert len(forms) > 1
    for form in forms:
        assert form["pageNumber"] == form["offset"] // FakeClient.CLAMP + 1
    assert [f["pageNumber"] for f in forms] == list(range(1, len(forms) + 1))


def test_a_page_that_breaks_the_stride_is_reported(adapter, client, caplog):
    """After a short page the two fields cannot describe one window any more.

    `offset` stays right — it is what this pager advances on — but if the
    platform reads `pageNumber` instead, this is where rows start being skipped
    or read twice with nothing raised.
    """
    client.overstate = 5
    with caplog.at_level("ERROR"):
        list(adapter.iter_credits(acr.CANCELLATIONS))
    assert "same window" in caplog.text


def test_every_row_across_every_page_is_distinct(adapter):
    for resource, expected in COUNTS.items():
        rows = list(adapter.iter_credits(resource))
        assert len({row["entity_id"] for row in rows}) == expected


def test_each_report_is_fetched_once_per_run(adapter, client):
    """A run asks `count` and then iterates; that is not a reason to re-fetch."""
    adapter.count(acr.CANCELLATIONS)
    list(adapter.iter_credits(acr.CANCELLATIONS))
    list(adapter.iter_credits(acr.CANCELLATIONS))
    first_pages = [f for _u, f in client.posts if f["offset"] == 0]
    assert len(first_pages) == 1


def test_each_project_detail_is_fetched_once(adapter, client):
    list(adapter.iter_projects())
    list(adapter.iter_projects())
    assert len(client.gets) == len(set(client.gets)) == PROJECTS


def test_a_short_read_is_reported(adapter, client, caplog):
    """Never trust a row count because nothing was raised."""
    client.overstate = 5
    with caplog.at_level("ERROR"):
        list(adapter.iter_credits(acr.CANCELLATIONS))
    assert "INCOMPLETE" in caplog.text


def test_an_unknown_ledger_is_refused(adapter):
    for call in (lambda: adapter.count("holdings"), lambda: list(adapter.iter_credits("holdings"))):
        with pytest.raises(ValueError):
            call()


def test_a_dataset_the_criteria_did_not_select_raises(adapter, client):
    """One URL serves four ledgers and the key changes with the filter.

    Reading a fixed key would yield silence the day a filter value changes, so
    a response that does not carry the expected key is an error rather than an
    empty page.
    """
    with pytest.raises(ValueError, match="another dataset"):
        adapter.fetch(acr.CREDITS_REPORT, "issuedCredits", {"holdingStatus": "BOGUS"})


def test_every_credit_call_states_a_status(adapter, client):
    """The unfiltered view is the whole book, and it double-counts two ledgers.

    Its RETIRED and CANCELED rows *are* the retirement and cancellation
    ledgers — 16,385 records summing to exactly the issued total. Asking
    without a status is how that gets scraped by accident.
    """
    for resource in acr.LEDGERS:
        list(adapter.iter_credits(resource))
    credit_posts = [f for u, f in client.posts if "holding-summaries" in u]
    assert credit_posts
    assert all(f.get("holdingStatus") for f in credit_posts)


def test_the_ledger_statuses_are_the_ones_the_platform_publishes():
    """Read from the report's own `/criteria`, not guessed.

    An unknown value narrows to zero rows rather than returning everything, so
    a typo here loses a whole ledger quietly.
    """
    assert {status for status, _ in acr.LEDGER_CRITERIA.values()} == {
        "ISSUED",
        "RETIRED",
        "CANCELED",
    }


# -- politeness: this registry bans rather than throttles ------------------


def test_acr_asks_for_a_slower_client_than_the_default():
    """~100 requests in ten minutes earns a 429 with `Retry-After: 3600`."""
    assert acr.ACRAPI.requests_per_second == settings.ACR_REQUESTS_PER_SECOND
    assert settings.ACR_REQUESTS_PER_SECOND < settings.REQUESTS_PER_SECOND


def test_an_adapter_cannot_use_its_rate_to_go_faster(monkeypatch):
    """The per-registry rate is a floor on politeness, never a lever on it."""
    from carbon_scraper.http_client import RegistryClient

    monkeypatch.setattr(settings, "REQUESTS_PER_SECOND", 1.0)
    fast = RegistryClient(requests_per_second=25.0)
    try:
        assert fast._limiter._min_interval >= 1.0
    finally:
        fast.close()


# -- identity and keys -----------------------------------------------------


def test_the_numeric_part_of_the_reference_is_the_key(by_reference):
    assert by_reference["ACR0885"]["project_id"] == 885
    assert by_reference["ACR1215"]["project_id"] == 1215


def test_project_ids_are_unique(projects):
    assert len({row["project_id"] for row in projects}) == PROJECTS


def test_a_reference_that_is_not_acr_shaped_is_dropped_loudly(adapter, client, caplog):
    """This registry has never published one; inventing a key would hide the day it does."""
    body = client._reports["project-summaries"][1]
    client._reports["project-summaries"] = (
        "projects",
        {
            "datasets": {
                "projects": {
                    "rows": [{**body["datasets"]["projects"]["rows"][0], "projectId": "TMP-9"}],
                    "totalCount": 1,
                }
            }
        },
    )
    with caplog.at_level("ERROR"):
        rows = [row for row, _raw in adapter.iter_projects()]
    assert rows == []
    assert "no usable reference" in caplog.text


def test_a_stated_holding_key_hashes_to_what_it_always_hashed_to():
    """The guard below must not renumber a database that already exists."""
    row = acr.normalize_credit(acr.ISSUANCES, {"holdingKey": "H-1"}, 7)
    assert row["entity_id"] == hashed_id(REGISTRY, acr.ISSUANCES, "H-1")


def test_rows_with_no_holding_key_do_not_collapse_into_one(caplog):
    """`hashed_id(REGISTRY, resource, None)` is ONE key for all of them.

    Every unkeyed row would upsert over the last, and nothing would say so:
    `reconciled()` counts rows yielded, not rows written, so the run reconciles
    and the sheet is simply short. The fallback is the Markit scheme — the
    row's own values plus its position — and the log line is what says it was
    needed.
    """
    records = [
        {
            "holdingKey": None,
            "projectReferenceId": "ACR0885",
            "holdingSerialNumber": "A",
            "holdingQuantity": 10,
        },
        {
            "holdingKey": "",
            "projectReferenceId": "ACR0885",
            "holdingSerialNumber": "B",
            "holdingQuantity": 20,
        },
    ]
    with caplog.at_level("ERROR"):
        rows = [
            acr.normalize_credit(acr.ISSUANCES, record, index)
            for index, record in enumerate(records)
        ]
    assert rows[0]["entity_id"] != rows[1]["entity_id"]
    assert rows[0]["entity_id"] != hashed_id(REGISTRY, acr.ISSUANCES, None)
    assert "no holdingKey" in caplog.text
    # Idempotent: the same row at the same position keys the same way, so a
    # re-run repairs rather than duplicating.
    assert acr.normalize_credit(acr.ISSUANCES, records[0], 0)["entity_id"] == (
        rows[0]["entity_id"]
    )


def test_the_link_is_built_from_the_project_key_not_the_reference(by_reference):
    """`…/project/ACR1275` answers HTTP 200 with a shell that renders an error.

    The API behind it 404s, so a link built from the published reference looks
    right in the sheet and is broken when a business user clicks it.
    """
    row = by_reference["ACR0885"]
    key = row["extra"]["project_key"]
    assert key.startswith("P")
    assert row["detail_url"].endswith(f"/project/{key}")
    assert str(row["project_id"]) not in row["detail_url"].rsplit("/", 1)[1]


def test_every_row_carries_its_own_link(projects):
    """The settings fallback formats `project_id`, and the routes 404 on it."""
    assert all(row["detail_url"] for row in projects)


# -- the quantity trap -----------------------------------------------------


def test_the_issued_quantity_is_the_block_not_the_parent_event(adapter):
    """`issuanceQuantity` is the parent event's total, repeated on every block.

    3,358 live blocks belong to 2,337 events; summing the field per row reports
    604,312,772 units against a true 379,674,647.
    """
    rows = list(adapter.iter_credits(acr.ISSUANCES))
    assert sum(row["quantity"] for row in rows) == ISSUED_UNITS
    assert PARENT_FIELD_UNITS > ISSUED_UNITS


def test_the_parent_total_is_not_stored_anywhere_a_sum_could_reach_it(adapter):
    rows = list(adapter.iter_credits(acr.ISSUANCES))
    assert all(row["quantity"] is not None for row in rows)
    assert not any(row["quantity"] == PARENT_FIELD_UNITS for row in rows)


# -- retirements and cancellations ----------------------------------------


def test_the_beneficiary_is_the_named_third_party_not_the_account(adapter):
    """`registryAccountName` is on every row and is often an intermediary.

    Filling `beneficiary` from it would make every retirement look like a
    third-party sale the moment `sold_equals_retired` is flipped. Same call as
    BioCarbon's `final_user` over `to_name`.
    """
    rows = list(adapter.iter_credits(acr.RETIREMENTS))
    named = [row for row in rows if row["beneficiary"]]
    assert len(named) == RETIREMENTS_WITH_A_BENEFICIARY
    assert len(rows) > len(named)


def test_a_cancellation_records_why(adapter):
    """Most of them are conversions to ARB, not credits ceasing to exist.

    1,166 of the live 1,358 leave for California's or Washington's compliance
    registry. The column is the only copy of that distinction.
    """
    rows = list(adapter.iter_credits(acr.CANCELLATIONS))
    reasons = {row["reason"] for row in rows}
    assert "Convert to ARB Offset Credits" in reasons


def test_a_credit_row_records_the_registrys_own_state(adapter):
    retired = list(adapter.iter_credits(acr.RETIREMENTS))
    assert {row["status"] for row in retired} == {"RETIRED"}


def test_market_eligibility_is_not_an_additional_certification(adapter):
    """CORSIA and the SDG labels are a claim, like Cercarbono's `elegible` list."""
    for resource in acr.LEDGERS:
        for row in adapter.iter_credits(resource):
            assert row["additional_certification"] is None


# -- what this registry publishes, and what it does not --------------------


def test_the_state_is_carried_through_in_all_three_forms(by_reference):
    """Uppercase names, ISO 3166-2 codes, and both at once in one value."""
    assert by_reference["ACR0885"]["state_province"] == "ARKANSAS"
    assert by_reference["ACR1187"]["state_province"] == "US-FL, US-SC, US-GA"
    assert by_reference["ACR1168"]["state_province"] == "Israel"


def test_no_country_name_is_published_and_none_is_invented(projects):
    """The list states an ISO code, the detail states the same code, and that is all.

    Continent derives from the code — the Gold Standard path — and País stays
    blank rather than being filled from a lookup nobody asked for.
    """
    assert all(row.get("country_name") is None for row in projects)
    assert all(row["country_code"] for row in projects)


def test_the_namibian_country_code_survives_the_placeholder_table():
    """`NA` is Namibia, and this registry states a country as a code only.

    Three adapters here list `na` as "not stated" and Puro carries an empty
    table on purpose for exactly this reason. ACR does need a table — its
    `projectListingStatus` really does say `N/A` — so the ambiguous entry is
    dropped for the country field alone, and running the full table over an
    ISO code would delete a Namibian project's only country value and take its
    Continent with it.
    """
    namibia = acr.normalize_project({"projectId": "ACR9999", "country": "NA"}, {})
    assert namibia["country_code"] == "NA"

    from_detail = acr.normalize_project(
        {"projectId": "ACR9998"}, {"projectSiteLocCountry": "na"}
    )
    assert from_detail["country_code"] == "na"

    # The unambiguous placeholder is still read as one, and the table still
    # applies in full to every field that is not a country code.
    blank = acr.normalize_project({"projectId": "ACR9997", "country": "N/A"}, {})
    assert blank["country_code"] is None
    assert acr._stated("NA") is None
    assert "na" in acr.NOT_STATED and "na" not in acr.NOT_STATED_CODE


def test_the_deliberate_blanks_stay_blank(projects):
    """Measured over the full 994-project index; see docs/field-mapping.md."""
    for row in projects:
        assert row.get("city") is None
        assert row.get("region_name") is None
        assert row.get("afolu_names") is None
        assert row.get("additional_certification") is None
        # `estimatedTotalCredits` is the number 0 on every project sampled.
        # Storing that would read as "none estimated" rather than "not stated";
        # excel computes the total from the yearly figure instead.
        assert row.get("exante_quantity") is None


def test_the_yearly_estimate_is_read_and_the_period_comes_from_the_detail(by_reference):
    row = by_reference["ACR0885"]
    assert row["avg_annual_vol_vcu"] == 40000
    assert row["credit_period_start"] and row["credit_period_end"]


def test_the_standard_name_is_asserted_and_the_programme_is_not_it(by_reference):
    """`creditingProgram` is the compliance programme, not the standard."""
    row = by_reference["ACR0885"]
    assert row["standard_name"] == settings.ACR_STANDARD_NAME
    assert row["extra"]["crediting_program"] in {
        "ACR",
        "California Air Resources Board",
        "Washington Department of Ecology",
    }


def test_the_registrys_own_double_registration_flags_are_kept(adapter):
    """Booleans with no name attached, so they cannot fill a certification column.

    They are still what a cross-registry check reads first, so they are stored
    where a query can reach them.
    """
    rows = [raw["detail"] for _row, raw in adapter.iter_projects()]
    assert all("hasAnotherCarbonProgram" in detail for detail in rows)


def test_a_stated_total_of_zero_is_kept_and_a_false_flag_is_not(projects):
    """`0 == False` in Python, and the extra filter used to drop both.

    A project that has issued credits and retired none states
    `projectHoldingsTotalRetiredQuantity: 0`. Dropping it makes that
    indistinguishable from a project the registry published no figure for,
    which defeats the reason the stated totals are stored at all — being able
    to answer the day they disagree with the ledgers without re-scraping. The
    false flag is still dropped: `hasAnotherCarbonProgram` is false on 978 of
    994 projects and its absence says the same thing.
    """
    row = acr.normalize_project(
        {"projectId": "ACR9999", "projectName": "Zero"},
        {
            "projectHoldingsTotalRetiredQuantity": 0,
            "projectSiteLocLatitude": 0.0,
            "hasAnotherCarbonProgram": False,
            "hasAnotherEnvironmentalMarket": True,
        },
    )
    assert row["extra"]["stated_retired_total"] == 0.0
    assert row["extra"]["latitude"] == 0.0
    assert "has_another_carbon_program" not in row["extra"]
    assert row["extra"]["has_another_environmental_market"] is True


def test_no_real_project_loses_a_value_to_the_extra_filter(projects):
    """The fixtures' own rows, so the fix is checked against captured data too."""
    assert any(row["extra"] for row in projects)
    assert all(
        "" not in (row["extra"] or {}).values() for row in projects
    )


def test_tipo_macro_carries_the_registrys_own_vocabulary(projects):
    """Untranslated, like every registry here. Never mapped into a shared taxonomy."""
    assert {row["sectoral_scope"] for row in projects} >= {
        "Forest Carbon",
        "Refrigerants",
        "Ozone Depleting Substances",
    }


def test_greentrace_is_a_platform_and_art_is_not_ingested():
    """One tenant has an adapter; the other is named so nobody re-derives it.

    ART is the same API with one path segment changed, and it is a different
    registry — its own row set and its own decision, not ACR's rows.
    """
    assert settings.ART_REGISTRY_KEY != settings.ACR_REGISTRY_KEY
    assert acr.ACRAPI.registry_key == settings.ACR_REGISTRY_KEY
    assert all(
        cls.registry_key == settings.ACR_REGISTRY_KEY
        for cls in registries.adapter_classes(REGISTRY)
    )


def test_the_project_report_is_not_fetched_with_a_credit_filter(adapter, client):
    """Two reports, two shapes; the projects one takes no holdingStatus."""
    list(adapter.iter_projects())
    project_posts = [f for u, f in client.posts if "project-summaries" in u]
    assert project_posts
    assert not any("holdingStatus" in f for f in project_posts)


def test_fixtures_are_not_mutated_between_tests(client):
    """The pager slices fixture rows; a test that edited them would leak."""
    before = copy.deepcopy(client._reports["ISSUED"][1])
    adapter = acr.ACRAPI(client)
    list(adapter.iter_credits(acr.ISSUANCES))
    assert client._reports["ISSUED"][1] == before


# -- derivation ------------------------------------------------------------


def test_the_forest_protocols_separate_a_single_project_type():
    """352 projects read "Forest Carbon"; only the protocol name tells them apart.

    Improved management, the compliance protocols and reforestation are three
    different answers behind one `projectType`, and reading the type alone
    would file all of them the same way.
    """
    rulesets = derive.load_rulesets()

    def micro(methodology: str) -> str | None:
        row = {"sectoral_scope": "Forest Carbon", "methodologies": methodology}
        values = dict((c, v) for c, v, _rule in derive.derive_for_project(row, rulesets))
        return values.get("Tipo Micro de Projeto")

    assert micro("Improved Forest Management (IFM) on Non-Federal U.S. Forestlands") == (
        "Manejo Florestal Melhorado (IFM)"
    )
    assert micro("Afforestation and Reforestation of Degraded Lands") == (
        "Florestamento, Reflorestamento e Revegetação (ARR)"
    )
    # The compliance protocols cover reforestation, improved management AND
    # avoided conversion under one name, and nothing finer is published.
    assert micro("ARB Compliance Offset Protocol: U.S. Forest Projects") == (
        "AFOLU (subtipo não informado)"
    )


def test_the_foam_blowing_agents_are_fluorinated_gas_not_ods():
    """All 82 are "Industrial Process Emissions" in the registry's own vocabulary.

    What they avoid is a high-GWP blowing agent, not an ozone-depleting bank.
    Filing them with the ODS destruction protocols contradicted both.
    """
    rulesets = derive.load_rulesets()
    row = {
        "sectoral_scope": "Industrial Process Emissions",
        "methodologies": (
            "Transition to Advanced Formulation Blowing Agents in Foam "
            "Manufacturing and Use"
        ),
    }
    values = dict((c, v) for c, v, _rule in derive.derive_for_project(row, rulesets))
    assert values["Tipo Micro de Projeto"] == "Refrigerantes e Gases Fluorados (HFC)"


def test_every_project_type_the_registry_publishes_reaches_a_durability():
    """A wording this file does not recognise leaves a blank and logs nothing.

    The 17 values are the registry's own closed list, measured over the full
    994-project index on 2026-08-04.
    """
    rulesets = derive.load_rulesets()
    for scope in (
        "Forest Carbon",
        "Ozone Depleting Substances",
        "Refrigerants",
        "Coal Mine Methane",
        "Industrial Process Emissions",
        "Transport / Fleet Efficiency",
        "Orphaned Wells",
        "Landfill Gas Capture & Combustion",
        "Livestock Waste Management",
        "Agricultural Land Management",
        "Renewable Energy",
        "Carbon Capture & Storage (CCS)",
        "Wetland Restoration",
        "Fuel Switching",
        "Energy Efficiency",
        "Wastewater Treatment",
        "Industrial Gas Substitution",
    ):
        values = dict(
            (c, v)
            for c, v, _rule in derive.derive_for_project({"sectoral_scope": scope}, rulesets)
        )
        assert values.get("Durabilidade"), scope


def test_the_forests_reach_a_biome_through_the_iso_code(projects):
    """No country name is published, so the name-keyed bands all miss.

    The ISO-code band is last in `biome.yaml` for that reason — measured blast
    radius on the seven existing registries: 4 rows added, 0 changed.
    """
    rulesets = derive.load_rulesets()
    forest = next(p for p in projects if p["sectoral_scope"] == "Forest Carbon")
    values = dict(
        (c, v) for c, v, _rule in derive.derive_for_project(dict(forest), rulesets)
    )
    assert values["Bioma"] == "Floresta Temperada Norte-Americana"
    assert values["Continent"]


def test_a_country_name_still_beats_the_code_band():
    """The code band must never shadow a more specific rule for another registry."""
    biome = next(r for r in derive.load_rulesets() if r.column == "Bioma")
    value = biome.evaluate(
        {
            "sectoral_scope": "Agriculture Forestry and Other Land Use",
            "country_name": "Brazil",
            "state_province": "Amazonas",
            "country_code": "US",
        }
    )
    assert value is not None
    assert value[0] == "Amazônia"


# -- the front end's own list of registries --------------------------------


def test_the_help_string_names_every_registry_and_each_one_resolves():
    """It said "verra | gs | cercarbono | planvivo | all" long after there were
    seven, so `--help` told a user a registry did not exist. Built from the
    dispatch table now, which is only useful if every name it prints works."""
    from carbon_scraper import cli

    choices = cli._registry_choices()
    assert len(choices) == len(registries.ALL)
    assert "acr" in choices
    for name in choices:
        assert registries.resolve(name)


# -- the platform declining to answer --------------------------------------


def test_a_401_is_reported_as_a_block_not_a_missing_credential(adapter, client):
    """After a sustained read, every route answers `401 "Invalid API Key"`.

    Measured 2026-08-04, several hundred requests into a sync: the report
    endpoints that had just served hundreds of pages began refusing, while the
    site's own page stayed byte-identical and still carried no key anywhere.
    Reading that as "we need credentials" sends the next person hunting for a
    key that does not exist.
    """
    def refuse(url, form_body, *, headers=None, **kwargs):
        response = httpx.Response(
            401,
            json={"message": "Invalid API Key"},
            request=httpx.Request("POST", url),
        )
        raise httpx.HTTPStatusError("401", request=response.request, response=response)

    client.post_form = refuse
    with pytest.raises(greentrace.GreenTraceBlocked, match="block, not a missing"):
        adapter.project_total()


def test_an_ordinary_http_error_is_not_dressed_up_as_a_block(adapter, client):
    """A 404 is a wrong path, and saying "wait and re-run" about one is a lie."""
    def not_found(url, form_body, *, headers=None, **kwargs):
        response = httpx.Response(404, request=httpx.Request("POST", url))
        raise httpx.HTTPStatusError("404", request=response.request, response=response)

    client.post_form = not_found
    with pytest.raises(httpx.HTTPStatusError):
        adapter.project_total()


def test_a_429_is_reported_as_a_block_too(adapter, client):
    """The rate-limit refusal never arrives as an `HTTPStatusError`.

    `http_client` raises `RetryableStatus` for a 429 before the response can
    reach `raise_for_status()`, so catching only `HTTPStatusError` left the
    429 arm of `_blocked` unreachable — and this is the refusal ACR actually
    documents, at roughly 100 requests in ten minutes with
    `Retry-After: 3600`. It surfaced as a bare `RetryableStatus: 429 from …`,
    which in the window is a traceback file rather than the message saying the
    run resumes from the cache.
    """
    def rate_limited(url, form_body, *, headers=None, **kwargs):
        raise RetryableStatus(f"429 from {url}", 3600.0, 429)

    client.post_form = rate_limited
    with pytest.raises(greentrace.GreenTraceBlocked, match="block, not a missing"):
        adapter.project_total()


def test_a_retryable_500_is_not_dressed_up_as_a_block(adapter, client):
    """The platform's generic fault is an unmapped path or a bad body.

    Both answer HTTP 500, both reach here as `RetryableStatus`, and neither
    clears by waiting — so telling the operator to re-run later would send
    them away from the actual cause.
    """
    def faulted(url, form_body, *, headers=None, **kwargs):
        raise RetryableStatus(f"500 from {url}", None, 500)

    client.post_form = faulted
    with pytest.raises(RetryableStatus):
        adapter.project_total()


def test_the_projects_verra_also_publishes_are_cross_linked():
    """Two mines are registered in both registries, for consecutive periods.

    Verra issued 234,591 and 610,404 units for the earlier periods and ACR
    221,141 and 1,185,666 for the later ones, so the tranches are disjoint and
    both rows ship — the same shape as Cercarbono's `CDC-106`/`CDC-107` against
    BioCarbon. The link is measured from the names, because ACR's own
    `hasAnotherCarbonProgram` flag never says which programme.
    """
    assert acr.ALSO_REGISTERED_AS == {
        "ACR0242": "VERRA 559",
        "ACR0388": "VERRA 573",
        "ACR1192": "VERRA 573",
    }
    row = acr.normalize_project({"projectId": "ACR0242", "projectKey": "P1"}, {})
    assert row["extra"]["also_registered_as"] == "VERRA 559"


def test_a_project_with_no_known_twin_carries_no_link(by_reference):
    for row in by_reference.values():
        assert (row["extra"] or {}).get("also_registered_as") is None
