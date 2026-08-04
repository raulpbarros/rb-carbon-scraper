"""BioCarbon adapter: the 200-that-is-a-403, paging, ledger scope, cancellations. No network.

Runs against `tests/fixtures/biocarbon-*.json`, captured from the live Global
CarbonTrace API on 2026-08-04. Ten projects out of the live 105, chosen for
being awkward rather than representative:

* **24 and 31** are the two that Cercarbono also publishes, as `CDC-106` and
  `CDC-107`. Both registries ship a row (user's decision, 2026-08-04) and this
  is what pins the cross-link.
* **85** has 322,687 verified reductions and **no issuance blocks at all** —
  the reason `verified_reductions` is not read as an issued total.
* **9 and 35** are the only projects with cancellations, and they are also the
  ones whose issuance blocks carry `dropouts`. The two feeds disagree, which
  is the whole point of `iter_credit_totals`.
* **116** publishes a crediting period that ends 26 years before it starts.
  The registry's own data error, stored as published.
* **22** was migrated in from the CDM, which is the provenance a double-count
  check reads first.

The detail fixtures are trimmed of `files`, `verifications`, `objetives` and
the lifecycle log — several hundred KB per project that the adapter never
opens. Everything the adapter reads is as captured.
"""

from __future__ import annotations

import pytest

from carbon_scraper import db, derive, excel, settings
from carbon_scraper import registries
from carbon_scraper.registries import base
from carbon_scraper.registries.biocarbon import api as bc

from conftest import RecordingProgress, load_fixture as _load

REGISTRY = settings.BIOCARBON

# What the fixtures' own paginators report.
PROJECTS = 10
COUNTS = {bc.ISSUANCES: 144, bc.RETIREMENTS: 60, bc.CANCELLATIONS: 3}

# The two projects Cercarbono also publishes.
SHARED_WITH_CERCARBONO = {24: "CERCARBONO CDC-106", 31: "CERCARBONO CDC-107"}

# Verified but never issued: the trap `iter_credit_totals` deliberately does
# not paper over.
VERIFIED_BUT_UNISSUED = 85

# `dropouts` across the issuance blocks, against what the cancellations
# endpoint publishes for the same projects.
DROPOUT_UNITS = 477859.0
CANCELLATION_ROWS = 3


class FakeClient:
    """Serves the platform's routes from fixtures, honouring `page`.

    Records every URL and page asked for, so a test can prove the pager
    stopped where it should rather than only that it got the right rows.
    """

    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.invalidated: list[str] = []
        self._details = _load("biocarbon-details.json")
        self._cancellations = _load("biocarbon-cancellations.json")
        self._pages = {
            "projects": [
                _load("biocarbon-projects-page1.json"),
                _load("biocarbon-projects-page2.json"),
            ],
            "carbon-credits": [_load("biocarbon-carbon-credits.json")],
            "retreats": [
                _load("biocarbon-retreats-page1.json"),
                _load("biocarbon-retreats-page2.json"),
            ],
        }
        #: Set to make the next list response the refusal the live API returns
        #: without an `x-api-key` — HTTP 200 carrying `status: 403`.
        self.refuse = False

    def get_json(self, url, *, params=None, headers=None, **kwargs):
        path = url.split("/api/ghg/", 1)[1]
        page = int((params or {}).get("page", 1))
        self.calls.append((path, page))

        if self.refuse:
            return {
                "status": 403,
                "data": [],
                "message": "You do not have permissions to perform this action.",
            }
        if path.endswith("/cancellations"):
            initiative = path.split("/")[2]
            return self._cancellations[initiative]
        if path.startswith("projects/"):
            return self._details[path.split("/", 1)[1]]
        for body in self._pages[path]:
            if int(body["current_page"]) == page:
                return body
        raise AssertionError(f"no fixture for {path} page {page}")

    def truncate(self, resource: str, promised: int) -> None:
        """Make a resource claim more records than it will hand over.

        The registry stating a total it does not deliver is what
        reconciliation exists for, and there is no other way to provoke it:
        every fixture is internally consistent.
        """
        for body in self._pages[resource]:
            body["total"] = promised

    def invalidate(self, method, url, **kwargs):
        self.invalidated.append(url)

    def close(self):
        pass


@pytest.fixture()
def client():
    return FakeClient()


@pytest.fixture()
def adapter(client):
    return bc.BioCarbonAPI(client)


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
    """The retirements arrive in two pages; the bar must not restart."""
    progress = RecordingProgress()
    list(adapter.iter_credits(bc.RETIREMENTS, progress=progress))
    progress.assert_cumulative(COUNTS[bc.RETIREMENTS])


def test_registry_resolves_by_alias():
    for name in ("bc", "bcr", "biocarbon", "bio-carbon", "gct", "BIOCARBON"):
        assert registries.resolve(name) == REGISTRY
    assert registries.adapter_class(REGISTRY) is bc.BioCarbonAPI


def test_every_project_row_uses_shared_column_names(projects):
    for row in projects:
        assert set(row) <= set(db.PROJECT_FIELDS)


def test_every_credit_row_uses_shared_column_names(adapter):
    for resource in bc.LEDGERS:
        for row in adapter.iter_credits(resource):
            assert set(row) <= set(db.CREDIT_EVENT_FIELDS)


# -- the refusal that arrives at HTTP 200 ----------------------------------


def test_a_403_inside_a_200_raises_rather_than_reading_as_empty(client):
    """Without `x-api-key` the body says 403 under a 200 status line.

    `data: []` is indistinguishable from an empty registry, which is exactly
    how EcoRegistry's `ERROR_401` cost an afternoon. It must raise.
    """
    adapter = bc.BioCarbonAPI(client)
    client.refuse = True
    with pytest.raises(RuntimeError, match="403"):
        adapter.project_total()


def test_the_refusal_is_dropped_from_the_cache(client):
    """A 4xx is not cached; a refusal wearing a 200 would be.

    Left in place, the next run replays it without reaching the network.
    """
    adapter = bc.BioCarbonAPI(client)
    client.refuse = True
    with pytest.raises(RuntimeError):
        adapter.project_total()
    assert client.invalidated


def test_the_public_api_key_is_sent(client):
    """It ships in the site's own bundle; without it nothing is readable."""
    adapter = bc.BioCarbonAPI(client)
    headers = adapter._headers()
    assert headers["x-api-key"] == settings.BIOCARBON_API_KEY
    # And this registry's own Origin, not Verra's, which is what
    # settings.BROWSER_HEADERS carries.
    assert headers["Origin"] == settings.BIOCARBON_SITE


# -- counts and paging -----------------------------------------------------


def test_totals_come_from_the_paginators_own_total(adapter):
    assert adapter.project_total() == PROJECTS
    for resource, expected in COUNTS.items():
        assert adapter.count(resource) == expected


def test_paging_follows_last_page(adapter, client):
    rows = list(adapter.iter_credits(bc.RETIREMENTS))
    assert len(rows) == COUNTS[bc.RETIREMENTS]
    assert [c for c in client.calls if c[0] == "retreats"] == [
        ("retreats", 1),
        ("retreats", 2),
    ]


def test_every_row_across_both_pages_is_distinct(adapter):
    rows = list(adapter.iter_credits(bc.RETIREMENTS))
    assert len({row["entity_id"] for row in rows}) == COUNTS[bc.RETIREMENTS]


def test_each_resource_is_fetched_once_per_run(adapter, client):
    """A run asks `count` and then iterates; that is not a reason to re-fetch."""
    adapter.count(bc.ISSUANCES)
    list(adapter.iter_credits(bc.ISSUANCES))
    assert client.calls.count(("carbon-credits", 1)) == 1


def test_each_project_detail_is_fetched_once(adapter, client):
    list(adapter.iter_projects())
    list(adapter.iter_projects())
    assert client.calls.count(("projects/20", 1)) == 1


def test_a_short_read_is_reported(adapter, client, caplog):
    """Never trust a row count because nothing was raised."""
    client.truncate("retreats", COUNTS[bc.RETIREMENTS] + 5)
    with caplog.at_level("ERROR"):
        list(adapter.iter_credits(bc.RETIREMENTS))
    assert "INCOMPLETE" in caplog.text


def test_an_unknown_ledger_is_refused(adapter):
    with pytest.raises(ValueError):
        adapter.count("transferences")
    with pytest.raises(ValueError):
        list(adapter.iter_credits("transferences"))


def test_transferences_are_not_a_ledger():
    """714 holder-to-holder moves of units already issued.

    Scraping them as a credit event adds a bucket that double-counts the
    issuances — the same shape as SocialCarbon's `asset` feed.
    """
    assert "transferences" not in bc.LEDGERS
    assert "transferences" not in bc.LEDGER_RESOURCES.values()


def test_the_ledger_names_avoid_gold_standards_bucketing():
    """`db.credit_totals()` branches on the exact string `credits`."""
    assert "credits" not in bc.LEDGERS


# -- projects --------------------------------------------------------------


def test_the_primary_key_is_the_numeric_initiative_id(projects):
    """The published `BCR-…` reference is unique here, and still not the key.

    Every ledger row and the public URL are keyed on the numeric id, so that
    is what `project_id` holds; the reference goes to `external_id`.
    """
    assert {p["project_id"] for p in projects} == {116, 24, 31, 85, 35, 9, 20, 22, 46, 7}
    assert all(isinstance(p["project_id"], int) for p in projects)


def test_the_published_reference_is_unique_here(projects):
    """Unlike SocialCarbon's. Checked rather than assumed."""
    references = [p["external_id"] for p in projects]
    assert len(set(references)) == len(references)
    assert "BCR-CO-319-14-002" in references


def test_the_detail_url_is_the_apps_own_route(projects, adapter):
    assert adapter.detail_url(20).endswith("/registry/biocarbon/gei/project/20")
    assert all(p["detail_url"] for p in projects)


def test_tipo_macro_is_the_registrys_own_sector_untranslated(projects):
    scopes = {p["sectoral_scope"] for p in projects}
    assert "Agriculture, forestry and other land uses (AFOLU)" in scopes
    # Never normalised into another registry's vocabulary.
    assert "Agriculture Forestry and Other Land Use" not in scopes


def test_the_standard_name_is_read_not_asserted(projects):
    assert {p["standard_name"] for p in projects} == {bc.STANDARD_NAME}


def test_country_code_is_published_so_continent_can_be_derived(projects):
    assert all(p["country_code"] for p in projects)


def test_country_names_are_carried_through_in_the_registrys_own_language(projects):
    """"Colombia" beside "Malasia". Not translated at scrape time.

    config/derivation/continent.yaml reads the ISO code, so this costs
    nothing there; biome.yaml reads the name and carries both spellings.
    """
    assert "Colombia" in {p["country_name"] for p in projects}


def test_an_inverted_crediting_period_is_stored_as_published(projects):
    """The registry's own data error. Not repaired, not dropped."""
    row = next(p for p in projects if p["project_id"] == 116)
    assert row["credit_period_start"] == "2045-01-09"
    assert row["credit_period_end"] == "2019-01-07"


def test_state_and_city_are_blank_because_none_is_published(projects):
    """The only sub-national location is a free-text sentence."""
    assert all(p.get("state_province") is None for p in projects)
    assert all(p.get("city") is None for p in projects)
    row = next(p for p in projects if p["project_id"] == 20)
    assert row["extra"]["location_text"]


def test_no_yearly_ex_ante_is_invented_from_the_total(projects):
    """The total is published; the yearly figure is not, and is not computed."""
    row = next(p for p in projects if p["project_id"] == 20)
    assert row.get("avg_annual_vol_vcu") is None


def test_the_overlap_with_cercarbono_is_recorded_on_both_rows(projects):
    """Both registries publish these two projects, and both rows ship."""
    for project_id, reference in SHARED_WITH_CERCARBONO.items():
        row = next(p for p in projects if p["project_id"] == project_id)
        assert row["extra"]["also_registered_as"] == reference
    others = [
        p for p in projects if p["project_id"] not in SHARED_WITH_CERCARBONO
    ]
    assert all("also_registered_as" not in (p["extra"] or {}) for p in others)


def test_migration_provenance_is_kept(projects):
    """What a double-count check reads first."""
    row = next(p for p in projects if p["project_id"] == 22)
    assert row["extra"]["migrated_in"] == "YES"
    assert row["extra"]["migrated_from"] == "Clean Development Mechanism"


def test_methodologies_carry_both_the_code_and_the_activity(projects):
    """Which is what lets the existing CDM rules fire on this registry."""
    row = next(p for p in projects if p["project_id"] == 20)
    assert "BCR0002" in row["methodologies"]
    assert "REDD" in row["methodologies"]


# -- credits ---------------------------------------------------------------


def test_quantities_survive_the_thousands_separator(adapter):
    """`amount` reads "299,564". float() on that raises."""
    rows = list(adapter.iter_credits(bc.ISSUANCES))
    assert all(row["quantity"] is not None for row in rows)
    assert sum(row["quantity"] for row in rows) > 0


def test_every_credit_row_links_to_a_project(adapter, projects):
    known = {p["project_id"] for p in projects}
    for resource in bc.LEDGERS:
        for row in adapter.iter_credits(resource):
            assert row["project_id"] in known


def test_buffer_units_are_issued_and_their_class_is_kept(adapter):
    """Counted, because the registry's own published figure counts them.

    The class is stored so splitting them out is a config/credits.yaml change
    rather than a re-scrape.
    """
    rows = list(adapter.iter_credits(bc.ISSUANCES))
    classes = {row["unit_type"] for row in rows}
    assert classes & {"Reserva", "reserved"}


def test_the_beneficiary_is_the_end_user_not_the_retiring_account(adapter):
    """`to_name` is filled on every row and is not a third party.

    Reading it as the beneficiary would make every retirement look like a
    third-party sale the moment `sold_equals_retired` is flipped.
    """
    rows = list(adapter.iter_credits(bc.RETIREMENTS))
    named = [r for r in rows if r["beneficiary"]]
    assert named
    assert len(named) < len(rows)


def test_the_visibility_flag_is_kept_beside_the_name(adapter):
    """The registry marks most retirements private; its API returns them anyway."""
    rows = list(adapter.iter_credits(bc.RETIREMENTS))
    assert {r["status"] for r in rows} <= {"public", "private"}


# -- cancellations: the two feeds that disagree ----------------------------


def test_cancellation_rows_come_from_the_endpoint(adapter):
    rows = list(adapter.iter_credits(bc.CANCELLATIONS))
    assert len(rows) == CANCELLATION_ROWS
    assert all(row["event_date"] for row in rows)


def test_every_project_is_swept_for_cancellations(adapter, client):
    """Not only those whose blocks show a dropout.

    Narrowing on our own reading of another feed is how a registry's own
    records go missing.
    """
    list(adapter.iter_credits(bc.CANCELLATIONS))
    swept = {c[0] for c in client.calls if c[0].endswith("/cancellations")}
    assert len(swept) == PROJECTS


def test_the_stated_cancelled_total_outranks_the_rows(adapter):
    """`dropouts` on the blocks is the registry's own arithmetic.

    `amount = active + outof + dropouts` holds on every block, and the
    endpoint publishes fewer units than the blocks account for.
    """
    totals = dict(
        (project_id, quantity)
        for project_id, quantity, _events in adapter.iter_credit_totals(
            bc.CANCELLATIONS
        )
    )
    assert sum(totals.values()) == DROPOUT_UNITS


def test_no_total_is_stated_for_issuances_or_retirements(adapter):
    """Both ledgers sum to exactly what the registry publishes for itself.

    Restating them would add nothing, and a total that merely echoes the rows
    hides the day they stop agreeing.
    """
    assert list(adapter.iter_credit_totals(bc.ISSUANCES)) == []
    assert list(adapter.iter_credit_totals(bc.RETIREMENTS)) == []


def test_verified_reductions_are_not_read_as_issued(adapter, projects):
    """One project has verified units and no issuance blocks at all.

    Letting the verified figure win would report issued credits for a project
    that has issued none — the opposite error to Cercarbono's missing rows.
    """
    row = next(p for p in projects if p["project_id"] == VERIFIED_BUT_UNISSUED)
    assert row["extra"]["verified_reductions"] > 0
    issued = [
        r
        for r in adapter.iter_credits(bc.ISSUANCES)
        if r["project_id"] == VERIFIED_BUT_UNISSUED
    ]
    assert issued == []


# -- derivation ------------------------------------------------------------


def test_the_registrys_sector_wording_reaches_the_biome_rules():
    """"Agriculture, forestry and other land uses (AFOLU)" — with a comma.

    `Agriculture Forestry` misses it on the comma alone, and the gate fails
    silently: no biome for any row of the registry, nothing in the log.
    """
    biome = next(r for r in derive.load_rulesets() if r.column == "Bioma")
    assert biome.evaluate(
        {
            "sectoral_scope": "Agriculture, forestry and other land uses (AFOLU)",
            "country_name": "Colombia",
        }
    )


def test_the_registrys_spanish_country_names_reach_a_biome():
    """The names arrive as the registry spells them: "Malasia", "Perú"."""
    biome = next(r for r in derive.load_rulesets() if r.column == "Bioma")
    scope = "Agriculture, forestry and other land uses (AFOLU)"
    for name, expected in (
        ("Malasia", "Floresta Tropical do Sudeste Asiático"),
        ("Malaysia", "Floresta Tropical do Sudeste Asiático"),
        ("Perú", "Amazônia (bacia amazônica)"),
        ("Peru", "Amazônia (bacia amazônica)"),
        ("Panamá", "Floresta Tropical Mesoamericana"),
        ("Panama", "Floresta Tropical Mesoamericana"),
    ):
        value = biome.evaluate({"sectoral_scope": scope, "country_name": name})
        assert value is not None, name
        assert value[0] == expected, name


def test_every_project_in_the_index_reaches_a_continent(projects):
    """The ISO code is published on all of them, so this is the code path."""
    rulesets = derive.load_rulesets()
    for row in projects:
        values = {
            column: value
            for column, value, _rule in derive.derive_for_project(dict(row), rulesets)
        }
        assert values.get("Continent"), row["country_code"]


def test_the_bcr_methodology_numbers_classify(projects):
    """No AFOLU sub-type code is published; the methodology name is the key."""
    rulesets = derive.load_rulesets()
    row = next(p for p in projects if p["project_id"] == 20)
    values = {
        column: value
        for column, value, _rule in derive.derive_for_project(dict(row), rulesets)
    }
    assert values["Tipo Micro de Projeto"] == "Desmatamento e Degradação Evitados (REDD)"


# -- end to end through db, derive and excel -------------------------------


def _derive(conn):
    rulesets = derive.load_rulesets()
    rows = []
    for project in db.all_projects(conn, REGISTRY):
        record = dict(project)
        for column, value, rule in derive.derive_for_project(record, rulesets):
            rows.append((REGISTRY, record["project_id"], column, value, rule))
    db.replace_derived(conn, rows)


def test_it_stores_derives_and_exports(conn, adapter):
    db.upsert_projects(conn, REGISTRY, adapter.iter_projects())
    for resource in bc.LEDGERS:
        db.upsert_credit_events(
            conn, REGISTRY, resource, adapter.iter_credits(resource)
        )
        totals = base.credit_totals_of(adapter, resource)
        if totals is not None:
            db.upsert_credit_totals(conn, REGISTRY, resource, totals)
    conn.commit()
    _derive(conn)

    columns, rows = excel.build_rows(conn, REGISTRY)
    assert len(rows) == PROJECTS

    by_id = {row["Project ID"]: row for row in rows}
    assert by_id["BCR-CO-319-14-002"]["Registry"] == "BioCarbon"
    assert by_id["BCR-CO-319-14-002"]["Standard"] == bc.STANDARD_NAME
    # Continent from the ISO code, the Gold Standard path.
    assert by_id["BCR-CO-319-14-002"]["Continent"] == "South America"
    assert "Project URL" in columns
    assert all(r["Project URL"].startswith(settings.BIOCARBON_SITE) for r in rows)

    # The stated cancellation total wins over the endpoint's three rows.
    cancelled = sum(r["Total Credits Cancelled"] or 0 for r in rows)
    assert cancelled == DROPOUT_UNITS


def test_total_ex_ante_falls_back_to_the_published_figure(conn, adapter):
    """BioCarbon publishes the total and no yearly estimate.

    `derive` builds Total Ex Ante as yearly x duration, so no rule can fire
    here. The stored figure fills in rather than leaving a published number
    out of the sheet.
    """
    db.upsert_projects(conn, REGISTRY, adapter.iter_projects())
    conn.commit()
    _derive(conn)
    _columns, rows = excel.build_rows(conn, REGISTRY)
    stated = [r for r in rows if r["Total Ex Ante"]]
    assert stated
    assert all(r["Yearly Ex Ante"] is None for r in stated)
