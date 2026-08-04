"""Puro adapter: reading a Next.js payload, bundles, withdrawals, totals. No network.

Runs against `tests/fixtures/puro-*.html`, captured from the live registry on
2026-08-05. Six projects out of the live 118, and the subset is **closed**: a
transaction is included only if every facility it touches is one of the six, so
no bundle here names a project the fixture does not have. Five of the six carry
their real published totals; 310958 is the exception, because it is in the
fixture only to be the other half of a transaction and its own retirements
reach facilities that are not.

Chosen for being awkward rather than representative:

* **583695** is in Namibia, whose `countryCode` is the two-letter string
  `NA` — the value every other adapter's `NOT_STATED` table would delete. It
  also carries the one retirement here whose beneficiary the registry is
  withholding.
* **517437** and **452184** carry the two withdrawal labels. No withdrawn
  quantity is published for either and the registry's own issued total counts
  them in full, which is why there is no cancellation ledger.
* **181856** is retired in a transaction that also draws on **310958** — a
  transaction spanning two facilities, which is why a credit row is a bundle
  and not a transaction. 310958 is in the fixture for that reason alone.
* **861867** is certified with no credits at all and no crediting period. Its
  page states `0` for both totals, and a stated zero must leave the cell
  blank rather than write `0`.

The pages are the real ones only in the part that matters: the records are
verbatim, and the RSC stream is split across two `__next_f.push` calls
mid-object, which is what the live 5.2 MB retirement page does and what any
reader has to join before decoding. The markup around them is not kept — it is
900 KB of inline map per detail page and the adapter never looks at it.
"""

from __future__ import annotations

import logging

import pytest

from carbon_scraper import db, derive, excel, settings
from carbon_scraper import registries
from carbon_scraper.registries import base
from carbon_scraper.registries.puro import api as puro
from carbon_scraper.registries.puro import flight

from conftest import RecordingProgress

REGISTRY = settings.PURO
FIXTURES = settings.FIXTURES_DIR

PROJECTS = 6
ISSUANCE_BUNDLES = 11
RETIREMENT_BUNDLES = 57

#: `countryCode` is the string `NA`, and it means Namibia.
NAMIBIA = 583695
#: Certified, no credits, no crediting period.
EMPTY = 861867
#: One `FULLY_WITHDRAWN` issuance; one `PARTIALLY_WITHDRAWN`.
FULLY_WITHDRAWN = 517437
PARTLY_WITHDRAWN = 452184
#: Retired together in one transaction that spans both facilities.
SPANNED = (181856, 310958)

#: What the fixtures' own detail pages state. Real published figures except
#: 310958's retirements — see the note above.
STATED = {
    181856: (4261.0, 2916.0),
    310958: (22254.0, 7889.0),
    452184: (173.0, 69.0),
    517437: (9694.0, 460.0),
    583695: (799.0, 323.0),
    861867: (0.0, 0.0),
}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeClient:
    """Serves the four routes from fixtures. Records every URL asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.pages = {
            settings.PURO_PROJECTS_PATH: _fixture("puro-projects.html"),
            settings.PURO_ISSUANCES_PATH: _fixture("puro-issuances.html"),
            settings.PURO_RETIREMENTS_PATH: _fixture("puro-retirements.html"),
        }
        for code in STATED:
            self.pages[f"{settings.PURO_PROJECTS_PATH}/{code}"] = _fixture(
                f"puro-detail-{code}.html"
            )

    def get_text(self, url, *, headers=None, **kwargs):
        assert url.startswith(settings.PURO_SITE), url
        path = url[len(settings.PURO_SITE) :]
        self.calls.append(path)
        try:
            return self.pages[path]
        except KeyError:
            raise AssertionError(f"no fixture for {path}") from None

    def close(self) -> None:  # pragma: no cover - the adapter never owns this
        pass


@pytest.fixture()
def api():
    return puro.PuroAPI(FakeClient())


@pytest.fixture()
def client(api):
    return api.client


# --- wiring ---------------------------------------------------------------


def test_implements_the_adapter_protocol(api):
    assert isinstance(api, base.RegistryAdapter)
    assert api.registry == REGISTRY
    assert api.ledgers == (puro.ISSUANCES, puro.RETIREMENTS)


def test_registry_is_dispatched_and_aliased():
    assert registries.ADAPTERS[REGISTRY] == (registries._puro,)
    assert registries.adapter_class(REGISTRY) is puro.PuroAPI
    for alias in ("puro", "puro-earth", "puroearth", "corc", "PURO"):
        assert registries.resolve(alias) == REGISTRY


def test_registry_has_a_label_an_estimate_and_a_detail_url():
    assert settings.REGISTRY_LABELS[REGISTRY] == "Puro.earth"
    assert settings.SYNC_ESTIMATE_MINUTES[REGISTRY] > 0
    assert settings.PROJECT_DETAIL_URLS[REGISTRY] == settings.PURO_PROJECT_DETAIL_URL
    assert api_detail_url() == f"{settings.PURO_SITE}/projects/{NAMIBIA}"


def api_detail_url() -> str:
    return puro.PuroAPI(FakeClient()).detail_url(NAMIBIA)


def test_ledger_names_are_not_the_gold_standard_bucket_name():
    # `db.credit_totals()` branches on the exact string `credits` to select
    # bucket-by-status semantics. These rows carry no such status.
    assert "credits" not in puro.LEDGERS


def test_asks_for_html_and_this_registrys_own_origin(api):
    headers = api._headers()
    assert headers["Accept"].startswith("text/html")
    assert headers["Origin"] == settings.PURO_SITE
    assert settings.SITE not in headers["Referer"]
    # BROWSER_HEADERS is built for the JSON APIs; a GET of a page has no body.
    assert "Content-Type" not in headers


# --- the page payload -----------------------------------------------------


def test_payload_joins_every_push_before_decoding():
    html = _fixture("puro-projects.html")
    assert html.count("__next_f.push") == 3  # `[0]`, then the stream in two
    # Neither half is decodable alone: the split lands mid-object.
    rows = flight.data_list(flight.payload(html))
    assert len(rows) == PROJECTS


def test_a_page_without_a_data_array_raises_rather_than_reporting_empty():
    # Returning [] here would report a broken page as a registry with no
    # projects, which is the failure this whole codebase keeps meeting.
    with pytest.raises(ValueError, match="no data array"):
        flight.data_list("nothing useful here")


def test_a_missing_stat_block_is_none_not_zero():
    assert flight.labelled_number("", "Issued credits") is None


def test_country_name_is_read_beside_its_own_flag():
    payload = flight.payload(_fixture(f"puro-detail-{NAMIBIA}.html"))
    assert flight.flagged_country(payload, "NA") == "Namibia"
    # A different project's flag is not on this page, so nothing is returned
    # rather than whichever name happened to be nearby.
    assert flight.flagged_country(payload, "US") is None
    assert flight.flagged_country(payload, None) is None
    assert flight.flagged_country(payload, "XXX") is None


def test_flag_is_built_from_the_iso_code():
    assert flight.flag_for("NA") == "\U0001F1F3\U0001F1E6"
    assert flight.flag_for("n/a") is None


# --- projects -------------------------------------------------------------


def test_projects_are_read_with_one_detail_request_each(api, client):
    rows = [row for row, _ in api.iter_projects()]
    assert len(rows) == PROJECTS
    assert api.project_total() == PROJECTS
    detail_calls = [c for c in client.calls if c.count("/") == 2]
    assert len(detail_calls) == PROJECTS
    assert len(set(detail_calls)) == PROJECTS  # each fetched once


def test_progress_is_cumulative(api):
    progress = RecordingProgress()
    list(api.iter_projects(progress=progress))
    progress.assert_cumulative(PROJECTS)


def test_the_published_code_is_the_primary_key_and_is_unique(api):
    rows = [row for row, _ in api.iter_projects()]
    keys = [row["project_id"] for row in rows]
    assert len(set(keys)) == len(keys)
    assert all(isinstance(key, int) for key in keys)
    # Unlike SocialCarbon's reference, nothing is hashed: the key is the code.
    assert all(str(row["project_id"]) == row["external_id"] for row in rows)


def test_namibia_keeps_its_country_code(api):
    row = _project(api, NAMIBIA)
    # `NA` survives only because puro.NOT_STATED is empty. Every other
    # adapter's table would blank it and take the Continent with it.
    assert row["country_code"] == "NA"
    assert row["country_name"] == "Namibia"
    assert "na" not in {value.casefold() for value in puro.NOT_STATED}


def test_tipo_macro_and_metodologia_are_the_one_published_classification(api):
    row = _project(api, NAMIBIA)
    assert row["sectoral_scope"] == "Terrestrial Storage of Biomass"
    assert row["methodologies"] == row["sectoral_scope"]
    assert (row["extra"] or {})["methodology_code"] == "C06000000"


def test_standard_name_is_asserted_and_the_rule_version_is_kept(api):
    row = _project(api, NAMIBIA)
    assert row["standard_name"] == "Puro Standard"
    assert (row["extra"] or {})["general_rules_version"].startswith(
        "Puro Standard General Rules"
    )


def test_deliberate_blanks(api):
    for row, _ in api.iter_projects():
        # No sub-national field exists anywhere on this registry, and no
        # estimate is published. Nothing is back-computed from the issuances.
        assert row.get("state_province") is None
        assert row.get("city") is None
        assert row.get("region_name") is None
        assert row.get("avg_annual_vol_vcu") is None
        assert row.get("exante_quantity") is None
        assert row.get("additional_certification") is None


def test_sdgs_are_not_written_into_additional_certification(api):
    row = _project(api, NAMIBIA)
    assert "Climate action" in (row["extra"] or {})["sdgs"]
    assert row.get("additional_certification") is None


def test_a_project_may_publish_no_crediting_period(api):
    row = _project(api, EMPTY)
    assert row["credit_period_start"] is None
    assert row["credit_period_end"] is None


def test_every_bundle_names_a_project_we_stored(api, caplog):
    with caplog.at_level(logging.ERROR):
        list(api.iter_projects())
        api.count(puro.ISSUANCES)
        api.count(puro.RETIREMENTS)
        api._check_facilities_resolve(
            {row["project_id"] for row, _ in api.iter_projects()}
        )
    assert "not in the project list" not in caplog.text


def test_an_orphan_facility_is_reported(api, caplog):
    api.count(puro.ISSUANCES)
    with caplog.at_level(logging.ERROR):
        api._check_facilities_resolve({NAMIBIA})
    assert "INCOMPLETE" in caplog.text
    assert "not in the project list" in caplog.text


# --- credits --------------------------------------------------------------


def test_a_credit_row_is_a_bundle_not_a_transaction(api):
    issuances = list(api.iter_credits(puro.ISSUANCES))
    retirements = list(api.iter_credits(puro.RETIREMENTS))
    assert len(issuances) == ISSUANCE_BUNDLES
    assert len(retirements) == RETIREMENT_BUNDLES
    assert api.count(puro.ISSUANCES) == ISSUANCE_BUNDLES
    assert api.count(puro.RETIREMENTS) == RETIREMENT_BUNDLES
    # More rows than transactions, because a retirement draws from several
    # production facilities at once.
    transactions = {row["serial_no"].rsplit("_", 1)[0] for row in retirements}
    assert len(transactions) < len(retirements)


def test_one_transaction_spans_two_facilities(api):
    rows = list(api.iter_credits(puro.RETIREMENTS))
    by_project = {row["project_id"] for row in rows}
    assert set(SPANNED) <= by_project
    # Filing that transaction against one project would lose the other's
    # units entirely.
    for project_id in SPANNED:
        assert any(row["project_id"] == project_id for row in rows)


def test_entity_ids_are_unique_across_and_within_ledgers(api):
    issuances = list(api.iter_credits(puro.ISSUANCES))
    retirements = list(api.iter_credits(puro.RETIREMENTS))
    ids = [row["entity_id"] for row in issuances + retirements]
    assert len(set(ids)) == len(ids)
    assert all(row["serial_no"] for row in issuances + retirements)


def test_credit_progress_is_cumulative(api):
    progress = RecordingProgress()
    list(api.iter_credits(puro.RETIREMENTS, progress=progress))
    progress.assert_cumulative(RETIREMENT_BUNDLES)


def test_an_unknown_ledger_is_refused(api):
    with pytest.raises(ValueError, match="no ledger named"):
        list(api.iter_credits("cancellations"))
    with pytest.raises(ValueError, match="no ledger named"):
        api.count("cancellations")


def test_withdrawal_is_a_label_with_no_quantity(api):
    rows = list(api.iter_credits(puro.ISSUANCES))
    withdrawn = {row["project_id"]: row["status"] for row in rows if row["status"]}
    assert withdrawn[FULLY_WITHDRAWN] == "FULLY_WITHDRAWN"
    assert withdrawn[PARTLY_WITHDRAWN] == "PARTIALLY_WITHDRAWN"
    # The units are still issued: the registry's own total counts them.
    issued = sum(
        row["quantity"] for row in rows if row["project_id"] == FULLY_WITHDRAWN
    )
    assert issued == STATED[FULLY_WITHDRAWN][0]


def test_there_is_no_cancellation_ledger(api):
    # No withdrawn quantity is published for either label, so cancelled stays
    # blank rather than being guessed at from the transaction volume.
    assert "cancellations" not in api.ledgers
    assert list(api.iter_credit_totals("cancellations")) == []


def test_retirement_status_is_the_usage_type(api):
    rows = list(api.iter_credits(puro.RETIREMENTS))
    assert all(row["status"] for row in rows)
    assert {row["status"] for row in rows} <= {
        "GENERIC_COMPENSATION",
        "BUNDLED_WITH_PRODUCT_OR_SERVICE",
        "SPECIFIC_ACTIVITY_LIKE_FLIGHTS",
        "DISCLOSURE",
        "SUPPORT",
        "OTHER",
    }


def test_beneficiary_is_the_third_party_and_may_be_embargoed(api):
    rows = [
        row for row in api.iter_credits(puro.RETIREMENTS) if row["project_id"] == NAMIBIA
    ]
    assert any(row["beneficiary"] for row in rows)
    # One of Namibia's retirements is under an embargo the registry states and
    # honours: the name is simply absent, and a later sync picks it up.
    assert any(row["beneficiary"] is None for row in rows)


def test_the_credit_class_is_carried_through_as_published(api):
    classes = {row["unit_type"] for row in api.iter_credits(puro.RETIREMENTS)}
    assert classes <= {"CORC", "CORC20+", "CORC100+", "CORC1000+", "CORC_100"}


def test_a_transaction_whose_bundles_do_not_add_up_is_reported(api, caplog):
    api._fetch(settings.PURO_ISSUANCES_PATH)[0]["volume"] = 999999
    with caplog.at_level(logging.ERROR):
        list(api.iter_credits(puro.ISSUANCES))
    assert "INCOMPLETE" in caplog.text
    assert "bundle(s) sum to" in caplog.text


# --- the registry's own totals -------------------------------------------


def test_stated_totals_come_from_the_project_pages(api):
    for resource, index in ((puro.ISSUANCES, 0), (puro.RETIREMENTS, 1)):
        stated = dict(
            (project_id, quantity)
            for project_id, quantity, _ in api.iter_credit_totals(resource)
        )
        expected = {k: v[index] for k, v in STATED.items() if v[index] > 0}
        assert stated == expected


def test_a_stated_zero_leaves_the_cell_blank(api):
    stated = {p for p, _, _ in api.iter_credit_totals(puro.ISSUANCES)}
    # 861867 is certified with no credits. Plan Vivo's empty ledgers set the
    # convention: blank, not 0.
    assert EMPTY not in stated


def test_stated_totals_agree_with_the_bundles(api, caplog):
    with caplog.at_level(logging.ERROR):
        list(api.iter_credit_totals(puro.ISSUANCES))
        list(api.iter_credit_totals(puro.RETIREMENTS))
    assert "INCOMPLETE" not in caplog.text


def test_a_short_ledger_is_reported_against_the_stated_total(api, caplog):
    # The one failure a self-counting feed cannot see: the transaction route
    # truncating. The stated total is what notices, and it wins in
    # db.credit_totals().
    api._fetch(settings.PURO_ISSUANCES_PATH).pop()
    with caplog.at_level(logging.ERROR):
        list(api.iter_credit_totals(puro.ISSUANCES))
    assert "INCOMPLETE" in caplog.text
    assert "bundles sum to" in caplog.text


def test_a_detail_page_that_states_nothing_leaves_the_ledger_alone(api, caplog):
    api.client.pages[f"{settings.PURO_PROJECTS_PATH}/{NAMIBIA}"] = "<html></html>"
    with caplog.at_level(logging.ERROR):
        stated = {p for p, _, _ in api.iter_credit_totals(puro.ISSUANCES)}
    assert NAMIBIA not in stated
    assert "publishes no" in caplog.text


def test_the_optional_totals_seam_is_reachable(api):
    assert base.credit_totals_of(api, puro.ISSUANCES) is not None


# --- end to end -----------------------------------------------------------


def _project(api, project_id: int) -> dict:
    for row, _ in api.iter_projects():
        if row["project_id"] == project_id:
            return row
    raise AssertionError(f"{project_id} not in the fixture")


def _sync(conn, api) -> None:
    db.upsert_projects(
        conn, REGISTRY, [(row, raw) for row, raw in api.iter_projects()]
    )
    for resource in api.ledgers:
        db.upsert_credit_events(conn, REGISTRY, resource, list(api.iter_credits(resource)))
        totals = base.credit_totals_of(api, resource)
        if totals is not None:
            db.upsert_credit_totals(conn, REGISTRY, resource, list(totals))


def _derive(conn) -> None:
    rulesets = derive.load_rulesets()
    rows = []
    for project in db.all_projects(conn, REGISTRY):
        record = dict(project)
        for column, value, rule in derive.derive_for_project(record, rulesets):
            rows.append((REGISTRY, record["project_id"], column, value, rule))
    db.replace_derived(conn, rows)


def test_end_to_end_reaches_the_spreadsheet(conn, api):
    _sync(conn, api)
    _derive(conn)
    _columns, rows = excel.build_rows(conn, REGISTRY)
    assert len(rows) == PROJECTS

    by_id = {row["Project ID"]: row for row in rows}
    namibia = by_id[str(NAMIBIA)]
    assert namibia["País"] == "Namibia"
    assert namibia["Standard"] == "Puro Standard"
    assert namibia["Tipo Macro de Projeto"] == "Terrestrial Storage of Biomass"
    assert namibia["Total Credits Issued"] == STATED[NAMIBIA][0]
    assert namibia["Total Credits Retired"] == STATED[NAMIBIA][1]
    # No cancellation ledger and no withdrawn quantity anywhere.
    assert not namibia["Total Credits Cancelled"]
    # No estimate is published, and none is back-computed.
    assert not namibia["Yearly Ex Ante"]
    assert not namibia["Total Ex Ante"]

    empty = by_id[str(EMPTY)]
    assert not empty["Total Credits Issued"]
    assert not empty["Total Credits Retired"]


def test_derivation_reaches_every_puro_project(conn, api):
    _sync(conn, api)
    _derive(conn)
    _columns, rows = excel.build_rows(conn, REGISTRY)
    assert all(row["Durabilidade"] for row in rows)
    assert all(row["Tipo Micro de Projeto"] for row in rows)
    # Biome is for land-use projects. Puro certifies engineered and hybrid
    # removals, so it stays blank rather than being guessed from a country.
    assert not any(row["Bioma"] for row in rows)


def test_continent_is_derived_from_the_iso_code(conn, api):
    _sync(conn, api)
    _derive(conn)
    _columns, rows = excel.build_rows(conn, REGISTRY)
    by_id = {row["Project ID"]: row for row in rows}
    assert by_id[str(NAMIBIA)]["Continent"]
