"""What every registry adapter must provide.

The pipeline (`cli.sync`) only ever talks to this interface, so adding a third
registry means writing one adapter — not touching db, derive or excel.

Two rules hold for every adapter, and both are lessons paid for on Verra:

* **Normalise, don't leak.** `iter_projects` yields (row, raw) where `row`
  uses `db.PROJECT_FIELDS` names. The database never learns a registry's own
  JSON field names.
* **Reconcile.** Every adapter reports `project_total()` and, where the API
  offers one, a credit total. A finished run that returned fewer records than
  the registry claims must log `INCOMPLETE` — never trust a row count just
  because no exception was raised.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

# (normalised_row, raw_payload)
ProjectRecord = tuple[dict[str, Any], dict[str, Any]]


@runtime_checkable
class RegistryAdapter(Protocol):
    """The contract `cli.sync` drives."""

    #: Value stored in every `registry` column; matches settings.VERRA etc.
    registry: str

    #: Credit resources this registry exposes, in the order they are scraped.
    ledgers: tuple[str, ...]

    def close(self) -> None: ...

    def __enter__(self) -> RegistryAdapter: ...

    def __exit__(self, *exc: object) -> None: ...

    def project_total(self) -> int:
        """How many projects the registry itself says it has."""

    def count(self, resource: str) -> int:
        """How many records the registry says `resource` holds."""

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[ProjectRecord]:
        """Yield every project, already normalised to db.PROJECT_FIELDS."""

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every credit record, normalised to db.CREDIT_EVENT_FIELDS."""

    def detail_url(self, project_id: int) -> str:
        """Public page for one project, so any row can be spot-checked."""


def credit_totals_of(
    adapter: Any, resource: str
) -> Iterator[tuple[int, float, Any]] | None:
    """Optional: per-project totals the registry states itself.

    Not part of the protocol above, because most registries do not publish
    one. An adapter that does implements `iter_credit_totals(resource)`
    yielding `(project_id, quantity, event_count)`; those land in
    `credit_totals`, which outranks summing `credit_events` rows.

    Worth having wherever the ledger is not guaranteed complete. Cercarbono is
    the case in point: its bulk credit feed omits projects converted in from
    another registry, so summing its rows reports zero issued credits for two
    projects that have plainly retired some.
    """
    method = getattr(adapter, "iter_credit_totals", None)
    if method is None:
        return None
    return method(resource)
