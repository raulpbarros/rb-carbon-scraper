"""Client for the Gold Standard Impact Registry's public API.

Contract measured against the live service on 2026-07-28; see
docs/api-contract-gs.md. It is a plain unauthenticated REST API — far simpler
than Verra's — but it has traps of its own, all of them measured:

    GET https://public-api.goldstandard.org/projects?page=1&size=150
    GET https://public-api.goldstandard.org/credits?page=1&size=25

* **A browser `User-Agent` is required.** Without one the edge returns a
  Cloudflare 403 HTML page. `settings.BROWSER_HEADERS` supplies it.
* **`size` is capped per resource and the cap is silent.** `projects` clamps
  to 150 — ask for 1000 and you still get 150, with no error and no hint in
  the body, so a naive `size=1000` loop would skip 85% of the registry.
  `credits` is harder: anything above 25 is refused with a *403*, which reads
  like a block but is a response-size limit. Both caps live in settings.
* **Filters that are not understood are silently ignored**, exactly as on
  Verra. `?project_id=1890` and `?status=RETIRED` both return the entire
  182,989-record index with an unchanged `X-Total-Count`. Only `query` was
  proven to narrow. Never partition on an unproven filter.
* Counts come back in headers, not the body: `X-Total-Count` and
  `X-Total-Number-Of-Credits`. Both are used to reconcile every run.
* Results are ordered by descending `id` and stay stable across pages, so
  plain paging is safe — there is no Elasticsearch window to work around and
  deep pages (tested at page 7000) return normally.

A credit block embeds its whole `project` object, so the credit stream carries
its own project linkage and needs no per-project fan-out.

`status` on a credit block is its *current* state, not an event type: a block
that was issued and later retired reads `RETIRED`. Issued totals are therefore
the sum of every block regardless of status — see db.credit_totals().
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any

from ... import db, settings
from ...http_client import RegistryClient
from ..base import ClientOwner, reconciled

log = logging.getLogger(__name__)

REGISTRY = settings.GOLD_STANDARD

PROJECT = "projects"
CREDITS = "credits"

#: Gold Standard publishes one credit stream, not Verra's four ledgers.
LEDGERS = (CREDITS,)

PAGE_SIZES = {
    PROJECT: settings.GS_PROJECT_PAGE_SIZE,
    CREDITS: settings.GS_CREDIT_PAGE_SIZE,
}

TOTAL_HEADER = "x-total-count"
CREDIT_TOTAL_HEADER = "x-total-number-of-credits"


class GoldStandardAPI(ClientOwner):
    """Talks to the public registry API. Knows nothing about our database.

    Implements registries.base.RegistryAdapter.
    """

    registry = REGISTRY
    ledgers = LEDGERS

    def __init__(
        self,
        client: RegistryClient | None = None,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self._bind_client(client, cancel)

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # A browser User-Agent is not cosmetic here; without it the edge
        # answers 403 for every call.
        return dict(settings.BROWSER_HEADERS)

    def page(
        self, resource: str, page: int, *, size: int | None = None, refresh: bool = False
    ) -> tuple[list[dict[str, Any]], Any]:
        """One page of `resource`, plus the response headers holding the counts."""
        body, headers = self.client.get_json_with_headers(
            f"{settings.GS_API}/{resource}",
            params={"page": page, "size": size or PAGE_SIZES.get(resource, 25)},
            headers=self._headers(),
            refresh=refresh,
        )
        return (body if isinstance(body, list) else []), headers

    def count(self, resource: str) -> int:
        """How many records the registry says `resource` holds."""
        _, headers = self.page(resource, 1, size=1)
        return int(headers.get(TOTAL_HEADER) or 0)

    def project_total(self) -> int:
        return self.count(PROJECT)

    def credit_total(self) -> int:
        """Registry-wide sum of credits, straight from the header.

        An independent cross-check on locally aggregated issuance totals.
        """
        _, headers = self.page(CREDITS, 1, size=1)
        return int(headers.get(CREDIT_TOTAL_HEADER) or 0)

    def detail_url(self, project_id: int) -> str:
        return settings.GS_PROJECT_DETAIL_URL.format(project_id=project_id)

    # -- bulk iteration ----------------------------------------------------

    def iter_raw(
        self,
        resource: str,
        *,
        max_records: int | None = None,
        progress: Any = None,
    ) -> Iterator[dict[str, Any]]:
        """Page through `resource` and shout if the registry's count disagrees.

        Ordering is by descending id and stable between pages, so a plain
        walk is safe. The reconciliation at the end is the point: a run that
        ends without an exception still has to prove it saw everything.
        """
        yield from reconciled(
            self._paged(resource),
            expected=self.count(resource),
            progress=progress,
            max_records=max_records,
            label=resource,
        )

    def _paged(self, resource: str) -> Iterator[dict[str, Any]]:
        """Every record of `resource`, in page order.

        `size` caps differ per resource and lie in different ways: `projects`
        clamps 1000 to 150 with no error and no marker, `credits` refuses
        anything above 25 with a 403. `PAGE_SIZES` holds the measured values,
        and a page shorter than the one asked for is the end of the feed.
        """
        size = PAGE_SIZES.get(resource, 25)
        page = 1
        while True:
            records, _ = self.page(resource, page)
            if not records:
                return
            yield from records
            if len(records) < size:
                return
            page += 1

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        for record in self.iter_raw(
            PROJECT, max_records=max_records, progress=progress
        ):
            yield normalize_project(record), record

    def iter_credits(
        self, resource: str = CREDITS, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        for record in self.iter_raw(
            resource, max_records=max_records, progress=progress
        ):
            yield normalize_credit(record)


# -- normalisation ---------------------------------------------------------
# Gold Standard's field names -> the registry-neutral db.PROJECT_FIELDS.
#
# Deliberate gaps, decided with the user — see CLAUDE.md:
#   * `city` — Gold Standard publishes no city. Left blank, never guessed.
#   * `methodologies` — carried through, but usually null upstream.
#   * `sectoral_scope` (Tipo Macro) — the raw `type` value, NOT translated
#     into Verra's sectoral-scope vocabulary. The two taxonomies sit side by
#     side in the sheet until the business decides how to relate them.

PROJECT_COLUMNS: dict[str, str] = {
    "name": "project_name",
    "gsf_standards_version": "standard_name",
    "status": "status",
    "type": "sectoral_scope",
    "methodology": "methodologies",
    "size": "project_size",
    "project_developer": "proponents",
    "country": "country_name",
    "country_code": "country_code",
    "state": "state_province",
    "crediting_period_start_date": "credit_period_start",
    "crediting_period_end_date": "credit_period_end",
    "estimated_annual_credits": "avg_annual_vol_vcu",
    "sustaincert_url": "detail_url",
}

# Kept as JSON in `projects.extra`: real registry data with no column of its
# own, so it stays queryable without inventing schema for it.
EXTRA_FIELDS = (
    "carbon_stream",
    "labels",
    "latitude",
    "longitude",
    "programme_of_activities",
    "poa_project_id",
    "poa_project_name",
    "sustainable_development_goals",
    "description",
)


def external_id(record: dict[str, Any]) -> str | None:
    """The reference the registry shows to humans, e.g. `GS7495`.

    `sustaincert_id` is the canonical one: a project's own `name` sometimes
    carries a different GS number (its parent programme's), so the name is not
    a safe source for this.
    """
    value = record.get("sustaincert_id")
    return f"GS{value}" if value not in (None, "") else None


def normalize_project(record: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        column: record.get(field) for field, column in PROJECT_COLUMNS.items()
    }
    row["project_id"] = db.int_or_none(record.get("id"))
    row["external_id"] = external_id(record)
    extra = {f: record.get(f) for f in EXTRA_FIELDS if record.get(f) not in (None, "")}
    row["extra"] = extra or None
    return row


def normalize_credit(record: dict[str, Any]) -> dict[str, Any]:
    project = record.get("project") or {}
    return {
        "entity_id": db.int_or_none(record.get("id")),
        "project_id": db.int_or_none(project.get("id")),
        "quantity": db.float_or_none(record.get("number_of_credits")),
        "vintage": record.get("vintage"),
        "unit_type": record.get("product"),
        "status": record.get("status"),
        "event_date": record.get("certified_date"),
        # Gold Standard publishes no retirement beneficiary or reason on the
        # public credit record. Left blank rather than guessed.
        "beneficiary": None,
        "reason": None,
        # `labels` is the credit's product class (EMISSION_REDUCTION), not a
        # co-certification. Feeding it to Additional Certification would fill
        # that column with a value that does not mean what it says. The raw
        # list is preserved in raw_snapshots.
        "additional_certification": None,
        "serial_no": record.get("serial_number"),
    }
