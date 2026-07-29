"""Client for the S&P Global (Platts) public search API.

One platform, several registries. `registry.spglobal.com`'s own bundle
publishes the list — `VERRA`, `UKLR`, `RAAS`, `PVCL`, `OxCP`, `KRR`, `GCC`,
`BCCR` — and every one of them answers on the same backend with the same
request shape. A registry on this platform is therefore three header values,
not a new scraper.

    POST {es_public}/{resource}/publicReportPageSearch
    {"searchFilter": {"pagination": {"start": 0, "limit": 400},
                      "filterModel": {...}}}

The contract was captured from Verra's live site by `verra discover` on
2026-07-27 and re-confirmed against Plan Vivo on 2026-07-28. See
docs/api-contract.md and docs/api-contract-planvivo.md.

Hard-won constraints, all measured — do not "optimise" past them:

* The four registry-specific headers (`registry`, `standardid`,
  `standardacronym`, `language`) are REQUIRED. Without them every call is a
  generic HTTP 500 that looks like a server fault but is not.
* A wrong-but-plausible `standardid` is worse: it answers **HTTP 200 with
  `totalEntities: 0`**, which is indistinguishable from an empty registry.
  Read the real one from `{cmsResources}/public/standardsByRegistry/<registry>`
  rather than guessing; see `standards_by_registry()`.
* `limit` maxes out at 400. 500 fails.
* Elasticsearch's result window caps `start` at 10000. Anything beyond that
  is a 500, so large resources must be split into partitions of <10k hits.
* `sortOptions` on a non-sortable field (e.g. `id`) fails. `entityId` works and
  is the unique document id, so it is used as a stable sort on every paged
  read. This is NOT cosmetic: without a deterministic order Elasticsearch
  returns documents in an unstable order across pages, which silently skips
  records. An early run lost 1,271 of 5,244 projects exactly this way.
* `aggregateResultBySearchFilter` with `groupKeys` fails; only the ungrouped
  sum works. That is why totals are aggregated locally instead of server-side.
* **The API silently ignores filters it does not understand and returns the
  whole index.** `{"filterType": "Text", "type": "blank"}` on `vintage`
  returned 305,146 rows — everything — instead of an error. A partition built
  on an ignored filter overlaps every other partition, is then truncated at
  the 10k window, and produces duplicates *and* gaps simultaneously. This cost
  us 38,522 retirement records on the first full run. `filter_narrows()`
  exists to catch it; never partition on an unverified filter.

Because Verra's `retirements` (305k rows) cannot be partitioned safely under
those constraints, its per-project totals come from `sum_by_project()` — an
exact server-side SUM per project — rather than from paged rows. See
`verra totals`. A small registry on this platform needs no such thing.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator
from typing import Any

from ... import db, settings
from ...http_client import RegistryClient

log = logging.getLogger(__name__)

PROJECT = "project"
ISSUANCES = "issuances"
HOLDINGS = "holdings"
RETIREMENTS = "retirements"
CANCELLATIONS = "cancellations"

LEDGERS = (ISSUANCES, HOLDINGS, RETIREMENTS, CANCELLATIONS)
ALL_RESOURCES = (PROJECT, *LEDGERS)

MAX_LIMIT = 400
MAX_WINDOW = 10_000

# Unique document id. Sorting by it makes paging deterministic; see module docs.
STABLE_SORT = [{"sort": "entityId", "dir": "ASC"}]

# Fields used to split a resource that exceeds the 10k window, most selective
# last. Applied in order until a partition fits.
PARTITION_KEYS: dict[str, list[str]] = {
    PROJECT: ["status", "regionName", "countryName"],
    ISSUANCES: ["vintage", "regionName", "countryName"],
    HOLDINGS: ["vintage", "regionName", "countryName"],
    RETIREMENTS: ["vintage", "regionName", "countryName"],
    CANCELLATIONS: ["vintage", "regionName", "countryName"],
}


def text_equals(field: str, value: str) -> dict[str, Any]:
    return {field: {"columnFilters": [{"filterType": "Text", "type": "equals", "filter": value}]}}


def number_equals(field: str, value: Any) -> dict[str, Any]:
    return {
        field: {
            "columnFilters": [
                {"filterType": "Number", "type": "equals", "filter": str(value)}
            ]
        }
    }


def standards_by_registry(
    client: RegistryClient, registry_code: str, *, config_url: str | None = None
) -> list[dict[str, Any]]:
    """The standards a Platts registry publishes, with their real `standardId`.

    This is how a new registry on this platform is onboarded without guessing.
    The app calls it on every page load, it is unauthenticated, and it is the
    only published source of the id — a wrong one returns HTTP 200 with an
    empty result rather than an error, so guessing is not survivable.

        [{"id": "671000000000001", "name": "PV Climate", "metaData": "PV",
          "publicReportExport": {"PROJECTS": true, ...}}]

    `metaData` is the `standardacronym` header. `publicReportExport` says which
    ledgers the registry exposes publicly, which is how a new registry's ledger
    set is decided rather than assumed from Verra's.
    """
    base = client.base_uri("cmsResources", config_url=config_url) or (
        f"{settings.PLATTS_BASE}/cms-resources"
    )
    url = f"{base.rstrip('/')}/public/standardsByRegistry/{registry_code}"
    payload = client.get_json(url, headers=client.api_headers(config_url=config_url))
    return payload if isinstance(payload, list) else []


class PlattsAPI:
    """Talks to the public search API. Knows nothing about our database.

    Implements registries.base.RegistryAdapter. Subclasses set the six class
    attributes below and nothing else; everything measured about the platform
    is shared, so a fix here fixes every registry on it.
    """

    #: Value stored in our `registry` column (settings.VERRA, ...).
    registry: str = ""
    #: The platform's own code for this registry, sent as the `registry` header.
    registry_code: str = ""
    #: Real `standardid`. Read from standards_by_registry(), never invented.
    standard_id: str = ""
    #: `standardacronym` header; the standard's `metaData` in the call above.
    standard_acronym: str = ""
    #: Public site this registry is served from. Sets Origin/Referer.
    site: str = ""
    #: That site's own routing table, read at runtime so a backend move is free.
    config_url: str = ""
    #: Public project page, for spot-checking a row against the registry.
    detail_url_template: str = ""

    ledgers: tuple[str, ...] = LEDGERS
    partition_keys: dict[str, list[str]] = PARTITION_KEYS

    def __init__(
        self,
        client: RegistryClient | None = None,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self.client = client or RegistryClient(cancel=cancel)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> PlattsAPI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------

    @classmethod
    def registry_headers(cls) -> dict[str, str]:
        """The four headers without which every call is a misleading 500."""
        return {
            "registry": cls.registry_code,
            "standardid": cls.standard_id,
            "standardacronym": cls.standard_acronym,
            "language": "en",
            "Accept": "application/json",
        }

    def _base(self) -> str:
        uri = self.client.base_uri("raasReportPublicManager", config_url=self.config_url)
        return (uri or settings.FALLBACK_URIS["raasReportPublicManager"]).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = dict(self.client.api_headers(config_url=self.config_url))
        headers.update(self.registry_headers())
        # Each registry is served from its own site. Cercarbono taught us that
        # a shared Origin is not free: EcoRegistry validates it and answers a
        # wrong one with a generic 500. Send this registry's own.
        if self.site:
            headers["Origin"] = self.site
            headers["Referer"] = f"{self.site}/"
        # The app sends a fresh correlation id per call; mirror that.
        headers["x-request-id"] = str(uuid.uuid4())
        return headers

    def page_search(
        self,
        resource: str,
        *,
        start: int = 0,
        limit: int = MAX_LIMIT,
        filter_model: dict[str, Any] | None = None,
        sort: list[dict[str, str]] | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        pagination: dict[str, Any] = {"start": start, "limit": min(limit, MAX_LIMIT)}
        if sort:
            pagination["sortOptions"] = sort
        body = {"searchFilter": {"pagination": pagination, "filterModel": filter_model or {}}}
        return self.client.post_json(
            f"{self._base()}/{resource}/publicReportPageSearch",
            body,
            headers=self._headers(),
            refresh=refresh,
        )

    def count(self, resource: str, filter_model: dict[str, Any] | None = None) -> int:
        payload = self.page_search(resource, start=0, limit=1, filter_model=filter_model)
        return int(payload.get("totalEntities") or 0)

    def sum_field(
        self, resource: str, field: str, filter_model: dict[str, Any] | None = None
    ) -> float:
        """Server-side SUM. Used as an independent cross-check on our totals."""
        body = {
            "searchFilter": {
                "pagination": {},
                "filterModel": filter_model or {},
                "groupKeys": [],
                "statsSumByField": field,
            }
        }
        payload = self.client.post_json(
            f"{self._base()}/{resource}/aggregateResultBySearchFilter",
            body,
            headers=self._headers(),
        )
        return float(payload.get("sum") or 0.0)

    def filter_narrows(
        self, resource: str, filter_model: dict[str, Any], parent_total: int
    ) -> bool:
        """True if `filter_model` actually filters anything.

        The API returns the whole index for filters it does not understand, so
        a partition whose count equals its parent's is a filter that was
        ignored — using it would corrupt the data. Always check before
        partitioning on a filter shape that has not been proven.
        """
        return self.count(resource, filter_model) < parent_total

    # -- exact per-project totals ------------------------------------------

    def sum_by_project(
        self,
        resource: str,
        project_ids: list[int],
        *,
        field: str = "holdingQuantity",
        with_counts: bool = False,
        progress: Any = None,
    ) -> Iterator[tuple[int, float, int | None]]:
        """Yield (project_id, summed_quantity, row_count) straight from the API.

        This sidesteps paging entirely, so it is immune to both the 10k window
        and the silently-ignored-filter trap. It is the authoritative source
        for Verra's `retirements`, which is too large to page reliably.

        One request per project (two with `with_counts`), so it is slow — but
        exact.
        """
        for index, project_id in enumerate(project_ids, start=1):
            filter_model = number_equals("projectId", project_id)
            try:
                quantity = self.sum_field(resource, field, filter_model)
                rows = self.count(resource, filter_model) if with_counts else None
            except Exception as exc:  # noqa: BLE001 - one bad project must not stop the run
                log.error("Totals failed for project %s on %s: %s", project_id, resource, exc)
                continue
            yield project_id, quantity, rows
            if progress is not None:
                progress(index)

    # -- bulk iteration ----------------------------------------------------

    def iter_records(
        self,
        resource: str,
        *,
        filter_model: dict[str, Any] | None = None,
        max_records: int | None = None,
        progress: Any = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every record of `resource`, splitting around the 10k window.

        Partitioning is driven by the values actually present in the data
        (discovered by sampling), so it adapts if the registry adds a new
        vintage or region rather than relying on a hardcoded list.
        """
        emitted = 0
        for partition, expected in self._partitions(resource, filter_model or {}):
            for record in self._drain(resource, partition, expected):
                yield record
                emitted += 1
                if progress is not None:
                    progress(emitted)
                if max_records is not None and emitted >= max_records:
                    return

    def _drain(
        self, resource: str, filter_model: dict[str, Any], expected: int
    ) -> Iterator[dict[str, Any]]:
        """Page through one partition that is known to fit inside the window.

        Sorted by `entityId` so the order is stable between pages — otherwise
        Elasticsearch reshuffles and records fall through the gaps.
        """
        start = 0
        seen = 0
        while start < MAX_WINDOW:
            payload = self.page_search(
                resource,
                start=start,
                limit=MAX_LIMIT,
                filter_model=filter_model,
                sort=STABLE_SORT,
            )
            entities = payload.get("entities") or []
            if not entities:
                break
            yield from entities
            seen += len(entities)
            start += len(entities)
            if payload.get("last") or len(entities) < MAX_LIMIT:
                break
        if seen < expected:
            log.error(
                "INCOMPLETE: %s %s yielded %s of %s records.",
                resource,
                _describe(filter_model),
                seen,
                expected,
            )

    def _partitions(
        self, resource: str, filter_model: dict[str, Any], total: int | None = None
    ) -> Iterator[tuple[dict[str, Any], int]]:
        """Split `filter_model` until every partition holds < 10k records.

        Yields (filter, expected_count) so the caller does not have to ask the
        server for a count it has already been told.
        """
        if total is None:
            total = self.count(resource, filter_model)
        if total == 0:
            return
        if total < MAX_WINDOW:
            log.info("Partition %s %s -> %s records", resource, _describe(filter_model), total)
            yield filter_model, total
            return

        for key in self.partition_keys.get(resource, []):
            if key in filter_model:
                continue
            values = self._distinct_values(resource, key, filter_model)
            if not values:
                continue
            log.info(
                "Splitting %s %s (%s records) by %s into %s buckets",
                resource,
                _describe(filter_model),
                total,
                key,
                len(values),
            )
            # Partition values are discovered by sampling, so a value that never
            # appears in the sample would silently drop its records. Account for
            # every bucket and shout if the parts do not add up to the whole.
            covered = 0
            ignored = False
            for value in values:
                child = dict(filter_model)
                child.update(text_equals(key, value))
                child_total = self.count(resource, child)
                # A child that is no smaller than its parent means the filter
                # was ignored and this "partition" is actually the whole index.
                # Draining it would duplicate everything and still miss rows.
                if child_total >= total:
                    log.error(
                        "Filter %s=%s on %s was IGNORED by the API "
                        "(returned %s of a %s-record parent). Abandoning this split.",
                        key,
                        value,
                        resource,
                        child_total,
                        total,
                    )
                    ignored = True
                    break
                covered += child_total
                yield from self._partitions(resource, child, child_total)
            if ignored:
                continue
            if covered < total:
                log.error(
                    "INCOMPLETE: %s %s split by %s covers %s of %s records "
                    "(%s missing). Sampling missed some %s values — widen "
                    "_distinct_values or add a partition key.",
                    resource,
                    _describe(filter_model),
                    key,
                    covered,
                    total,
                    total - covered,
                    key,
                )
            return

        # Out of split keys. Take what the window allows and say so loudly
        # rather than silently dropping records.
        log.error(
            "Cannot split %s %s below the %s window (%s records). "
            "Only the first %s will be captured — add a partition key.",
            resource,
            _describe(filter_model),
            MAX_WINDOW,
            total,
            MAX_WINDOW,
        )
        yield filter_model, min(total, MAX_WINDOW)

    def _distinct_values(
        self, resource: str, field: str, filter_model: dict[str, Any]
    ) -> list[str]:
        """Sample the data to learn which values of `field` actually occur.

        Server-side faceting (`groupKeys`) returns 500, so we sample instead.
        Sampling both ends of the sort order keeps rare values from hiding.
        """
        found: set[str] = set()
        for start in (0, 1_000, 2_500, 4_000, 5_500, 7_000, 8_500, 9_600):
            try:
                payload = self.page_search(
                    resource, start=start, limit=MAX_LIMIT, filter_model=filter_model
                )
            except Exception:  # noqa: BLE001 - a short index is not an error
                break
            entities = payload.get("entities") or []
            for entity in entities:
                value = entity.get(field)
                if value not in (None, ""):
                    found.add(str(value))
            if len(entities) < MAX_LIMIT:
                break
        return sorted(found)

    # -- convenience -------------------------------------------------------

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        for record in self.iter_records(
            PROJECT, max_records=max_records, progress=progress
        ):
            yield self.normalize_project(record), record

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        for record in self.iter_records(
            resource, max_records=max_records, progress=progress
        ):
            yield self.normalize_credit(record)

    def project_total(self) -> int:
        return self.count(PROJECT)

    def detail_url(self, project_id: int) -> str:
        return self.detail_url_template.format(project_id=project_id)

    # -- normalisation -----------------------------------------------------
    # The platform's field names -> the registry-neutral columns in
    # db.PROJECT_FIELDS. Classmethods, not module functions, because the
    # detail-URL template and the sectoral-scope source differ per registry.

    #: Platform field -> our column. Overridden where a registry populates a
    #: different field for the same idea (Plan Vivo states `projectType` and
    #: leaves `sectoralScope` null).
    project_columns: dict[str, str] = {
        "projectId": "project_id",
        "vcsProjectId": "external_id",
        "projectName": "project_name",
        "standardName": "standard_name",
        "status": "status",
        "sectoralScope": "sectoral_scope",
        "methodologies": "methodologies",
        "afoluNames": "afolu_names",
        "projectSize": "project_size",
        "proponents": "proponents",
        "regionName": "region_name",
        "countryName": "country_name",
        "stateProvince": "state_province",
        "city": "city",
        "area": "area",
        "creditPeriod": "credit_period",
        "creditPeriodTerm": "credit_period_term",
        "creditPeriodStartDate": "credit_period_start",
        "creditPeriodEndDate": "credit_period_end",
        "projectStartDate": "project_start_date",
        "projectEndDate": "project_end_date",
        "avgAnnualVolVcu": "avg_annual_vol_vcu",
        "exanteQuantity": "exante_quantity",
        "unitsIssued": "units_issued",
        "additionalCertification": "additional_certification",
    }

    #: Where a credit record states its unit type, most specific first.
    unit_type_fields: tuple[str, ...] = ("unitType",)

    @classmethod
    def normalize_project(cls, record: dict[str, Any]) -> dict[str, Any]:
        row = {column: record.get(field) for field, column in cls.project_columns.items()}
        project_id = db.int_or_none(row.get("project_id"))
        row["project_id"] = project_id
        if project_id is not None and cls.detail_url_template:
            row["detail_url"] = cls.detail_url_template.format(project_id=project_id)
        return row

    @classmethod
    def normalize_credit(cls, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "entity_id": db.int_or_none(record.get("entityId") or record.get("id")),
            "project_id": db.int_or_none(
                record.get("projectId") or record.get("publicProjectId")
            ),
            "quantity": db.float_or_none(record.get("holdingQuantity")),
            "vintage": record.get("vintage"),
            "unit_type": db.first(record, cls.unit_type_fields),
            "status": record.get("status"),
            "event_date": db.first(record, EVENT_DATE_FIELDS),
            "beneficiary": record.get("beneficialOwner"),
            "reason": record.get("retirementReason"),
            "additional_certification": record.get("additionalCertification"),
            "serial_no": record.get("serialNo"),
        }


# Per-resource date field carrying "when the event happened".
EVENT_DATE_FIELDS = ("retiredDate", "issueDate", "cancelledDate", "modifiedDate")

# Kept at module level: it is the platform's map, and a subclass that wants
# most of it copies from here.
PROJECT_COLUMNS = PlattsAPI.project_columns


def _describe(filter_model: dict[str, Any]) -> str:
    if not filter_model:
        return "(all)"
    parts = []
    for key, spec in filter_model.items():
        try:
            parts.append(f"{key}={spec['columnFilters'][0]['filter']}")
        except (KeyError, IndexError, TypeError):
            parts.append(key)
    return "(" + ", ".join(parts) + ")"
