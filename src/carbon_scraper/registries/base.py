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

import logging
import threading
from collections.abc import Iterable, Iterator
from typing import Any, Protocol, runtime_checkable

from ..http_client import RegistryClient

log = logging.getLogger(__name__)

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


class ClientOwner:
    """Owns an HTTP client only if it made one.

    Four adapters carried a byte-identical copy of this. It is not much code,
    but it is the code that decides whether a `with` block closes a client the
    caller still needs, and four copies is four places to get that wrong.
    """

    client: Any
    _owns_client: bool

    #: Requests per second, where this registry asks for fewer than the
    #: default. `RegistryClient` takes the minimum of this and
    #: `settings.REQUESTS_PER_SECOND`, so an adapter can only ever slow itself
    #: down — ACR is behind a Cloudflare rule that bans for an hour at ~100
    #: requests in ten minutes, and there is no version of "make it faster"
    #: that ends well.
    requests_per_second: float | None = None

    def _bind_client(
        self,
        client: RegistryClient | None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.client = client or RegistryClient(
            cancel=cancel, requests_per_second=self.requests_per_second
        )
        # An injected client belongs to the caller — closing it here would
        # shut a connection pool the pipeline is still paging through.
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self):  # noqa: ANN204 - the subclass's own type
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def reconciled(
    records: Iterable[Any],
    *,
    expected: int | None = None,
    progress: Any = None,
    max_records: int | None = None,
    label: str = "",
) -> Iterator[Any]:
    """Yield `records`, reporting progress and reconciling at the end.

    The four adapters each grew their own version of this loop and gave four
    slightly different answers. Two things in particular have to be the same
    everywhere:

    * **`progress` is cumulative.** Both sinks read it as an absolute
      position against a total — `done * 100 / total` in the window,
      `{done}/{total}` on the console. An adapter passing `1` per record
      pinned the bar at "1 of N" for a whole scrape.
    * **A short read is an error, not a quiet finish.** Never trust a row
      count just because nothing was raised.

    `expected=None` means the caller reconciles elsewhere (the S&P adapter
    does it per partition, where the counts actually are).
    """
    seen = 0
    for record in records:
        yield record
        seen += 1
        if progress is not None:
            progress(seen)
        if max_records is not None and seen >= max_records:
            return

    if expected is not None and seen < expected:
        log.error(
            "INCOMPLETE: %s yielded %s of %s records the registry reports.",
            label or "resource",
            seen,
            expected,
        )


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
