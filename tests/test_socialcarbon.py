"""SocialCarbon adapter: keying, paging, ledger scope and credit accounting. No network.

Runs against `tests/fixtures/socialcarbon-*.json`, captured from the live
Bubble Data API on 2026-08-04. Two of the fixtures earn their keep by being
awkward rather than representative:

* `socialcarbon-projects.json` contains the **two different projects that both
  publish `SOCIALCARBON-19`** — a peatland programme in Poland and a forest
  project in Brazil. Any keying that reads the human reference collapses them.
* the retirements are split into `-page1` (50 rows, `remaining: 31`) and
  `-page2` (31 rows, `remaining: 0`), so the pager is exercised even though
  the live registry answers every type in one page today.
"""

from __future__ import annotations

import pytest

from carbon_scraper import db, derive, excel, settings
from carbon_scraper import registries
from carbon_scraper.registries import base
from carbon_scraper.registries.socialcarbon import api as sc

from conftest import RecordingProgress, load_fixture as _load

REGISTRY = settings.SOCIAL_CARBON

# What the registry itself reported on 2026-08-04.
PROJECTS = 19
COUNTS = {sc.ISSUANCES: 17, sc.RETIREMENTS: 81, sc.CANCELLATIONS: 2}
ISSUED_UNITS = 189794
RETIRED_UNITS = 67991
CANCELLED_UNITS = 20145

# The reference two different projects share, and one that is simply absent.
DUPLICATED_REFERENCE = "SOCIALCARBON-19"
MISSING_REFERENCE = "SOCIALCARBON-15"


class FakeClient:
    """Serves the Bubble types from fixtures, honouring `cursor`.

    Records every (type, cursor) asked for, so a test can prove the pager
    stopped where it should rather than only that it returned the right rows.
    """

    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.invalidated: list[str] = []
        self._pages = {
            "project": [_load("socialcarbon-projects.json")],
            "issuance": [_load("socialcarbon-issuances.json")],
            "cancellations": [_load("socialcarbon-cancellations.json")],
            "retirement": [
                _load("socialcarbon-retirements-page1.json"),
                _load("socialcarbon-retirements-page2.json"),
            ],
        }

    def get_json(self, url, *, params=None, headers=None, **kwargs):
        bubble_type = url.rsplit("/", 1)[1]
        cursor = int((params or {}).get("cursor", 0))
        self.calls.append((bubble_type, cursor))
        for page in self._pages[bubble_type]:
            envelope = page.get("response")
            # Bubble answers a bad type with a plain error object and no
            # envelope at all. Hand it back as it came.
            if not isinstance(envelope, dict):
                return page
            if int(envelope["cursor"]) == cursor:
                return page
        raise AssertionError(f"no fixture for {bubble_type} at cursor {cursor}")

    def truncate(self, bubble_type: str, promised: int) -> None:
        """Make a type claim more records than it will ever hand over.

        The registry stating a total it does not deliver is the failure
        reconciliation exists for, and there is no other way to provoke it
        here: every fixture is internally consistent.
        """
        pages = self._pages[bubble_type]
        pages[-1]["response"]["remaining"] = promised
        end = sum(len(p["response"]["results"]) for p in pages)
        pages.append(
            {
                "response": {
                    "cursor": end,
                    "results": [],
                    "count": 0,
                    "remaining": promised,
                }
            }
        )

    def invalidate(self, method, url, **kwargs):
        self.invalidated.append(url)

    def close(self):
        pass


@pytest.fixture()
def client():
    return FakeClient()


@pytest.fixture()
def adapter(client):
    return sc.SocialCarbonAPI(client)


@pytest.fixture()
def projects(adapter):
    return [row for row, _raw in adapter.iter_projects()]


# -- the contract pipeline drives -----------------------------------------


def test_it_satisfies_the_adapter_protocol(adapter):
    assert isinstance(adapter, base.RegistryAdapter)


def test_progress_is_cumulative(adapter, progress):
    list(adapter.iter_projects(progress=progress))
    progress.assert_cumulative(PROJECTS)


def test_credit_progress_is_cumulative_across_pages(adapter):
    """The 81 retirements arrive in two pages; the bar must not restart."""
    progress = RecordingProgress()
    list(adapter.iter_credits(sc.RETIREMENTS, progress=progress))
    progress.assert_cumulative(COUNTS[sc.RETIREMENTS])


def test_registry_resolves_by_alias():
    for name in ("sc", "socialcarbon", "social-carbon", "SOCIAL_CARBON"):
        assert registries.resolve(name) == REGISTRY
    assert registries.adapter_class(REGISTRY) is sc.SocialCarbonAPI


# -- counts and paging -----------------------------------------------------


def test_totals_come_from_the_registrys_own_count_plus_remaining(adapter):
    assert adapter.project_total() == PROJECTS
    for resource, expected in COUNTS.items():
        assert adapter.count(resource) == expected


def test_paging_follows_remaining_rather_than_the_page_size(adapter, client):
    """`limit` clamps to 100 in silence, so the page size proves nothing.

    Page one holds 50 rows and promises 31 more. A pager that stopped because
    it got fewer rows than it asked for would lose those 31; one that read
    `remaining` asks for cursor 50 and stops there.
    """
    rows = list(adapter.iter_credits(sc.RETIREMENTS))
    assert len(rows) == COUNTS[sc.RETIREMENTS]
    assert ("retirement", 0) in client.calls
    assert ("retirement", 50) in client.calls
    # And it stops: no third request past the end, which is where the Markit
    # view answers HTTP 500.
    assert [c for c in client.calls if c[0] == "retirement"] == [
        ("retirement", 0),
        ("retirement", 50),
    ]


def test_every_row_across_both_pages_is_distinct(adapter):
    rows = list(adapter.iter_credits(sc.RETIREMENTS))
    assert len({row["entity_id"] for row in rows}) == COUNTS[sc.RETIREMENTS]


def test_each_type_is_fetched_once_per_run(adapter, client):
    """A run asks `count` and then iterates; that is not a reason to re-fetch."""
    adapter.count(sc.ISSUANCES)
    list(adapter.iter_credits(sc.ISSUANCES))
    assert client.calls.count(("issuance", 0)) == 1


def test_an_unknown_ledger_is_refused(adapter):
    with pytest.raises(ValueError):
        adapter.count("assets")
    with pytest.raises(ValueError):
        list(adapter.iter_credits("transfers"))


# -- the duplicated reference ---------------------------------------------


def test_two_projects_share_one_published_reference(projects):
    """The registry's own collision, kept visible rather than repaired."""
    sharing = [p for p in projects if p["external_id"] == DUPLICATED_REFERENCE]
    assert len(sharing) == 2
    assert {p["country_name"] for p in sharing} == {"Poland", "Brazil"}
    assert MISSING_REFERENCE not in {p["external_id"] for p in projects}


def test_the_duplicated_reference_still_yields_two_rows(projects):
    """Keying on `Project ID` would merge Poland and Brazil into one project.

    That is the Markit merge in reverse: there a repeated id was one project
    and rows had to be joined, here it is two projects and they must not be.
    """
    assert len(projects) == PROJECTS
    assert len({p["project_id"] for p in projects}) == PROJECTS
    sharing = [p for p in projects if p["external_id"] == DUPLICATED_REFERENCE]
    assert sharing[0]["project_id"] != sharing[1]["project_id"]


def test_the_key_is_hashed_from_bubbles_record_id(projects):
    """Stable across runs, and readable back out of `extra`.

    `credit_events.project_id` is an integer and Bubble's id is a string, so
    something has to bridge them. Hashing rather than counting is what keeps
    the upsert idempotent when the registry inserts a project.
    """
    for row in projects:
        bubble_id = row["extra"]["bubble_id"]
        assert row["project_id"] == sc.project_key(bubble_id)
        assert row["project_id"] == sc.hashed_id(REGISTRY, bubble_id)


def test_keys_do_not_collide_with_another_registrys_numeric_ids(projects):
    """The registry name is part of the seed for exactly this reason."""
    for row in projects:
        assert row["project_id"] != sc.hashed_id(row["extra"]["bubble_id"])


# -- ledger scope: what must NOT be scraped -------------------------------


def test_asset_is_not_a_ledger():
    """`asset` mirrors `issuance` and carries another registry's credits.

    Its 22 rows are the 17 issuances again — summing to the same 189,794 — plus
    5 rows reading `Standard: VCS` with no project link, which are Verra
    credits deposited into this platform. Scraping it would double every
    issued figure and import Verra's credits under SocialCarbon's name.
    """
    assert "asset" not in sc.LEDGER_TYPES.values()
    assert set(sc.LEDGERS) == set(COUNTS)


def test_account_data_is_not_scraped():
    """`meta` also offers `user`, `billing` and friends. Nothing asks for them."""
    for private in ("user", "billing", "accountmanager", "organisationdetails"):
        assert private not in sc.LEDGER_TYPES.values()


def test_the_ledgers_are_not_called_credits():
    """`db.credit_totals()` branches on that exact string.

    `credits` selects Gold Standard's bucket-by-status semantics. These rows
    carry no such status — each Bubble type already is a bucket — so any other
    name is what they want.
    """
    assert "credits" not in sc.LEDGERS


# -- normalisation ---------------------------------------------------------


def test_projects_normalise_onto_the_shared_columns(projects):
    row = next(p for p in projects if p["external_id"] == "SOCIALCARBON-1")
    assert set(row) <= set(db.PROJECT_FIELDS)
    assert row["project_name"].startswith("Spekboom")
    assert row["country_name"] == "South Africa"
    assert row["standard_name"] == "SOCIALCARBON"
    assert row["status"] == "Listed"
    assert row["methodologies"] == "SCM0004"
    assert row["sectoral_scope"] == "Agriculture Forestry and Other Land Use"
    assert row["proponents"] == "Spekboom Net Zero (Pty) Ltd"
    assert row["area"] == 7311
    assert row["credit_period_start"].startswith("2023-01-31")


def test_tipo_macro_is_carried_through_untranslated(projects):
    """Each registry's own vocabulary, never mapped into a shared taxonomy."""
    assert {p["sectoral_scope"] for p in projects} == {
        "Agriculture Forestry and Other Land Use",
        "AFOLU",
        "Harmful Algae Bloom Treatment",
    }


def test_placeholders_are_treated_as_blank(projects):
    """`TBC`/`TBD` sit in validator and verifier before validation happens."""
    values = {p["extra"].get("validator") for p in projects}
    assert "TBC" not in values and "TBD" not in values


def test_deliberate_blanks_stay_blank(projects):
    """Measured over the full index. Never filled from somewhere else.

    `Address` is a free-text string plus a lat/lng pair; reading a state out
    of "XG3P+H8, South Africa" would be inventing one.
    """
    for row in projects:
        assert row.get("state_province") is None
        assert row.get("city") is None
        assert row.get("country_code") is None
        assert row.get("additional_certification") is None
        assert row.get("region_name") is None
        assert row.get("exante_quantity") is None


def test_yearly_ex_ante_is_read_where_published(projects):
    filled = [p for p in projects if p["avg_annual_vol_vcu"] is not None]
    assert len(filled) == PROJECTS - 1


def test_every_project_carries_its_own_detail_url(projects):
    """Load-bearing: the fallback in settings cannot work for this registry.

    `PROJECT_DETAIL_URLS` formats `project_id`, and ours is a hash of Bubble's
    record id, so a row that reached the sheet without a `detail_url` would
    get a link that does not resolve.
    """
    for row in projects:
        assert row["detail_url"] == (
            f"{settings.SOCIALCARBON_SITE}/project_details/"
            f"{row['extra']['bubble_id']}"
        )


# -- credit rows -----------------------------------------------------------


def test_credit_rows_normalise_onto_the_shared_columns(adapter):
    for resource in sc.LEDGERS:
        for row in adapter.iter_credits(resource):
            assert set(row) == set(db.CREDIT_EVENT_FIELDS)


def test_every_ledger_row_links_to_a_project(adapter, projects):
    """No orphans and no foreign rows — measured, not assumed."""
    keys = {p["project_id"] for p in projects}
    for resource in sc.LEDGERS:
        for row in adapter.iter_credits(resource):
            assert row["project_id"] in keys


def test_credit_keys_are_stable_across_runs(adapter):
    """Every write is an idempotent upsert, which needs a key that does not move."""
    first = [r["entity_id"] for r in adapter.iter_credits(sc.RETIREMENTS)]
    second = [r["entity_id"] for r in adapter.iter_credits(sc.RETIREMENTS)]
    assert first == second


def test_an_issuance_and_a_retirement_cannot_share_a_key(adapter):
    issued = {r["entity_id"] for r in adapter.iter_credits(sc.ISSUANCES)}
    retired = {r["entity_id"] for r in adapter.iter_credits(sc.RETIREMENTS)}
    assert not issued & retired


def test_quantities_are_read_from_each_ledgers_own_field(adapter):
    """Bubble spells it `Quantity requested` on one type and `Quantity` on two."""
    totals = {
        resource: sum(r["quantity"] or 0 for r in adapter.iter_credits(resource))
        for resource in sc.LEDGERS
    }
    assert totals == {
        sc.ISSUANCES: ISSUED_UNITS,
        sc.RETIREMENTS: RETIRED_UNITS,
        sc.CANCELLATIONS: CANCELLED_UNITS,
    }


def test_beneficiary_is_never_filled_from_the_retiring_account(adapter):
    """`Retiree` is who retired the units, not who they were retired for.

    It is filled on all 81 rows and `Beneficiary` on 20. Falling back to it
    would make every retirement look like a third-party sale the moment
    `sold_equals_retired` is flipped.
    """
    rows = list(adapter.iter_credits(sc.RETIREMENTS))
    named = [r for r in rows if r["beneficiary"]]
    assert len(named) == 20
    retirees = {
        r.get("Retiree")
        for r in _load("socialcarbon-retirements-page1.json")["response"]["results"]
    }
    assert not ({r["beneficiary"] for r in named} & retirees)


def test_the_note_is_kept_whole(adapter):
    """`credit_events` keeps no raw payload, so this column is the only copy.

    61 of 81 retirements state their beneficiary only as prose inside `Notes`.
    That prose is deliberately not parsed — but it has to survive, or deciding
    to parse it later would mean re-scraping.
    """
    rows = list(adapter.iter_credits(sc.RETIREMENTS))
    assert any(r["reason"] and r["reason"].startswith("Beneficiary:") for r in rows)


# -- issued totals ---------------------------------------------------------


def test_stated_totals_are_only_for_issuances(adapter):
    assert base.credit_totals_of(adapter, sc.RETIREMENTS) is not None
    assert list(adapter.iter_credit_totals(sc.RETIREMENTS)) == []


def test_stated_issued_totals_match_the_rows_today(adapter):
    """All 17 issuances are approved and complete, so the two agree exactly.

    The point of the seam is the day they do not.
    """
    stated = sum(q for _pid, q, _n in adapter.iter_credit_totals(sc.ISSUANCES))
    assert stated == ISSUED_UNITS


def test_a_pending_request_is_not_issued_credits(adapter, client):
    """An unapproved issuance is a request; the units do not exist yet."""
    page = client._pages["issuance"][0]
    row = page["response"]["results"][0]
    row["Approved"] = False
    stated = dict(
        (pid, q) for pid, q, _n in adapter.iter_credit_totals(sc.ISSUANCES)
    )
    assert sum(stated.values()) == ISSUED_UNITS - row["Quantity requested"]
    # The row is still stored — it is published, and dropping it would make
    # the ledger disagree with the registry.
    assert len(list(adapter.iter_credits(sc.ISSUANCES))) == COUNTS[sc.ISSUANCES]


def test_the_pending_flag_reaches_the_row(adapter, client):
    client._pages["issuance"][0]["response"]["results"][0]["Approved"] = False
    statuses = [r["status"] for r in adapter.iter_credits(sc.ISSUANCES)]
    assert statuses.count("Pending") == 1
    assert statuses.count("Issued") == COUNTS[sc.ISSUANCES] - 1


# -- reconciliation --------------------------------------------------------


def test_a_short_read_is_reported(adapter, client, caplog):
    """Never trust a row count just because nothing was raised."""
    client.truncate("cancellations", promised=5)
    with caplog.at_level("ERROR"):
        rows = list(adapter.iter_credits(sc.CANCELLATIONS))
    assert len(rows) == COUNTS[sc.CANCELLATIONS]
    assert adapter.count(sc.CANCELLATIONS) == COUNTS[sc.CANCELLATIONS] + 5
    assert "INCOMPLETE" in caplog.text


def test_an_empty_page_that_promises_more_does_not_spin(adapter, client, caplog):
    """`remaining` says there is more and the page delivers none."""
    page = client._pages["project"][0]["response"]
    page["results"] = []
    page["remaining"] = 12
    with caplog.at_level("ERROR"):
        assert adapter._fetch("project") == []
    assert "INCOMPLETE" in caplog.text


def test_a_response_without_an_envelope_is_refused(adapter, client):
    """Bubble reports a bad type as a JSON error object, not an envelope.

    A 4xx is not cached, but a malformed 200 would be — so the cached copy is
    dropped, or "fix it and re-run" replays the same broken answer.
    """
    client._pages["project"] = [{"statusCode": 404, "body": {"status": "NOT_FOUND"}}]
    with pytest.raises(ValueError):
        adapter._fetch("project")
    assert client.invalidated


# -- derivation ------------------------------------------------------------


def test_the_bare_afolu_wording_reaches_the_biome_rules():
    """One project says "AFOLU" and not "Land use (AFOLU)".

    `applies_when` gates the whole ruleset, so an unrecognised wording means
    no biome for that row and nothing in the log to say so.
    """
    rulesets = derive.load_rulesets()
    biome = next(r for r in rulesets if r.column == "Bioma")
    assert biome.evaluate({"sectoral_scope": "AFOLU", "country_name": "Brazil"})
    assert biome.evaluate(
        {
            "sectoral_scope": "Agriculture Forestry and Other Land Use",
            "country_name": "Brazil",
        }
    )


def test_a_non_land_use_project_gets_no_biome():
    """"Harmful Algae Bloom Treatment" is not land use and must stay blank."""
    rulesets = derive.load_rulesets()
    biome = next(r for r in rulesets if r.column == "Bioma")
    assert (
        biome.evaluate(
            {
                "sectoral_scope": "Harmful Algae Bloom Treatment",
                "country_name": "United States of America",
            }
        )
        is None
    )


def test_the_iso_inverted_congo_resolves_to_a_continent():
    """A third spelling of the DRC, and these lists are exact-match."""
    rulesets = derive.load_rulesets()
    values = {
        column: value
        for column, value, _rule in derive.derive_for_project(
            {"country_name": "Congo, Democratic Republic of the"}, rulesets
        )
    }
    assert values["Continent"] == "Africa"


def test_every_country_in_the_index_reaches_a_continent(projects):
    """No ISO code is published, so the name-based table is the only route."""
    rulesets = derive.load_rulesets()
    for row in projects:
        values = {
            column: value
            for column, value, _rule in derive.derive_for_project(dict(row), rulesets)
        }
        assert values.get("Continent"), row["country_name"]


# -- end to end ------------------------------------------------------------


def test_a_full_pass_lands_in_the_sheet(adapter, conn):
    """Projects, all three ledgers and the rules, through to the export."""
    db.upsert_projects(conn, REGISTRY, adapter.iter_projects())
    for resource in sc.LEDGERS:
        db.upsert_credit_events(
            conn, REGISTRY, resource, adapter.iter_credits(resource)
        )
    db.upsert_credit_totals(
        conn, REGISTRY, sc.ISSUANCES, adapter.iter_credit_totals(sc.ISSUANCES)
    )

    rulesets = derive.load_rulesets()
    rows = []
    for project in db.all_projects(conn, REGISTRY):
        record = dict(project)
        for column, value, rule in derive.derive_for_project(record, rulesets):
            rows.append((REGISTRY, record["project_id"], column, value, rule))
    db.replace_derived(conn, rows)

    _columns, sheet = excel.build_rows(conn, REGISTRY)
    assert len(sheet) == PROJECTS
    assert all(r["Registry"] == "SocialCarbon" for r in sheet)
    # Every row reachable from the sheet's own link column, and none of them
    # via the settings fallback, which cannot build a working URL here.
    assert all(r["Project URL"].startswith(settings.SOCIALCARBON_SITE) for r in sheet)

    # Both halves of the duplicated reference survive as separate rows.
    shared = [r for r in sheet if r["Project ID"] == DUPLICATED_REFERENCE]
    assert len(shared) == 2

    issued = sum(r["Total Credits Issued"] or 0 for r in sheet)
    retired = sum(r["Total Credits Retired"] or 0 for r in sheet)
    cancelled = sum(r["Total Credits Cancelled"] or 0 for r in sheet)
    assert (issued, retired, cancelled) == (
        ISSUED_UNITS,
        RETIRED_UNITS,
        CANCELLED_UNITS,
    )
