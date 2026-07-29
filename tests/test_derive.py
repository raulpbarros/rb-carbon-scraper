"""Derivation rules — the layer most likely to be wrong, so test it hardest.

No network. These run against the real YAML in config/derivation/.
"""

from __future__ import annotations

import pytest

from carbon_scraper import derive


@pytest.fixture(scope="module")
def rulesets():
    return derive.load_rulesets()


def _value_for(rulesets, column, row):
    for ruleset in rulesets:
        if ruleset.column == column:
            result = ruleset.evaluate(row)
            return result[0] if result else None
    raise AssertionError(f"No ruleset defines column {column!r}")


# -- Tipo Micro ------------------------------------------------------------

@pytest.mark.parametrize(
    "afolu, expected_fragment",
    [
        ("ARR", "ARR"),
        ("REDD", "REDD"),
        ("IFM", "IFM"),
        ("ALM", "ALM"),
        ("WRC", "WRC"),
        ("ACoGS", "ACoGS"),
    ],
)
def test_afolu_subtypes_drive_tipo_micro(rulesets, afolu, expected_fragment):
    row = {"afolu_names": afolu, "sectoral_scope": "Agriculture Forestry and Other Land Use"}
    assert expected_fragment in _value_for(rulesets, "Tipo Micro de Projeto", row)


def test_multi_valued_afolu_matches_first_rule(rulesets):
    # Verra emits comma-separated codes like "ARR,REDD,WRC".
    row = {"afolu_names": "ARR,REDD,WRC", "sectoral_scope": "Agriculture Forestry"}
    assert "ARR" in _value_for(rulesets, "Tipo Micro de Projeto", row)


def test_sectoral_scope_fallback(rulesets):
    row = {"sectoral_scope": "Energy industries (renewable/non-renewable sources)"}
    assert _value_for(rulesets, "Tipo Micro de Projeto", row) == "Energia Renovável"


def test_methodology_beats_scope(rulesets):
    """Cookstoves are 'Energy demand' by scope but deserve the finer label."""
    row = {"sectoral_scope": "Energy demand", "methodologies": "AMS-II.G. (Version NA)"}
    assert _value_for(rulesets, "Tipo Micro de Projeto", row) == "Fogões Eficientes"


def test_no_match_returns_none(rulesets):
    assert _value_for(rulesets, "Tipo Micro de Projeto", {"sectoral_scope": "Nonsense"}) is None


# -- Bioma -----------------------------------------------------------------

def test_biome_only_applies_to_afolu(rulesets):
    """A wind farm in Brazil has no meaningful biome; it must stay blank."""
    row = {
        "sectoral_scope": "Energy industries (renewable/non-renewable sources)",
        "country_name": "Brazil",
        "state_province": "Bahia",
    }
    assert _value_for(rulesets, "Bioma", row) is None


def test_biome_brazil_states(rulesets):
    base = {"sectoral_scope": "Agriculture Forestry and Other Land Use", "country_name": "Brazil"}
    assert _value_for(rulesets, "Bioma", {**base, "state_province": "Pará"}) == "Amazônia"
    assert _value_for(rulesets, "Bioma", {**base, "state_province": "Rio Grande do Sul"}) == "Pampa"
    assert _value_for(rulesets, "Bioma", {**base, "state_province": "São Paulo"}) == "Mata Atlântica"


def test_biome_unknown_brazil_state_is_flagged_not_guessed(rulesets):
    row = {
        "sectoral_scope": "Agriculture Forestry",
        "country_name": "Brazil",
        "state_province": "",
    }
    assert "não determinado" in _value_for(rulesets, "Bioma", row)


# -- Durabilidade ----------------------------------------------------------

def test_durability_distinguishes_removal_from_reduction(rulesets):
    removal = {"sectoral_scope": "Agriculture Forestry", "afolu_names": "ARR"}
    reduction = {"sectoral_scope": "Energy industries (renewable)"}
    assert "reversível" in _value_for(rulesets, "Durabilidade", removal)
    assert "redução de emissões" in _value_for(rulesets, "Durabilidade", reduction)


# -- computed values -------------------------------------------------------

def test_duration_from_crediting_period():
    row = {
        "credit_period_start": "2020-09-25T00:00:00",
        "credit_period_end": "2060-09-24T00:00:00",
    }
    assert derive.duration_years(row) == 40


def test_duration_falls_back_to_verra_credit_period_field():
    assert derive.duration_years({"credit_period": "100"}) == 100


def test_duration_rejects_absurd_values():
    assert derive.duration_years({"credit_period": "9999"}) is None
    assert derive.duration_years({}) is None


def test_total_ex_ante_is_yearly_times_duration():
    assert derive.total_ex_ante({"avg_annual_vol_vcu": 1000}, 40) == 40000


def test_total_ex_ante_blank_without_inputs():
    """No duration means no total — never a half-computed number."""
    assert derive.total_ex_ante({"avg_annual_vol_vcu": 1000}, None) is None
    assert derive.total_ex_ante({"avg_annual_vol_vcu": None}, 40) is None


def test_derive_for_project_tags_every_value_with_its_rule():
    rulesets = derive.load_rulesets()
    row = {
        "sectoral_scope": "Agriculture Forestry and Other Land Use",
        "afolu_names": "ARR",
        "country_name": "Brazil",
        "state_province": "Pará",
        "credit_period_start": "2020-01-01T00:00:00",
        "credit_period_end": "2050-01-01T00:00:00",
        "avg_annual_vol_vcu": 500,
    }
    results = derive.derive_for_project(row, rulesets)
    columns = {column: (value, rule) for column, value, rule in results}

    assert columns["Duração"][0] == 30
    assert columns["Total Ex Ante"][0] == 15000
    assert columns["Bioma"][0] == "Amazônia"
    # Every derived value must name the rule that produced it.
    assert all(rule for _, (_, rule) in columns.items())
