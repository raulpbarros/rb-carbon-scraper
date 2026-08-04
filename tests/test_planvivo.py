"""Plan Vivo adapter and the Platts generalisation. No network.

Runs against `tests/fixtures/planvivo-*.json`, captured from the live API on
2026-07-28.

Two things are under test here, and they are different things:

* that Plan Vivo is correctly *identified* to the platform — the wrong
  `standardId` is answered with HTTP 200 and no rows, so nothing downstream
  would raise;
* that the Phase-2 refactor left Verra alone. `tests/test_api.py` pins the
  request shapes; the tests below pin that both registries still get their own
  identity, columns and URLs out of the shared base class.
"""

from __future__ import annotations

import json

import pytest

from carbon_scraper import db, derive, excel, settings
from carbon_scraper.registries import ADAPTERS, adapter_class, resolve
from carbon_scraper.registries.goldstandard.api import GoldStandardAPI
from carbon_scraper.registries.planvivo import api as pv
from carbon_scraper.registries.platts import api as platts
from carbon_scraper.registries.platts import discovery
from carbon_scraper.registries.verra import api as verra

from conftest import load_fixture as _load

@pytest.fixture(scope="module")
def projects():
    return _load("planvivo-projects.json")["entities"]


@pytest.fixture(scope="module")
def issuances():
    return _load("planvivo-issuances.json")["entities"]


@pytest.fixture(scope="module")
def holdings():
    return _load("planvivo-holdings.json")["entities"]


class FakeClient:
    """Serves the fixtures and records what was asked for. Never touches HTTP."""

    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.calls = []
        self.invalidated = []

    def post_json(self, url, body, *, headers=None, refresh=False):
        self.calls.append((url, body, headers or {}))
        for resource, payload in self.payloads.items():
            if f"/{resource}/" in url:
                return payload
        return {"entities": [], "totalEntities": 0}

    def base_uri(self, name, *, config_url=None):
        return f"https://api.example/{name}"

    def api_headers(self, config_url=None):
        return {"appkey": "public"}

    def invalidate(self, method, url, **kwargs):
        self.invalidated.append(url)

    def close(self):
        return None


# --- identity: the part that fails silently --------------------------------


def test_the_four_identity_headers_are_all_present():
    """Without all four, every call is a generic HTTP 500 that reads as a fault."""
    for header in ("registry", "standardid", "standardacronym", "language"):
        assert header in pv.REGISTRY_HEADERS, header
        assert pv.REGISTRY_HEADERS[header], header


def test_plan_vivo_sends_its_own_registry_and_standard_not_verras():
    """A wrong-but-plausible standardId answers HTTP 200 with zero rows.

    That is indistinguishable from an empty registry: no exception, no log
    line, no reason. The values below were read from the platform's own
    `standardsByRegistry` lookup, which is the only published source.
    """
    assert pv.REGISTRY_HEADERS["registry"] == "PVCL"
    assert pv.REGISTRY_HEADERS["standardid"] == "671000000000001"
    assert pv.REGISTRY_HEADERS["standardacronym"] == "PV"

    # And Verra's are untouched by the refactor.
    assert verra.REGISTRY_HEADERS["registry"] == "VERRA"
    assert verra.REGISTRY_HEADERS["standardid"] == "150000000000001"
    assert verra.REGISTRY_HEADERS["standardacronym"] == "VCS"


def test_no_projects_at_all_is_reported_as_a_wrong_standard_id(caplog):
    """`totalEntities: 0` looks exactly like an empty registry, and is not.

    No tenant on this platform publishes zero projects — an empty *ledger*
    is a fact (JNR has four of them), an empty project list is a
    misconfigured adapter. The wrong answer is also a 200, so it is cached
    and would be replayed for 24 hours; the entry has to go with it.
    """
    client = FakeClient()  # every search answers totalEntities: 0
    adapter = pv.PlanVivoAPI(client)
    with caplog.at_level("ERROR"):
        assert adapter.project_total() == 0
    assert "published no projects at all" in caplog.text
    assert "wrong standardId" in caplog.text
    assert client.invalidated, "the 200 that said 'empty' is still in the cache"


def test_a_registry_with_projects_says_nothing(projects, caplog):
    client = FakeClient({"project": {"entities": projects, "totalEntities": 2}})
    with caplog.at_level("ERROR"):
        assert pv.PlanVivoAPI(client).project_total() == 2
    assert client.invalidated == []
    assert caplog.text == ""


def test_the_two_platts_registries_do_not_share_an_identity():
    """One base class, two registries: nothing may leak between them."""
    for attribute in ("registry", "registry_code", "standard_id", "standard_acronym",
                      "site", "config_url", "detail_url_template", "slug"):
        assert getattr(pv.PlanVivoAPI, attribute) != getattr(verra.VerraAPI, attribute), (
            attribute
        )


def test_each_registry_reads_its_own_routing_table():
    """Both sites publish the identical table today; that is not a guarantee.

    Reading each registry's own `/config/environment.config.json` is what lets
    S&P split or move a backend without breaking us.
    """
    assert verra.VerraAPI.config_url.startswith(settings.SITE)
    assert pv.PlanVivoAPI.config_url.startswith(settings.PV_SITE)


def test_request_headers_carry_this_registrys_origin():
    """Cercarbono's lesson: a shared Origin is not free — it is validated."""
    client = FakeClient()
    headers = pv.PlanVivoAPI(client=client)._headers()
    assert headers["Origin"] == settings.PV_SITE
    assert headers["Referer"].startswith(settings.PV_SITE)
    assert headers["registry"] == "PVCL"


# --- the shared platform contract ------------------------------------------


def test_plan_vivo_inherits_every_measured_platform_constraint():
    assert platts.MAX_LIMIT == 400
    assert platts.MAX_WINDOW == 10_000
    assert platts.STABLE_SORT == [{"sort": "entityId", "dir": "ASC"}]
    assert issubclass(pv.PlanVivoAPI, platts.PlattsAPI)
    assert issubclass(verra.VerraAPI, platts.PlattsAPI)


def test_paging_always_sorts_by_entity_id():
    """Without a stable sort Elasticsearch reshuffles between pages.

    That silently dropped 1,271 of 5,244 Verra projects once. The sort has to
    survive the move into the shared base class.
    """
    client = FakeClient({"project": _load("planvivo-projects.json")})
    api = pv.PlanVivoAPI(client=client)
    list(api.iter_records(platts.PROJECT))
    drains = [body for _url, body, _h in client.calls
              if body["searchFilter"]["pagination"].get("sortOptions")]
    assert drains, "no paged read used a sort"
    for body in drains:
        assert body["searchFilter"]["pagination"]["sortOptions"] == platts.STABLE_SORT


def test_ledgers_match_what_the_registry_publishes_publicly():
    """Decided from the platform's own `publicReportExport`, not copied blindly.

    Plan Vivo happens to expose the same four as Verra. `retirements` and
    `cancellations` are empty today and are scraped anyway — an empty ledger
    is a fact about the registry, not a reason to leave it unwired.
    """
    assert pv.PlanVivoAPI.ledgers == ("issuances", "holdings", "retirements", "cancellations")


# --- normalisation ---------------------------------------------------------


def test_project_normalises_onto_the_shared_columns(projects):
    row = pv.normalize_project(projects[0])
    assert set(row) <= set(db.PROJECT_FIELDS)
    assert row["project_id"] == projects[0]["projectId"]
    assert row["project_name"] == projects[0]["projectName"]
    assert row["country_name"] == projects[0]["countryName"]


def test_tipo_macro_is_project_type_because_sectoral_scope_is_null(projects):
    """Plan Vivo states `projectType`; `sectoralScope` is null throughout.

    Carried through untranslated, per CLAUDE.md — "Afforestation /
    Reforestation" is Plan Vivo's own wording and stays that way.
    """
    for record in projects:
        assert record["sectoralScope"] is None
        assert record["projectType"]
        assert pv.normalize_project(record)["sectoral_scope"] == record["projectType"]


def test_verras_column_map_is_unchanged_by_plan_vivos(projects):
    """The two maps must not shadow each other through the shared base."""
    assert verra.VerraAPI.project_columns["sectoralScope"] == "sectoral_scope"
    assert "sectoralScope" not in pv.PlanVivoAPI.project_columns
    assert pv.PlanVivoAPI.project_columns["projectType"] == "sectoral_scope"


def test_no_platform_field_maps_onto_the_same_column_twice():
    """Two fields sharing a column lets a null overwrite a real value.

    Dict order would decide which won, which is not a thing to leave to luck.
    """
    for cls in (verra.VerraAPI, pv.PlanVivoAPI):
        columns = list(cls.project_columns.values())
        assert len(columns) == len(set(columns)), cls.__name__


def test_project_id_falls_back_to_the_numeric_id(projects, conn):
    """Plan Vivo publishes no human reference — `vcsProjectId` is Verra's.

    Nothing is invented: the numeric id is what the registry's own public URL
    uses, so the cell stays checkable.
    """
    assert "vcsProjectId" not in pv.PlanVivoAPI.project_columns
    row = pv.normalize_project(projects[0])
    assert row.get("external_id") is None

    db.upsert_projects(conn, settings.PLAN_VIVO, [(row, projects[0])])
    _, rows = excel.build_rows(conn, settings.PLAN_VIVO)
    assert rows[0]["Project ID"] == projects[0]["projectId"]


def test_region_is_not_published_so_continent_comes_from_the_country(projects):
    """Same path Cercarbono takes: no region, no ISO code, only a country name."""
    rulesets = derive.load_rulesets()
    for record in projects:
        row = pv.normalize_project(record)
        assert row["region_name"] is None
        assert row.get("country_code") is None
        values = {c: v for c, v, _rule in derive.derive_for_project(row, rulesets)}
        assert values["Continent"] in ("Africa", "North America")


def test_detail_url_points_at_plan_vivos_own_public_page(projects):
    row = pv.normalize_project(projects[0])
    assert row["detail_url"] == (
        f"{settings.PV_SITE}/pvclimate/public/pv/projects/{projects[0]['projectId']}"
    )
    # Never another registry's page: a link that resolves to the wrong
    # registry looks right and is worse than no link at all.
    assert settings.SITE not in row["detail_url"]


# --- credit records --------------------------------------------------------


def test_credit_normalises_onto_the_shared_columns(issuances):
    row = pv.normalize_credit(issuances[0])
    assert set(row) <= set(db.CREDIT_EVENT_FIELDS)
    assert row["project_id"] == issuances[0]["projectId"]
    assert row["quantity"] == issuances[0]["holdingQuantity"]
    assert row["serial_no"] == issuances[0]["serialNo"]


def test_reserve_classes_stay_visible_in_unit_type(issuances):
    """`unitType` alone cannot see the reserves.

    Plan Vivo issues `rPVC`/`fPVC` alongside two reserve classes — Achievement
    Reserve and Future Risk Buffer — and `unitType` reads the same for all of
    them. Recording `unitClass` keeps the split available without a re-scrape,
    exactly as Cercarbono's buffer flag does.
    """
    classes = {pv.normalize_credit(r)["unit_type"] for r in issuances}
    assert {"Achievement Reserve", "Future Risk Buffer"} <= classes
    assert any(c in classes for c in ("rPVC", "fPVC"))


def test_holdings_spell_the_unit_class_differently(holdings):
    """Issuances say `unitClass`, holdings say `unitClassName`. Both are read."""
    for record in holdings:
        assert record.get("unitClass") is None
        assert pv.normalize_credit(record)["unit_type"] == record["unitClassName"]


def test_verra_still_reads_the_bare_unit_type():
    """The Plan Vivo change must not reach Verra's records."""
    assert verra.VerraAPI.unit_type_fields == ("unitType",)
    row = verra.normalize_credit({"entityId": 1, "unitType": "VCU", "unitClass": "Buffer"})
    assert row["unit_type"] == "VCU"


def test_issued_total_includes_the_reserves(conn, projects, issuances):
    """The registry's own "Issued Units" figure counts every class.

    Filtering the reserves out would disagree with the number on Plan Vivo's
    own public page, which is what the business reconciles against.
    """
    db.upsert_projects(
        conn, settings.PLAN_VIVO, [(pv.normalize_project(r), r) for r in projects]
    )
    db.upsert_credit_events(
        conn, settings.PLAN_VIVO, platts.ISSUANCES,
        [pv.normalize_credit(r) for r in issuances],
    )
    totals = db.credit_totals(conn)
    issued = sum(t.get("issuances", 0) for (registry, _pid), t in totals.items()
                 if registry == settings.PLAN_VIVO)
    assert issued == sum(r["holdingQuantity"] for r in issuances)


def test_ledger_names_are_not_the_gold_standard_magic_string():
    """`db.credit_totals()` branches on the exact string 'credits'.

    That name selects Gold Standard's bucket-by-status semantics. Plan Vivo has
    one ledger per event type, like Verra, so it must not use it.
    """
    assert "credits" not in pv.PlanVivoAPI.ledgers


def test_no_cancellation_or_retirement_figure_is_invented(conn, projects):
    """Both ledgers are genuinely empty today. Empty means blank, not zero."""
    db.upsert_projects(
        conn, settings.PLAN_VIVO, [(pv.normalize_project(r), r) for r in projects]
    )
    _, rows = excel.build_rows(conn, settings.PLAN_VIVO)
    for row in rows:
        assert row["Total Credits Retired"] is None
        assert row["Total Credits Cancelled"] is None
        # Not published by this registry at all.
        assert row["Yearly Ex Ante"] is None
        assert row["Metodologia"] is None


# --- derivation ------------------------------------------------------------


def test_plan_vivos_wording_reaches_the_biome_gate():
    """`biome.yaml`'s applies_when is load-bearing and fails silently.

    One unrecognised sector wording means no biome for any row of that
    registry, with nothing in the log to say so.
    """
    rulesets = derive.load_rulesets()
    row = {
        "sectoral_scope": "Afforestation / Reforestation",
        "country_name": "Mozambique",
    }
    values = {c: v for c, v, _rule in derive.derive_for_project(row, rulesets)}
    assert values["Bioma"]
    assert values["Tipo Micro de Projeto"].startswith("Florestamento")
    assert values["Durabilidade"].startswith("Média")


def test_the_afforestation_rule_cannot_reach_another_registrys_rows():
    """`equals`, not `contains`: Verra and Cercarbono scopes are long strings."""
    rulesets = derive.load_rulesets()
    for scope, country in (
        ("Agriculture Forestry and Other Land Use (AFOLU)", "Brazil"),
        ("Land use (AFOLU)", "Colombia"),
    ):
        values = {
            c: (v, r)
            for c, v, r in derive.derive_for_project(
                {"sectoral_scope": scope, "country_name": country}, rulesets
            )
        }
        assert values["Tipo Micro de Projeto"][1] != "pv-afforestation"


# --- wiring ----------------------------------------------------------------


def test_plan_vivo_is_reachable_by_every_alias():
    for name in ("pv", "planvivo", "plan-vivo", "pvcl", "PLAN_VIVO"):
        assert resolve(name) == settings.PLAN_VIVO
    assert settings.PLAN_VIVO in ADAPTERS
    assert adapter_class(settings.PLAN_VIVO) is pv.PlanVivoAPI


def test_every_registry_has_a_label_and_a_detail_url_template():
    """The GUI builds its checkboxes from these, so a gap is a missing feature."""
    for name in ADAPTERS:
        assert settings.REGISTRY_LABELS.get(name), name
        assert settings.PROJECT_DETAIL_URLS.get(name), name


def test_discover_refuses_a_registry_that_is_not_on_the_platform():
    """`discover` drives an S&P page. Pointing it at a REST registry is a bug."""
    assert discovery.target(settings.PLAN_VIVO) is pv.PlanVivoAPI
    assert discovery.target(settings.VERRA) is verra.VerraAPI
    with pytest.raises(ValueError, match="Platts"):
        discovery.target(settings.GOLD_STANDARD)


def test_capture_paths_never_collide_with_the_handwritten_contract():
    """A `discover` run must not overwrite the doc a human keeps current."""
    verra_md, verra_json = settings.api_contract_paths("verra")
    pv_md, pv_json = settings.api_contract_paths("planvivo")
    assert verra_md != pv_md and verra_json != pv_json
    assert pv_md.name != "api-contract-planvivo.md"
    assert (settings.DOCS_DIR / "api-contract-planvivo.md").exists()


def test_a_capture_never_overwrites_another_registrys_fixture(tmp_path, monkeypatch):
    """Two registries on one platform share every role name.

    Unprefixed filenames would let a Plan Vivo capture land on top of Verra's
    fixture, and one of the two test suites would quietly become a lie.
    """
    monkeypatch.setattr(settings, "FIXTURES_DIR", tmp_path)
    call = discovery.RecordedCall(
        method="POST",
        url="https://x/project/publicReportPageSearch",
        role="project_search",
        request_headers={},
        item_count=2,
    )
    discovery._save_fixture("verra", call, {"entities": [{"projectId": 1}, {"projectId": 2}]})
    discovery._save_fixture("planvivo", call, {"entities": [{"projectId": 9}]})

    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == ["planvivo-project_search.json", "verra-project_search.json"]
    kept = json.loads((tmp_path / "verra-project_search.json").read_text(encoding="utf-8"))
    assert len(kept["entities"]) == 2


def test_the_rest_adapters_still_load_after_the_refactor():
    """Moving the Platts code must not disturb the registries that are not on it."""
    assert GoldStandardAPI.ledgers == ("credits",)
    assert not issubclass(GoldStandardAPI, platts.PlattsAPI)
