"""Cercarbono adapter: normalisation, filtering and credit accounting. No network.

Runs against `tests/fixtures/cercarbono-*.json`, captured from the live
EcoRegistry API on 2026-07-28. The fixtures deliberately include records the
adapter must reject — an analytics project on the circular-economy standard
(209) and a retirement belonging to one (224) — so the CO2 filter is tested
rather than assumed.
"""

from __future__ import annotations

import json

import pytest

from carbon_scraper import db, derive, excel, settings
from carbon_scraper.registries import base
from carbon_scraper.registries.cercarbono import api as cc

from conftest import RecordingProgress, load_fixture as _load

# Project ids in the fixture set, and what each one is there to prove.
IN_CO2 = {1, 29, 85, 106, 274}
OFF_STANDARD_ANALYTICS = 209   # circular economy: must never be scraped
OFF_STANDARD_RETIREMENT = 224  # ditto, on the retirement side
CONVERTED_IN = 106             # ex-BioCarbon: absent from analytics entirely




class FakeClient:
    """Serves the four endpoints from fixtures. Records what was asked for."""

    def __init__(self):
        self.calls = []
        self.invalidated = []
        self._by_path = {
            f"project/public-by-standard/{settings.CERCARBONO_STANDARD}": _load(
                "cercarbono-projects.json"
            ),
            "analytics/projects": _load("cercarbono-analytics.json"),
            "analytics/get-retirements": _load("cercarbono-retirements.json"),
        }
        self._detail = _load("cercarbono-detail.json")
        self._converted = _load("cercarbono-detail-converted.json")

    def get_json(self, url, **kwargs):
        path = url.split("/platform/", 1)[1]
        self.calls.append(path)
        if path.startswith("project/public/"):
            project_id = int(path.rsplit("/", 1)[1])
            source = self._converted if project_id == CONVERTED_IN else self._detail
            payload = json.loads(json.dumps(source))
            payload["project"]["id"] = project_id
            return payload
        return self._by_path[path]

    def invalidate(self, method, url, **kwargs):
        self.invalidated.append(url)

    def close(self):
        pass


@pytest.fixture()
def client():
    return FakeClient()


@pytest.fixture()
def adapter(client):
    return cc.CercarbonoAPI(client)


def test_progress_is_cumulative(adapter, progress):
    """The contract every adapter's progress callback has to honour."""
    list(adapter.iter_projects(progress=progress))
    progress.assert_cumulative(adapter.project_total())

    counts = RecordingProgress()
    list(adapter.iter_credits(cc.RETIREMENTS, progress=counts))
    counts.assert_cumulative()


# -- the header trap -------------------------------------------------------


def test_the_two_mandatory_headers_are_sent():
    """Without `platform` and `lng` every call answers ERROR_401.

    That response reads like a credential wall and is not one — the data is
    public. Dropping either header is the single easiest way to conclude this
    registry needs an account.
    """
    assert settings.CERCARBONO_HEADERS["platform"] == "ecoregistry"
    assert settings.CERCARBONO_HEADERS["lng"]


def test_an_application_level_error_is_raised_not_swallowed(adapter, client):
    """The platform reports refusals in a 200 body, so status is not enough.

    An unnoticed ERROR_401 here would be indistinguishable from an empty
    registry: no exception, no rows, no reason.
    """
    client._by_path["analytics/projects"] = {
        "status": 0,
        "codeMessages": [{"codeMessage": "ERROR_401", "message": "No autorizado"}],
    }
    with pytest.raises(ValueError, match="ERROR_401"):
        adapter.analytics()


def test_a_refusal_is_dropped_from_the_cache(adapter, client):
    """It arrived as a 200, so it was cached — for the next 24 hours.

    Left there, correcting the header and re-running replays the same
    refusal, and `--refresh` is not what anyone reaches for when debugging a
    header.
    """
    client._by_path["analytics/projects"] = {
        "codeMessages": [{"codeMessage": "ERROR_401", "message": "No autorizado"}],
    }
    with pytest.raises(ValueError):
        adapter.analytics()
    assert client.invalidated == [f"{settings.CERCARBONO_API}/analytics/projects"]


def test_a_good_response_is_left_in_the_cache(adapter, client):
    adapter.analytics()
    assert client.invalidated == []


# -- CO2 only --------------------------------------------------------------


def test_the_standard_list_decides_which_projects_exist(adapter):
    """Membership comes from the CO2 standard, not from the bulk feeds.

    `analytics/projects` covers every Cercarbono standard at once. Driving
    iteration from it would pull in biodiversity and circular-economy
    projects, whose credits are not tCO2e.
    """
    assert adapter.project_ids() == IN_CO2
    assert OFF_STANDARD_ANALYTICS in adapter.analytics()


def test_off_standard_credits_are_filtered_out(adapter):
    issued = [r["project_id"] for r in adapter.iter_credits(cc.ISSUANCES)]
    retired = [r["project_id"] for r in adapter.iter_credits(cc.RETIREMENTS)]
    assert set(issued) <= IN_CO2
    assert set(retired) <= IN_CO2
    assert OFF_STANDARD_ANALYTICS not in issued
    assert OFF_STANDARD_RETIREMENT not in retired


def test_counts_match_what_is_actually_yielded(adapter):
    """`count` feeds the progress total and the INCOMPLETE check.

    If it counted the unfiltered feed it would over-report, and every run
    would look like it had lost records.
    """
    for resource in cc.LEDGERS:
        assert adapter.count(resource) == len(list(adapter.iter_credits(resource)))


# -- normalisation ---------------------------------------------------------


def test_project_normalises_onto_the_shared_columns(adapter):
    rows = {row["project_id"]: row for row, _ in adapter.iter_projects()}
    for row in rows.values():
        assert set(row) <= set(db.PROJECT_FIELDS)
    assert rows[274]["external_id"] == "CDC-271"
    assert rows[274]["project_name"] == "Vinaqua WWTP Carbon Project"
    assert rows[274]["country_name"] == "South Africa"


def test_external_id_is_the_registry_code_not_the_internal_number(adapter):
    """`code` (CDC-271) is what the registry shows; `id` (274) is a key.

    They do not agree, so using the id would give the business a reference
    Cercarbono's own search does not find.
    """
    rows = {row["project_id"]: row for row, _ in adapter.iter_projects()}
    assert rows[274]["project_id"] == 274
    assert rows[274]["external_id"] == "CDC-271"


def test_tipo_macro_is_the_raw_cercarbono_sector(adapter, client):
    """Carried through untranslated, like every other registry's vocabulary."""
    listing = client._by_path[
        f"project/public-by-standard/{settings.CERCARBONO_STANDARD}"
    ]["projects"]
    published = {
        p["id"]: [s["description"] for s in p["sectors"]] for p in listing
    }
    for row, _ in adapter.iter_projects():
        value = row["sectoral_scope"]
        for description in published[row["project_id"]]:
            assert description in (value or "")


def test_repeated_sectors_are_not_repeated_in_the_cell(adapter):
    """Cercarbono lists a sector once per verification.

    A single-sector project can publish `Land use (AFOLU)` three times; the
    cell must say it once.
    """
    for row, _ in adapter.iter_projects():
        parts = (row["sectoral_scope"] or "").split("; ")
        assert len(parts) == len(set(parts))


def test_the_worldwide_placeholder_is_not_written_as_a_location(adapter):
    """EcoRegistry stores "Worldwide" where a region and city were never entered.

    It is a blank, not a place. Writing it into Estado/Cidade would state a
    location the registry never published.
    """
    for row, _ in adapter.iter_projects():
        assert row["state_province"] != "Worldwide"
        assert row["city"] != "Worldwide"


def test_not_defined_is_treated_as_blank(adapter):
    """"Not defined" is the registry saying it has no value, not a value."""
    assert cc._stated("Not defined") is None
    assert cc._stated("No definido/Not defined") is None
    assert cc._stated("  ") is None
    assert cc._stated("PROTOCOL CVCC 4.5") == "PROTOCOL CVCC 4.5"


def test_country_code_and_yearly_ex_ante_stay_blank(adapter):
    """Cercarbono publishes neither. They are never filled from elsewhere."""
    for row, _ in adapter.iter_projects():
        assert row.get("country_code") is None
        assert row.get("avg_annual_vol_vcu") is None


def test_crediting_period_comes_from_the_per_project_detail(adapter):
    """The only endpoint that publishes it — hence one request per project."""
    rows = {row["project_id"]: row for row, _ in adapter.iter_projects()}
    assert rows[1]["credit_period_start"] == "2008-01-16"
    assert rows[1]["credit_period_end"] == "2032-01-15"


# -- credits ---------------------------------------------------------------


def test_credit_rows_normalise_onto_the_shared_columns(adapter):
    for resource in cc.LEDGERS:
        for row in adapter.iter_credits(resource):
            assert set(row) <= set(db.CREDIT_EVENT_FIELDS)


def test_issuance_keys_are_stable_across_runs(adapter):
    """Every write is an idempotent upsert, which needs a key that does not move.

    An issuance has no numeric id, only a serial string. A counter would
    renumber every row the moment the feed order changed, turning a re-run
    into a duplicate instead of a repair.
    """
    first = [r["entity_id"] for r in adapter.iter_credits(cc.ISSUANCES)]
    second = [r["entity_id"] for r in cc.CercarbonoAPI(FakeClient()).iter_credits(
        cc.ISSUANCES
    )]
    assert first == second
    assert len(set(first)) == len(first)


def test_buffer_serials_are_kept_and_flagged(adapter):
    """The registry's own issued total counts buffer credits, so ours must too.

    The flag is kept so that splitting them out later needs no re-scrape.
    """
    rows = list(adapter.iter_credits(cc.ISSUANCES))
    assert {r["unit_type"] for r in rows} <= {"Buffer", "Credit"}
    assert any(r["unit_type"] == "Buffer" for r in rows)


def test_retirement_beneficiary_and_reason_are_carried(adapter):
    """Cercarbono publishes who credits were retired for, unlike Gold Standard.

    That is what the beneficiary-based reading of "sold" would need if the
    business ever asks for it.
    """
    rows = list(adapter.iter_credits(cc.RETIREMENTS))
    assert any(r["beneficiary"] for r in rows)
    assert any(r["reason"] for r in rows)


def test_kilogram_quantities_are_converted_to_tonnes():
    """`is_kg` marks a quantity in kg. Mixing units in one column is silently wrong.

    No live row uses it today; the platform has kg-denominated retirement
    endpoints, so the flag exists and is honoured rather than trusted.
    """
    assert cc.normalize_retirement({"id": 1, "quantity": 2500, "is_kg": 1})["quantity"] == 2.5
    assert cc.normalize_retirement({"id": 1, "quantity": 2500, "is_kg": 0})["quantity"] == 2500


def test_the_ledger_is_not_called_credits():
    """`db.credit_totals()` branches on that exact string.

    Naming it `credits` would select Gold Standard's bucket-by-status
    semantics, and Cercarbono records carry no status — every project would
    report zero issued credits.
    """
    assert "credits" not in cc.LEDGERS
    assert cc.LEDGERS == ("issuances", "retirements")


def test_no_cancellation_ledger_is_invented():
    """Cercarbono publishes none, so the column stays blank, not zero."""
    assert "cancellations" not in cc.LEDGERS


# -- the converted-in gap --------------------------------------------------


def test_a_converted_in_project_still_reports_its_issued_credits(adapter, conn):
    """Two ex-BioCarbon projects are missing from the bulk credit feed.

    They have retirements. Summing the rows alone would show them retiring
    credits they never issued — a number the business would rightly query.
    The per-project detail does publish their issued total, and
    `credit_totals` exists precisely to outrank a row sum.
    """
    assert CONVERTED_IN not in adapter.analytics()
    list(adapter.iter_projects())

    stated = dict(
        (pid, quantity) for pid, quantity, _ in adapter.iter_credit_totals(cc.ISSUANCES)
    )
    assert stated[CONVERTED_IN] > 0

    db.upsert_credit_events(
        conn, settings.CERCARBONO, cc.RETIREMENTS,
        list(adapter.iter_credits(cc.RETIREMENTS)),
    )
    db.upsert_credit_totals(
        conn, settings.CERCARBONO, cc.ISSUANCES,
        adapter.iter_credit_totals(cc.ISSUANCES),
    )
    totals = db.credit_totals(conn)[(settings.CERCARBONO, CONVERTED_IN)]
    assert totals["issuances"] >= totals["retirements"]


def test_a_twice_published_serial_does_not_inflate_the_issued_total(conn):
    """CDC-196 publishes its 2022 and 2023 issuances under two serial revisions.

    Identical quantities under `…_R6_…` and `…_R7_…`, so its rows sum to
    161,297 against a true 120,448 — 34% high, with the run finishing cleanly.
    Two other endpoints agree on 120,448, and `credit_totals` is what the
    exporter reads. Live figures, 2026-07-28.
    """
    rows = [
        {"entity_id": cc.serial_entity_id(s), "project_id": 196, "quantity": q}
        for s, q in (
            ("CDC_196_..._R7_..._2022", 13374),
            ("CDC_196_..._R7_..._2023", 27475),
            ("CDC_196_..._R6_..._2022", 13374),
            ("CDC_196_..._R6_..._2023", 27475),
            ("CDC_196_..._R6_..._1_2_2023", 20540),
            ("CDC_196_..._R6_..._1_2_2024", 40375),
            ("CDC_196_..._R6_..._1_2_2025", 18684),
        )
    ]
    db.upsert_credit_events(conn, settings.CERCARBONO, cc.ISSUANCES, rows)
    assert db.credit_totals(conn)[(settings.CERCARBONO, 196)]["issuances"] == 161297

    db.upsert_credit_totals(
        conn, settings.CERCARBONO, cc.ISSUANCES, [(196, 120448.0, None)]
    )
    assert db.credit_totals(conn)[(settings.CERCARBONO, 196)]["issuances"] == 120448


def test_issued_total_reads_every_verification(adapter):
    """A project is certified once per verification, and they add up.

    CDC-196: 40,849 + 79,599 = 120,448. Reading only the first entry would
    report a third of the project's credits.
    """
    detail = {"certificatedVerification": [{"total": 40849}, {"total": 79599}]}
    assert cc.issued_total(detail) == 120448

    # An empty list is a real zero; a record without the key is missing.
    assert cc.issued_total({"certificatedVerification": []}) == 0.0
    assert cc.issued_total({}) is None


def test_stated_totals_are_optional_and_only_for_issuances(adapter):
    """Most registries publish no per-project total; the seam must tolerate that.

    Gold Standard's adapter has no `iter_credit_totals` at all, and the
    pipeline has to keep working for it.
    """
    from carbon_scraper.registries.goldstandard.api import GoldStandardAPI

    assert not hasattr(GoldStandardAPI, "iter_credit_totals")
    assert base.credit_totals_of(GoldStandardAPI(client=FakeClient()), "credits") is None
    assert list(adapter.iter_credit_totals(cc.RETIREMENTS)) == []


# -- end to end ------------------------------------------------------------


def test_a_full_pass_lands_in_the_sheet(adapter, conn):
    """Projects, both ledgers and the derivation rules, through to the export."""
    db.upsert_projects(conn, settings.CERCARBONO, adapter.iter_projects())
    for resource in cc.LEDGERS:
        db.upsert_credit_events(
            conn, settings.CERCARBONO, resource, adapter.iter_credits(resource)
        )
    db.upsert_credit_totals(
        conn, settings.CERCARBONO, cc.ISSUANCES,
        adapter.iter_credit_totals(cc.ISSUANCES),
    )

    rulesets = derive.load_rulesets()
    rows = []
    for project in db.all_projects(conn, settings.CERCARBONO):
        record = dict(project)
        for column, value, rule in derive.derive_for_project(record, rulesets):
            rows.append((settings.CERCARBONO, record["project_id"], column, value, rule))
    db.replace_derived(conn, rows)

    columns, sheet = excel.build_rows(conn, settings.CERCARBONO)
    assert len(sheet) == len(IN_CO2)
    by_id = {r["Project ID"]: r for r in sheet}
    assert by_id["CDC-271"]["Registry"] == "Cercarbono"
    assert by_id["CDC-271"]["Project URL"].startswith(settings.CERCARBONO_SITE)
    # Every row must be reachable from the sheet's own link column.
    assert all(r["Project URL"] for r in sheet)


def test_continent_is_derived_from_the_country_name(adapter):
    """Cercarbono publishes no ISO code, so the code rules cannot fire.

    Without a name-based table its Continent column would be empty for every
    row.
    """
    rulesets = derive.load_rulesets()
    values = dict(
        (column, value)
        for column, value, _rule in derive.derive_for_project(
            {"country_name": "Colombia"}, rulesets
        )
    )
    assert values["Continent"] == "South America"


def test_a_country_code_still_wins_over_the_name(adapter):
    """The code rules sit first on purpose: a stated code is authoritative."""
    rulesets = derive.load_rulesets()
    values = dict(
        (column, value)
        for column, value, _rule in derive.derive_for_project(
            {"country_code": "KE", "country_name": "Colombia"}, rulesets
        )
    )
    assert values["Continent"] == "Africa"


def test_colombian_land_use_projects_are_not_all_called_amazon():
    """140 of 231 Cercarbono projects are Colombian.

    The old country-level band placed every one of them in the Amazon basin.
    The Andes and the Caribbean coast are not the Amazon, and a project with
    no department stated says so rather than being placed anywhere.
    """
    rulesets = derive.load_rulesets()

    def biome(**row):
        row.setdefault("sectoral_scope", "Land use (AFOLU)")
        return dict(
            (column, value)
            for column, value, _rule in derive.derive_for_project(row, rulesets)
        ).get("Bioma")

    assert biome(country_name="Colombia", state_province="Antioquia") == "Andes Colombianos"
    assert biome(country_name="Colombia", state_province="Caqueta") == "Amazônia (bacia amazônica)"
    assert biome(country_name="Colombia", state_province="Cordoba").startswith("Caribe")
    assert biome(country_name="Colombia") == "Colômbia (bioma não determinado)"


def test_land_use_afolu_reaches_the_biome_rules():
    """Cercarbono's wording for the sector differs from Verra's and GS's.

    `applies_when` gates the whole ruleset, so an unrecognised wording means
    no biome for any row of that registry.
    """
    rulesets = derive.load_rulesets()
    biome = next(r for r in rulesets if r.column == "Bioma")
    assert biome.evaluate(
        {"sectoral_scope": "Land use (AFOLU)", "country_name": "Brazil"}
    ) is not None
    assert biome.evaluate(
        {"sectoral_scope": "Energy industries", "country_name": "Brazil"}
    ) is None
