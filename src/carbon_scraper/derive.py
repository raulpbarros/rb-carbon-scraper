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
from datetime import datetime
from typing import Any

import yaml

from . import settings

log = logging.getLogger(__name__)


# -- rule engine -----------------------------------------------------------

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
        text = str(actual)
        if self.op == "equals":
            return text.strip().casefold() == str(self.value).strip().casefold()
        if self.op == "contains":
            return str(self.value).casefold() in text.casefold()
        if self.op == "in":
            values = {str(v).strip().casefold() for v in self.value}
            return text.strip().casefold() in values
        if self.op == "any_of":
            # e.g. a comma-separated field where any token matches
            tokens = {t.strip().casefold() for t in re.split(r"[,;/]", text)}
            return bool(tokens & {str(v).strip().casefold() for v in self.value})
        if self.op == "regex":
            return re.search(str(self.value), text, re.I) is not None
        if self.op == "not_empty":
            return True
        raise ValueError(f"Unknown operator '{self.op}' in derivation rules")


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


_OPERATORS = {"equals", "contains", "in", "any_of", "regex", "not_empty", "is_empty"}


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
    return rulesets


# -- computed values -------------------------------------------------------

def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


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
