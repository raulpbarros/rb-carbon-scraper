"""Client for Cercarbono, which runs on the EcoRegistry platform.

Contract measured against the live service on 2026-07-28; see
docs/api-contract-cercarbono.md. It is plain unauthenticated REST, and by far
the smallest of the registries so far — but it has traps of its own:

* **Two headers are mandatory.** Without `platform: ecoregistry` and `lng`,
  every call answers HTTP 200 with
  `{"codeMessages":[{"codeMessage":"ERROR_401","message":"No autorizado"}]}`.
  That reads like a credential wall and is not one: the data is public and
  the headers ship in the site's own bundle. Same "looks blocked, isn't" trap
  as Gold Standard's Cloudflare 403.
* **Three bulk endpoints, no paging at all.** The whole registry arrives in
  three responses. There is no page parameter to get wrong, and equally no
  page count to reconcile against — the guard here is that every project in
  the standard's own list must come back, see `iter_projects`.
* **The bulk feeds cover every standard, not just this one.** EcoRegistry
  hosts `cercarbono-co2` alongside `cercarbono-biodiversity` and
  `cercarbono-circular-economy`, whose credits are not tCO2e. Both bulk feeds
  are filtered down to the CO2 project ids before anything is yielded.
* **`analytics/projects` is not complete.** It omits projects converted in
  from another registry — two of them, both ex-BioCarbon — which nonetheless
  have retirements. Left alone that shows a project having retired credits it
  never issued. `iter_credit_totals` closes the gap from the per-project
  detail, which does publish their issued figure.

Four endpoints, in the order they are used:

    GET /project/public-by-standard/cercarbono-co2   the CO2 project list
    GET /analytics/projects                          locations + issued serials
    GET /analytics/get-retirements                   the retirement ledger
    GET /project/public/{id}                         crediting period, totals

The first three are one request each. The fourth is one request per project
(~231, so ~4 minutes at the default 1 req/s) and is what fills `Data de
Início` / `Data de Término`; nothing else publishes the crediting period.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Iterator
from typing import Any

from ... import db, settings
from ...http_client import RegistryClient

log = logging.getLogger(__name__)

REGISTRY = settings.CERCARBONO

ISSUANCES = "issuances"
RETIREMENTS = "retirements"

#: Two ledgers, deliberately named the way Verra's are. `db.credit_totals()`
#: branches on the exact string `credits` to pick Gold Standard's
#: bucket-by-status semantics; Cercarbono's records carry no status, so they
#: want the one-ledger-per-resource path that any other name selects.
#: Cercarbono publishes no cancellation ledger, so `Total Credits Cancelled`
#: stays blank rather than being reported as zero.
LEDGERS = (ISSUANCES, RETIREMENTS)

# Values the registry uses to mean "no value", which are not values.
NOT_STATED = {"not defined", "no definido", "no definido/not defined", "na", "n/a"}

#: The placeholder EcoRegistry stores when a project's region and city were
#: never entered — 575 of 811 locations use it, always as both region and city
#: at once, and always with a real country beside it. It is a blank, not a
#: place, and writing "Worldwide" into Estado/Cidade would be stating a
#: location the registry never published. Applied to region and city only.
PLACEHOLDER_PLACES = {"worldwide", "mundial"}


def _stated(value: Any) -> str | None:
    """Registry text, or None when the registry said it has none."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in NOT_STATED:
        return None
    return text


def _place(value: Any) -> str | None:
    """A region or city name, or None when it is the platform's placeholder."""
    text = _stated(value)
    if text is None or text.casefold() in PLACEHOLDER_PLACES:
        return None
    return text


def _joined(values: Any) -> str | None:
    """Distinct values in first-seen order, joined.

    Cercarbono repeats a sector once per verification, so a single-sector
    project can list `Land use (AFOLU)` three times. Order is preserved rather
    than sorted: the registry lists the primary entry first.
    """
    seen: list[str] = []
    for value in values or ():
        text = _stated(value)
        if text and text not in seen:
            seen.append(text)
    return "; ".join(seen) or None


def serial_entity_id(serial: str) -> int:
    """A stable numeric key for an issuance, which has no id of its own.

    `credit_events` is keyed on an integer, and a serial is a string. Hashing
    it gives a key that is identical on every run, which is what makes the
    upsert idempotent — an incrementing counter would renumber every row each
    time the feed order changed. 60 bits, so it fits SQLite's signed INTEGER
    with room to spare.
    """
    return int(hashlib.sha1(serial.encode("utf-8")).hexdigest()[:15], 16)


class CercarbonoAPI:
    """Talks to the public EcoRegistry API. Knows nothing about our database.

    Implements registries.base.RegistryAdapter.
    """

    registry = REGISTRY
    ledgers = LEDGERS

    def __init__(
        self,
        client: RegistryClient | None = None,
        *,
        cancel: threading.Event | None = None,
        standard: str | None = None,
    ) -> None:
        self.client = client or RegistryClient(cancel=cancel)
        self._owns_client = client is None
        self.standard = standard or settings.CERCARBONO_STANDARD
        self._projects: list[dict[str, Any]] | None = None
        self._analytics: dict[int, dict[str, Any]] | None = None
        self._retirements: list[dict[str, Any]] | None = None
        #: project_id -> issued total, as the registry itself states it.
        #: Filled while iterating projects; see iter_credit_totals.
        self._issued: dict[int, float] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> CercarbonoAPI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # `platform` and `lng` are not cosmetic: without them every call is
        # ERROR_401.
        #
        # Origin and Referer are not cosmetic either, and in the opposite
        # direction. `settings.BROWSER_HEADERS` carries Verra's site in both,
        # and EcoRegistry checks them: sending `Origin: registry.verra.org`
        # here answers HTTP 500 — a generic server fault that looks nothing
        # like the CORS rejection it is. We send this registry's own site,
        # which is what its browser sends.
        headers = dict(settings.BROWSER_HEADERS)
        headers.update(settings.CERCARBONO_HEADERS)
        headers["Origin"] = settings.CERCARBONO_SITE
        headers["Referer"] = f"{settings.CERCARBONO_SITE}/"
        return headers

    def get(self, path: str, *, refresh: bool = False) -> dict[str, Any]:
        body = self.client.get_json(
            f"{settings.CERCARBONO_API}/{path}",
            headers=self._headers(),
            refresh=refresh,
        )
        if not isinstance(body, dict):
            raise ValueError(f"{path} returned {type(body).__name__}, expected an object")
        # The platform reports application errors in a 200 body. An unnoticed
        # ERROR_401 here would look exactly like an empty registry.
        messages = body.get("codeMessages")
        if messages:
            raise ValueError(f"{path} refused the request: {messages}")
        return body

    # -- the three bulk feeds ----------------------------------------------

    def projects(self) -> list[dict[str, Any]]:
        """Every project on the configured standard. The authoritative list."""
        if self._projects is None:
            body = self.get(f"project/public-by-standard/{self.standard}")
            self._projects = list(body.get("projects") or [])
        return self._projects

    def project_ids(self) -> set[int]:
        return {
            pid
            for pid in (db.int_or_none(p.get("id")) for p in self.projects())
            if pid is not None
        }

    def analytics(self) -> dict[int, dict[str, Any]]:
        """Locations and issued serials, keyed by project id.

        Covers every standard, and does not cover every project: converted-in
        projects are absent. Callers must treat a miss as "not published here",
        not as "no data".
        """
        if self._analytics is None:
            body = self.get("analytics/projects")
            found: dict[int, dict[str, Any]] = {}
            for record in body.get("project") or []:
                pid = db.int_or_none(record.get("id"))
                if pid is not None:
                    found[pid] = record
            self._analytics = found
        return self._analytics

    def retirements(self) -> list[dict[str, Any]]:
        """The whole retirement ledger, every standard, in one response."""
        if self._retirements is None:
            body = self.get("analytics/get-retirements")
            self._retirements = list(body.get("retirements") or [])
        return self._retirements

    def detail(self, project_id: int) -> dict[str, Any]:
        """One project's full record — the only source of its crediting period."""
        return self.get(f"project/public/{project_id}")

    # -- counts ------------------------------------------------------------

    def project_total(self) -> int:
        return len(self.projects())

    def count(self, resource: str) -> int:
        """How many records this registry holds for `resource`, on this standard."""
        wanted = self.project_ids()
        if resource == ISSUANCES:
            return sum(
                len(record.get("serials") or [])
                for pid, record in self.analytics().items()
                if pid in wanted
            )
        if resource == RETIREMENTS:
            return sum(
                1
                for row in self.retirements()
                if db.int_or_none(row.get("project_id")) in wanted
            )
        raise ValueError(f"Cercarbono has no ledger named {resource!r}")

    def detail_url(self, project_id: int) -> str:
        return settings.CERCARBONO_PROJECT_DETAIL_URL.format(project_id=project_id)

    # -- iteration ---------------------------------------------------------

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """Yield every project on the standard, merged from three sources.

        The standard's own list decides membership and supplies the code and
        methodologies; `analytics/projects` adds locations; the per-project
        detail adds the crediting period and the registry's own issued total.
        The detail call is one request per project, which is why this is the
        slow part of an otherwise three-request sync.
        """
        records = self.projects()
        analytics = self.analytics()
        seen = 0
        for record in records:
            project_id = db.int_or_none(record.get("id"))
            if project_id is None:
                continue
            detail = self.detail(project_id)
            raw = {
                "standard_list": record,
                "analytics": analytics.get(project_id),
                "detail": detail,
            }
            row = normalize_project(record, analytics.get(project_id), detail)
            row["detail_url"] = self.detail_url(project_id)

            issued = issued_total(detail)
            if issued is not None:
                self._issued[project_id] = issued

            yield row, raw
            seen += 1
            if progress is not None:
                progress(seen)
            if max_records is not None and seen >= max_records:
                return

        if seen < len(records):
            log.error(
                "INCOMPLETE: projects yielded %s of the %s the standard lists.",
                seen,
                len(records),
            )

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        wanted = self.project_ids()
        if resource == ISSUANCES:
            source = self._iter_issuances(wanted)
        elif resource == RETIREMENTS:
            source = self._iter_retirements(wanted)
        else:
            raise ValueError(f"Cercarbono has no ledger named {resource!r}")

        expected = self.count(resource)
        seen = 0
        for row in source:
            yield row
            seen += 1
            if progress is not None:
                progress(seen)
            if max_records is not None and seen >= max_records:
                return

        if seen < expected:
            log.error(
                "INCOMPLETE: %s yielded %s of %s records the feed holds.",
                resource,
                seen,
                expected,
            )

    def _iter_issuances(self, wanted: set[int]) -> Iterator[dict[str, Any]]:
        for project_id, record in self.analytics().items():
            if project_id not in wanted:
                continue
            for serial in record.get("serials") or []:
                row = normalize_issuance(project_id, serial)
                if row is not None:
                    yield row

    def _iter_retirements(self, wanted: set[int]) -> Iterator[dict[str, Any]]:
        for record in self.retirements():
            if db.int_or_none(record.get("project_id")) not in wanted:
                continue
            yield normalize_retirement(record)

    def iter_credit_totals(self, resource: str) -> Iterator[tuple[int, float, Any]]:
        """The registry's own issued total per project, which outranks row sums.

        Two reasons this exists rather than trusting the summed serials:

        * `analytics/projects` omits projects converted in from another
          registry, so two ex-BioCarbon projects have retirements and no
          issuances at all. Their issued figure is published on the detail
          record, and only there.
        * It is the number Cercarbono itself states, which is what the
          business is reconciling against.

        Verified equal to the summed serials (buffer included) on every
        project where both exist.
        """
        if resource != ISSUANCES:
            return
        if not self._issued:
            for project_id in sorted(self.project_ids()):
                issued = issued_total(self.detail(project_id))
                if issued is not None:
                    self._issued[project_id] = issued
        for project_id, quantity in sorted(self._issued.items()):
            yield project_id, quantity, None


# -- normalisation ---------------------------------------------------------
# Cercarbono's field names -> the registry-neutral db.PROJECT_FIELDS.
#
# Deliberate gaps, measured over the full 231-project CO2 index:
#   * `country_code` — no ISO code is published anywhere, so Continent is
#     derived from `country_name` instead; see config/derivation/continent.yaml.
#   * `avg_annual_vol_vcu` (Yearly Ex Ante) — not published. Blank, never
#     back-computed from issuances, which are actuals and not an estimate.
#   * `afolu_names` — Cercarbono states no AFOLU sub-type code. The AFOLU
#     rules keyed on it simply do not fire; the methodology rules do.
#   * `additional_certification` — no equivalent field. The serials' `elegible`
#     list ("Colombian Carbon Tax", "Voluntary compensation") is market
#     eligibility, not a co-certification, and is deliberately not used for it.
#   * cancellations — no ledger is published, so the column stays blank rather
#     than being reported as a zero the registry never stated.


def sectoral_scope(record: dict[str, Any]) -> str | None:
    """Tipo Macro: Cercarbono's own sector vocabulary, carried through as-is.

    Not translated into Verra's or Gold Standard's taxonomy. Each registry's
    column says what that registry publishes — the settled shape of this
    column, not a gap.
    """
    return _joined(s.get("description") for s in record.get("sectors") or [])


def methodologies(record: dict[str, Any], analytics: dict[str, Any] | None) -> str | None:
    """The standard list carries the full methodology set; analytics one string."""
    joined = _joined(m.get("description") for m in record.get("methodology") or [])
    if joined:
        return joined
    return _stated((analytics or {}).get("quantification_method"))


def _locations(analytics: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((analytics or {}).get("locations") or [])


def normalize_project(
    record: dict[str, Any],
    analytics: dict[str, Any] | None,
    detail: dict[str, Any] | None,
) -> dict[str, Any]:
    project = (detail or {}).get("project") or {}
    locations = _locations(analytics)

    row: dict[str, Any] = {
        "project_id": db.int_or_none(record.get("id")),
        # `code` is the human-facing reference (CDC-271). The numeric id is an
        # internal key and is not what the registry shows.
        "external_id": _stated(record.get("code")),
        "project_name": _stated(record.get("name")),
        # Cercarbono states a protocol version rather than a standard name;
        # that is the closest analogue to Gold Standard's standards version.
        # When it is "Not defined" the exporter falls back to the registry
        # label, so the discriminator column is never empty.
        "standard_name": _stated((analytics or {}).get("evaluation_criteria"))
        or _stated(project.get("standarDescription")),
        "status": _stated(record.get("projectStage")),
        "sectoral_scope": sectoral_scope(record),
        "methodologies": methodologies(record, analytics),
        "proponents": _stated(record.get("developer"))
        or _stated((analytics or {}).get("owner")),
        # Every project in the index is single-country; the join is a guard,
        # not an expectation.
        "country_name": _joined(l.get("country") for l in locations)
        or _stated(record.get("locationText")),
        "state_province": _joined(_place(l.get("region")) for l in locations),
        "city": _joined(_place(l.get("city")) for l in locations),
        "credit_period_start": _stated(project.get("periodInit")),
        "credit_period_end": _stated(project.get("periodEnd")),
    }

    extra = {
        "owner": (analytics or {}).get("owner"),
        "validator": _stated((analytics or {}).get("validator")),
        "verifier": _stated(record.get("verifier"))
        or _stated((analytics or {}).get("verifier")),
        "protocols": _joined(p.get("description") for p in record.get("protocols") or []),
        "quantification_method": (analytics or {}).get("quantification_method"),
        "accreditation_period": project.get("accreditationPeriod"),
        "credit_unit": project.get("creditUnit"),
        "hash_id": project.get("hashId"),
        # Cercarbono re-issues projects migrated from another registry. Worth
        # keeping visible: the same project may also exist in that registry,
        # and counting both would double it.
        "converted_from": project.get("convertedFromDescription"),
        "converted_from_link": project.get("convertedFromLink"),
        "coordinates": [
            l.get("data_map") for l in locations if l.get("data_map")
        ] or None,
    }
    row["extra"] = {k: v for k, v in extra.items() if v not in (None, "", [])} or None
    return row


def issued_total(detail: dict[str, Any] | None) -> float | None:
    """The registry's own issued total for one project, buffer included.

    `certificatedVerification` is what the registry itself reports as issued,
    and it matches the sum of the project's serials wherever both are
    published. An empty list means zero issued, which is a real figure and not
    a missing one; a detail record without the key at all is missing, and
    returns None.
    """
    if detail is None or "certificatedVerification" not in detail:
        return None
    total = 0.0
    for entry in detail.get("certificatedVerification") or []:
        total += db.float_or_none(entry.get("total")) or 0.0
    return total


def normalize_issuance(project_id: int, serial: dict[str, Any]) -> dict[str, Any] | None:
    """One issued serial block.

    Buffer serials are included: Cercarbono's own issued total counts them,
    so excluding them would disagree with the registry. The flag is kept in
    `unit_type` so a later decision to split them needs no re-scrape.
    """
    reference = _stated(serial.get("serial"))
    if reference is None:
        return None
    return {
        "entity_id": serial_entity_id(reference),
        "project_id": project_id,
        "quantity": db.float_or_none(serial.get("issued_quantity")),
        "vintage": _stated(serial.get("year")),
        "unit_type": "Buffer" if serial.get("is_buffer") else "Credit",
        # Issuance blocks carry no status; the ledger name is the state.
        "status": None,
        "event_date": _stated(serial.get("issuance_date")),
        "beneficiary": None,
        "reason": None,
        "additional_certification": None,
        "serial_no": reference,
    }


def normalize_retirement(record: dict[str, Any]) -> dict[str, Any]:
    """One retirement.

    `is_kg` marks a quantity stated in kilograms rather than tonnes — the
    platform has kg-denominated retirement endpoints. No row in the live
    ledger uses it today, but mixing kg and tCO2e in one column would be
    silently wrong, so it is converted rather than trusted.
    """
    quantity = db.float_or_none(record.get("quantity"))
    if quantity is not None and record.get("is_kg"):
        quantity = quantity / 1000.0
    reason = record.get("reason_using") or {}
    return {
        "entity_id": db.int_or_none(record.get("id")),
        "project_id": db.int_or_none(record.get("project_id")),
        "quantity": quantity,
        "vintage": _stated(record.get("vintage")),
        "unit_type": None,
        "status": None,
        "event_date": _stated(record.get("date")),
        # Cercarbono publishes who the credits were retired for, which is what
        # the beneficiary-based reading of "sold" needs.
        "beneficiary": _stated(record.get("final_user")),
        "reason": _stated(reason.get("description")),
        "additional_certification": None,
        "serial_no": _stated(record.get("serial")),
    }
