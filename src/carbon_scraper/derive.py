"""Derived columns: the fields Verra does not publish.

Two distinct kinds of value are produced here, and the difference matters:

* **Computed** — arithmetic on real Verra fields (duration from the crediting
  period, total ex ante from the yearly figure). These are reliable.
* **Classified** — rule matches from `config/derivation/*.yaml` (Tipo Micro,
  Bioma, Durabilidade). These are *informed guesses awaiting business
  validation*. Every one records the rule that produced it in
  `project_derived.rule_name` so a wrong call is traceable and fixable by
  editing YAML, with no re-scrape.

Nothing here invents a value it cannot support: no rule match means the cell
stays empty.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import yaml

from . import db, settings

log = logging.getLogger(__name__)


# -- rule engine -----------------------------------------------------------

def _equals(text: str, value: Any) -> bool:
    return text.strip().casefold() == str(value).strip().casefold()


def _contains(text: str, value: Any) -> bool:
    return str(value).casefold() in text.casefold()


def _in(text: str, value: Any) -> bool:
    return text.strip().casefold() in {str(v).strip().casefold() for v in value}


def _any_of(text: str, value: Any) -> bool:
    """A comma-separated field where any token matches."""
    tokens = {t.strip().casefold() for t in re.split(r"[,;/]", text)}
    return bool(tokens & {str(v).strip().casefold() for v in value})


def _regex(text: str, value: Any) -> bool:
    return re.search(str(value), text, re.I) is not None


def _not_empty(text: str, value: Any) -> bool:
    return True


#: Operators that run against a *present* value. `is_empty` is not here: it is
#: the one operator that has to see an absent value, so it is handled before
#: the emptiness check rather than after it.
_MATCHERS = {
    "equals": _equals,
    "contains": _contains,
    "in": _in,
    "any_of": _any_of,
    "regex": _regex,
    "not_empty": _not_empty,
}


@dataclass
class Condition:
    field: str
    op: str
    value: Any

    def matches(self, row: dict[str, Any]) -> bool:
        actual = row.get(self.field)
        if self.op == "is_empty":
            return actual in (None, "")
        if actual in (None, ""):
            return False
        # `_parse_conditions` has already rejected anything not in the table,
        # so this is a lookup rather than a chain of seven string comparisons
        # re-run for every condition of every rule of every project.
        return _MATCHERS[self.op](str(actual), self.value)


@dataclass
class Rule:
    name: str
    value: str
    conditions: list[Condition]

    def matches(self, row: dict[str, Any]) -> bool:
        return all(c.matches(row) for c in self.conditions)


@dataclass
class RuleSet:
    column: str
    applies_when: list[Condition]
    rules: list[Rule]
    note: str = ""

    def evaluate(self, row: dict[str, Any]) -> tuple[str, str] | None:
        """Return (value, rule_name) for the first matching rule, else None."""
        if not all(c.matches(row) for c in self.applies_when):
            return None
        for rule in self.rules:
            if rule.matches(row):
                return rule.value, rule.name
        return None


#: Every operator a YAML rule may name. Derived from the dispatch table, so a
#: new matcher cannot be added without becoming valid, or validated without
#: being implemented.
_OPERATORS = {*_MATCHERS, "is_empty"}


def _parse_conditions(raw: list[dict[str, Any]] | None) -> list[Condition]:
    conditions: list[Condition] = []
    for entry in raw or []:
        field = entry.get("field")
        if not field:
            raise ValueError(f"Condition missing 'field': {entry}")
        ops = [k for k in entry if k in _OPERATORS]
        if len(ops) != 1:
            raise ValueError(f"Condition needs exactly one operator, got {ops}: {entry}")
        conditions.append(Condition(field=field, op=ops[0], value=entry[ops[0]]))
    return conditions


def load_rulesets(directory: Any = None) -> list[RuleSet]:
    """Load every ruleset in `directory`.

    Raises if it finds none. An empty list is not a degraded run, it is a
    silent one: every classified column would come back blank, `derive` would
    report success, and the sheet would build with the right shape and four
    empty columns. That is the failure this codebase keeps meeting, so it
    gets an exception rather than a warning nobody reads.
    """
    directory = directory or settings.derivation_dir()
    rulesets: list[RuleSet] = []
    for path in sorted(directory.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        column = spec.get("column")
        if not column:
            log.warning("%s has no 'column'; skipping", path.name)
            continue
        rules = [
            Rule(
                name=r.get("name") or f"{path.stem}:{i}",
                value=r["value"],
                conditions=_parse_conditions(r.get("match")),
            )
            for i, r in enumerate(spec.get("rules") or [])
        ]
        rulesets.append(
            RuleSet(
                column=column,
                applies_when=_parse_conditions(spec.get("applies_when")),
                rules=rules,
                note=spec.get("note", ""),
            )
        )
    if not rulesets:
        raise FileNotFoundError(
            f"No derivation rulesets found in {directory}. Every classified "
            "column would silently be blank; refusing to derive."
        )
    return rulesets


# -- computed values -------------------------------------------------------

#: `db.parse_date` under the name this module has always used for it. Shared,
#: because `excel` reads the same stored columns: a date format that parses for
#: `Data de Início` and not for `Duração` is a gap nobody would look for.
_parse_date = db.parse_date


def duration_years(row: dict[str, Any]) -> int | None:
    """Crediting-period length in whole years."""
    start = _parse_date(row.get("credit_period_start")) or _parse_date(row.get("project_start_date"))
    end = _parse_date(row.get("credit_period_end")) or _parse_date(row.get("project_end_date"))
    if start and end and end > start:
        return round((end - start).days / 365.25)
    # Verra's own `creditPeriod` field, when present, is already in years.
    raw = row.get("credit_period")
    try:
        years = int(float(str(raw)))
        return years if 0 < years <= 200 else None
    except (TypeError, ValueError):
        return None


def total_ex_ante(row: dict[str, Any], years: int | None) -> float | None:
    """Total ex ante = yearly estimate x crediting-period length.

    Verra's `exanteQuantity` is null throughout the public index, so the total
    has to be built from the yearly figure it does publish.
    """
    yearly = row.get("avg_annual_vol_vcu")
    if yearly in (None, "") or not years:
        return None
    try:
        return float(yearly) * years
    except (TypeError, ValueError):
        return None


# -- orchestration ---------------------------------------------------------

def derive_for_project(
    row: dict[str, Any], rulesets: list[RuleSet]
) -> list[tuple[str, Any, str]]:
    """Return [(column_name, value, rule_name)] for one project."""
    out: list[tuple[str, Any, str]] = []

    years = duration_years(row)
    if years is not None:
        out.append(("Duração", years, "computed:crediting-period-years"))

    total = total_ex_ante(row, years)
    if total is not None:
        out.append(("Total Ex Ante", round(total), "computed:yearly-x-duration"))

    for ruleset in rulesets:
        result = ruleset.evaluate(row)
        if result is not None:
            value, rule_name = result
            out.append((ruleset.column, value, rule_name))

    return out
