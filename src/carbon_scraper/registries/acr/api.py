"""Client for the American Carbon Registry, which publishes through ICE GreenTrace.

Contract measured against the live service on 2026-08-04; see
docs/api-contract-acr.md. The platform's own traps live in
`registries/greentrace/api.py`; this module is ACR's identity and the fields
ACR actually populates.

    POST {api}/project/registry/ACR_REGISTRY/project-summaries/results   994 projects
    POST {api}/credit/registry/ACR_REGISTRY/holding-summaries/results    the ledgers
    GET  {api}/project/registry/ACR_REGISTRY/project/{projectKey}        the detail

**ACR is not on APX any more.** Every older note points at `acr2.apx.com`, which
now answers "You have reached an invalid page" at HTTP 200 for every path —
including the report URLs, so a scraper written against it would "succeed" and
parse nothing. ACR's own site links to ICE GreenTrace instead.

Five things measured here that are ACR's rather than the platform's:

* **One URL is four ledgers, chosen by `holdingStatus`, and the unfiltered
  view is all of them at once.** With no status the report returns the whole
  holdings book — 16,385 records summing to exactly the issued total, whose
  RETIRED and CANCELED subsets are *key-identical* to the two ledgers. Scraping
  it as an "active units" feed would restate every retirement and cancellation,
  which is SocialCarbon's `asset` trap precisely. It is not ingested; the
  balance it would have provided is published anyway, and holds exactly:
  ACTIVE 89,712,900 + INACTIVE 45,186,629 = issued − retired − cancelled =
  134,899,529.
* **`issuanceQuantity` is the parent event's total, repeated on every block of
  it.** 3,358 issuance blocks belong to 2,337 issuance events, and summing the
  field per row gives 604,312,772 against a true 379,674,647. `holdingQuantity`
  is the block's own quantity and is what a row states about itself.
* **Cancellation here mostly means conversion, not destruction.** 1,152 of
  1,358 cancellations read "Convert to ARB Offset Credits" and 14 "Convert to
  Ecology Offset Credits": the units leave for California's or Washington's
  compliance registry rather than ceasing to exist. The reason is stored on
  every row, so reporting the split is a query. See docs/field-mapping.md.
* **The state field mixes two vocabularies, sometimes in one value.**
  `projectSiteLocState` reads `OHIO`, `US-CA`, and
  `MISSOURI, US-GA, US-IN, US-TX, US-WI` — uppercase names and ISO 3166-2
  codes, joined by commas for a multi-state project. Carried through as
  published, like every other registry's vocabulary.
* **No country *name* is published anywhere.** Not in the list, not on the
  detail, not in the report criteria: the registry states `US` and stops.
  `country_code` is 994/994 so Continent derives cleanly (the Gold Standard
  path), and `País` stays blank rather than being invented — the first
  registry here with that gap.

And one that is not a trap: the detail's own `projectHoldingsTotalQuantity` and
`projectHoldingsTotalRetiredQuantity` agree with the ledgers exactly on every
project sampled, so there is no `iter_credit_totals` here. Nothing is stated
twice with two different answers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import Any

from ... import db, settings
from ..base import reconciled
from ..greentrace.api import GreenTraceAPI
from ..text import hashed_id, joined, stated

log = logging.getLogger(__name__)

REGISTRY = settings.ACR

ISSUANCES = "issuances"
RETIREMENTS = "retirements"
CANCELLATIONS = "cancellations"

LEDGERS = (ISSUANCES, RETIREMENTS, CANCELLATIONS)

#: The reports this tenant publishes, as its own pages configure them.
PROJECTS_REPORT = "project/registry/{registry}/project-summaries"
CREDITS_REPORT = "credit/registry/{registry}/holding-summaries"

#: Our ledger name -> (the `holdingStatus` that selects it, the dataset key it
#: answers under). The status values are read from the report's own
#: `/criteria`, not guessed: an unknown one narrows to zero rows rather than
#: returning the whole index, so a typo here loses a ledger loudly.
#:
#: The names on the left are chosen rather than described. `db.credit_totals()`
#: branches on the exact string `credits` to select Gold Standard's
#: bucket-by-status semantics, and these rows are already one bucket each.
LEDGER_CRITERIA = {
    ISSUANCES: ("ISSUED", "issuedCredits"),
    RETIREMENTS: ("RETIRED", "retiredCredits"),
    CANCELLATIONS: ("CANCELED", "cancelledCredits"),
}

PROJECTS_DATASET = "projects"

#: Values this registry uses to mean "not filled in yet". `N/A` is its own
#: word for it on `projectListingStatus`, and the CORSIA fields say it too.
NOT_STATED = {"n/a", "na", "none", "null", "-", "--", "not applicable"}

#: The same table with the one entry that is also a real ISO country code
#: removed. **`NA` is Namibia**, and this registry states a country as a code
#: and never as a name — so running the placeholder table over `country_code`
#: would delete a Namibian project's only country field, and take its Continent
#: with it. Puro met this exact trap and answers it by carrying no placeholder
#: table at all; ACR does need one (`projectListingStatus` really does say
#: `N/A`), so it drops the ambiguous entry instead. `n/a` stays: nothing
#: spells a country that way.
NOT_STATED_CODE = NOT_STATED - {"na"}

#: ACR reference -> the Verra reference for the same physical project.
#: **Measured, not published by either registry.** ACR states
#: `hasAnotherCarbonProgram` on 16 of 994 projects without naming the
#: programme; matching those names against the database (rare name tokens plus
#: country, so that "wind" and "wastewater" do not manufacture pairs) found two
#: that Verra also publishes.
#:
#: The crediting periods are consecutive, not concurrent — Verra 2008-2018 and
#: ACR 2018-2027 for Cambria 33; Verra 2010-2019 and ACR 2015-2024/2026-2035
#: for Corinth — so the credit tranches are disjoint and neither row is a copy
#: of the other. Both ship, exactly as Cercarbono's `CDC-106`/`CDC-107` and
#: BioCarbon's `BCR-CO-319-14-002`/`-005` do. Nothing is summed across
#: registries; this table is what makes the duplicate visible from the ACR
#: side. See docs/api-contract-acr.md and docs/field-mapping.md.
ALSO_REGISTERED_AS = {
    "ACR0242": "VERRA 559",
    "ACR0388": "VERRA 573",
    "ACR1192": "VERRA 573",
}

#: `ACR1275` -> 1275. Every one of the 994 published references matches, and
#: the numeric parts are unique, so this is the registry's own numeric id
#: rather than a key invented here. A reference that does not match is dropped
#: with a log line rather than hashed into something unreadable: this registry
#: has never published one, and inventing a key for the day it does would hide
#: the change.
REFERENCE = re.compile(r"^ACR0*(\d+)$", re.IGNORECASE)


def _stated(value: Any) -> str | None:
    return stated(value, NOT_STATED)


def _country_code(value: Any) -> str | None:
    """An ISO country code, with `NA` left alone. See NOT_STATED_CODE."""
    return stated(value, NOT_STATED_CODE)


def _project_key(reference: Any) -> int | None:
    match = REFERENCE.match(str(reference or "").strip())
    return int(match.group(1)) if match else None


def _worth_storing(value: Any) -> bool:
    """Whether an `extra` value says anything, keeping numeric zero.

    A false flag is dropped: `hasAnotherCarbonProgram` is false on 978 of 994
    projects and storing it there says no more than its absence does. But
    `0 == False` in Python, so a membership test that drops `False` also drops
    `0` and `0.0` — which would delete a stated retired total of zero on a
    project that has issued credits and retired none, leaving it
    indistinguishable from a project the registry published no figure for.
    That is the whole reason the stated totals are kept.
    """
    if value is None or isinstance(value, bool):
        return value is True
    if isinstance(value, (int, float)):
        return True
    return bool(value)


class ACRAPI(GreenTraceAPI):
    """Talks to the public ICE GreenTrace API as the ACR tenant.

    Implements registries.base.RegistryAdapter; the paging, the clamp and the
    dataset-key checking are the platform's, in GreenTraceAPI.
    """

    registry = REGISTRY
    registry_key = settings.ACR_REGISTRY_KEY
    ledgers = LEDGERS

    # **This registry bans, it does not throttle.** GreenTrace is behind a
    # Cloudflare rate-limiting rule: a sync at the usual 1/s reached ~93
    # requests and then took a 429 with `Retry-After: 3600` on every retry, and
    # a 20-request probe minutes later tripped the same rule — consistent with
    # roughly 100 requests per ten minutes. A sync needs ~1,005 of them,
    # because only the per-project detail carries a crediting period. At one
    # request per seven seconds a full sweep stays under it and takes about two
    # hours, which is Verra's order of magnitude and the price of this
    # registry. Measured 2026-08-04; do not raise it.
    requests_per_second = settings.ACR_REQUESTS_PER_SECOND

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: project key -> its detail payload, fetched once per project.
        self._details: dict[str, dict[str, Any]] = {}

    # -- counts ------------------------------------------------------------

    def project_total(self) -> int:
        return self.total(PROJECTS_REPORT, PROJECTS_DATASET)

    def count(self, resource: str) -> int:
        status, dataset = self._ledger(resource)
        return self.total(CREDITS_REPORT, dataset, {"holdingStatus": status})

    def detail_url(self, project_id: Any) -> str:
        """The public page for one project.

        Takes the **project key** (`P2423FTH4Z22`), not the reference id: the
        API 404s on a reference id, while the page answers HTTP 200 with a
        shell that renders an error — a link that looks fine in the sheet and
        is broken when clicked.
        """
        return settings.ACR_PROJECT_DETAIL_URL.format(project_id=project_id)

    # -- iteration ---------------------------------------------------------

    def _ledger(self, resource: str) -> tuple[str, str]:
        try:
            return LEDGER_CRITERIA[resource]
        except KeyError:
            raise ValueError(f"ACR has no ledger named {resource!r}") from None

    def _detail(self, project_key: str) -> dict[str, Any]:
        """One project's detail, which the list does not carry.

        The only source of the crediting period, the state, the annual
        estimate and the registry's own per-project totals. One request per
        project, and the reason a sync is ~1,005 requests rather than four —
        the same trade Cercarbono and BioCarbon both make.
        """
        if project_key not in self._details:
            self._details[project_key] = self.detail(project_key)
        return self._details[project_key]

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        records = self.fetch(PROJECTS_REPORT, PROJECTS_DATASET)
        expected = {
            key
            for key in (_project_key(r.get("projectId")) for r in records)
            if key is not None
        }
        yielded: set[int] = set()
        seen = 0

        for record in records:
            project_id = _project_key(record.get("projectId"))
            project_key = _stated(record.get("projectKey"))
            if project_id is None or project_key is None:
                log.error(
                    "An ACR project has no usable reference and cannot be "
                    "stored: %r",
                    record.get("projectId") or record.get("projectName"),
                )
                continue
            detail = self._detail(project_key)
            row = normalize_project(record, detail)
            row["detail_url"] = self.detail_url(project_key)
            yield row, {"list": record, "detail": detail}
            seen += 1
            yielded.add(project_id)
            if progress is not None:
                progress(seen)
            if max_records is not None and seen >= max_records:
                return

        # Key sets, not counts: a count cannot see one project yielded twice
        # while another is dropped.
        missing = expected - yielded
        if missing:
            log.error(
                "INCOMPLETE: %s ACR project(s) never reached the database.",
                len(missing),
            )

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        status, dataset = self._ledger(resource)
        criteria = {"holdingStatus": status}
        records = self.fetch(CREDITS_REPORT, dataset, criteria)
        rows = (
            normalize_credit(resource, record, index)
            for index, record in enumerate(records)
        )
        yield from reconciled(
            rows,
            expected=self.total(CREDITS_REPORT, dataset, criteria),
            progress=progress,
            max_records=max_records,
            label=f"{REGISTRY} {resource}",
        )


# -- normalisation ---------------------------------------------------------
# ACR's field names -> the registry-neutral db.PROJECT_FIELDS.
#
# Deliberate gaps, measured over the full 994-project index on 2026-08-04:
#   * `country_name` — no country name is published anywhere. The list states
#     an ISO code, the detail states the same code, and the report criteria
#     offer no country filter at all. `country_code` is 994/994, so Continent
#     derives from it; País stays blank rather than being invented.
#   * `city` — `projectSiteLocCity` exists in the schema and is null
#     throughout (0/45 sampled). The only finer location is a lat/long pair,
#     which goes to `extra`.
#   * `exante_quantity` — `estimatedTotalCredits` is 0 on every project
#     sampled. `estimatedAnnualCredits` is the figure this registry keeps, so
#     the total is left for `excel` to compute from it rather than stored as a
#     zero that would read as "none estimated".
#   * `additional_certification` — no equivalent field.
#     `hasAnotherCarbonProgram` is a boolean with no name attached; it goes to
#     `extra`, where a double-count check can read it.
#   * `region_name` — only Verra publishes one.
#   * `afolu_names` — no sub-type code. `projectTypeCode` is a display code
#     (`FC`, `ODS`, `REF`), not an AFOLU vocabulary, so Tipo Micro keys on the
#     methodology string as Cercarbono's and BioCarbon's do.
#   * `project_size`, `units_issued` — not published.


def normalize_project(
    record: dict[str, Any], detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    detail = detail or {}

    row: dict[str, Any] = {
        "project_id": _project_key(record.get("projectId")),
        # `ACR1275`, unique across all 994 — checked, not assumed — and the
        # reference every serial number is built from.
        "external_id": _stated(record.get("projectId")),
        "project_name": _stated(record.get("projectName")),
        # Asserted. What the registry publishes per project is a
        # `creditingProgram` — ACR, California Air Resources Board, Washington
        # Department of Ecology — which is the compliance programme the credits
        # serve, not the standard they were certified under. It goes to `extra`.
        "standard_name": settings.ACR_STANDARD_NAME,
        # A closed lifecycle vocabulary: ACTIVE, COMPLETED, INACTIVE,
        # TRANSFERRED, TERMINATED, CANCELED. Every one is stored; the column is
        # what says which.
        "status": _stated(record.get("projectStatus")),
        # Tipo Macro: this registry's own vocabulary, untranslated like every
        # other registry's. 17 values, led by "Forest Carbon" (352), "Ozone
        # Depleting Substances" (201) and "Refrigerants" (134).
        "sectoral_scope": _stated(record.get("projectType"))
        or _stated(detail.get("projectTypeName")),
        # The protocol name, 994/994. It carries its own code
        # (`ECY Compliance Offset Protocol: U.S. Forest Projects`,
        # `ACR Methodology for …`), which is what the Tipo Micro rules match on.
        "methodologies": joined(
            (
                record.get("projectMethodology"),
                detail.get("projectMethodologyName"),
            ),
            NOT_STATED,
        ),
        "proponents": _stated(record.get("projectDeveloper"))
        or _stated(detail.get("owningProfileParticipantName")),
        # `_country_code`, not `_stated`: `NA` is Namibia, not "not stated".
        "country_code": _country_code(record.get("country"))
        or _country_code(detail.get("projectSiteLocCountry")),
        # Two vocabularies in one field, and both are the registry's own:
        # `OHIO` beside `US-CA`, and a multi-state project joins them with
        # commas. Carried through as published.
        "state_province": _stated(detail.get("projectSiteLocState")),
        "city": _stated(detail.get("projectSiteLocCity")),
        "credit_period_start": _stated(detail.get("currentCreditingPeriodStartDate")),
        "credit_period_end": _stated(detail.get("currentCreditingPeriodEndDate")),
        # `projectRegistrationDate` is null throughout; `projectStartDate` is
        # 45/45 and is what the registry's own page shows.
        "project_start_date": _stated(detail.get("projectStartDate")),
        "avg_annual_vol_vcu": db.float_or_none(detail.get("estimatedAnnualCredits")),
    }

    extra = {
        # The key every API route and the public URL use. `project_id` is the
        # reference's numeric part, which those routes 404 on, so this is what
        # makes a row's link rebuildable without another scrape.
        "project_key": _stated(record.get("projectKey")),
        # ACR / California Air Resources Board / Washington Department of
        # Ecology. 356 projects serve CARB and 26 serve Ecology — the reason
        # most of this registry's cancellations are conversions.
        "crediting_program": _stated(record.get("creditingProgram")),
        # The id the same project carries in that compliance programme, where
        # it has one. The first thing a cross-registry check reads.
        "compliance_project_id": _stated(
            (record.get("projectExtensionMap") or {}).get("complianceProjectId")
        ),
        "listing_status": _stated(detail.get("projectListingStatus")),
        "project_type_code": _stated(record.get("projectTypeCode"))
        or _stated(detail.get("projectTypeCode")),
        "methodology_key": _stated(detail.get("projectMethodologyMethodologyKey")),
        "developer_profile_key": _stated(record.get("projectDeveloperProfileKey")),
        "verifier": _stated(detail.get("verifierParticipantName")),
        "validator": _stated(detail.get("validatorParticipantName")),
        "latitude": db.float_or_none(detail.get("projectSiteLocLatitude")),
        "longitude": db.float_or_none(detail.get("projectSiteLocLongitude")),
        "coverage_area": db.float_or_none(detail.get("coverageArea")),
        "coverage_area_unit": _stated(detail.get("coverageAreaUnit")),
        "website": _stated(detail.get("website")),
        "initial_crediting_period_start": _stated(
            detail.get("initialCreditingPeriodStartDate")
        ),
        "initial_crediting_period_end": _stated(
            detail.get("initialCreditingPeriodEndDate")
        ),
        # The registry's own double-registration flags. Booleans with no name
        # attached, so they cannot fill `additional_certification` — but they
        # are what says a project is worth checking against another registry.
        "has_another_carbon_program": detail.get("hasAnotherCarbonProgram"),
        "has_another_environmental_market": detail.get("hasAnotherEnvironmentalMarket"),
        "is_registry_transferred": detail.get("isRegistryTransferred"),
        # Which other registry, where that has been established. The flag above
        # says a project has one and never says which; this is the measured
        # answer for the two that do. See ALSO_REGISTERED_AS.
        "also_registered_as": ALSO_REGISTERED_AS.get(
            _stated(record.get("projectId")) or ""
        ),
        # The registry's own per-project figures. They agree with the ledgers
        # on every project sampled, which is why there is no
        # `iter_credit_totals` here — kept so the day they disagree is
        # answerable from the database rather than by re-scraping.
        "stated_holdings_total": db.float_or_none(
            detail.get("projectHoldingsTotalQuantity")
        ),
        "stated_retired_total": db.float_or_none(
            detail.get("projectHoldingsTotalRetiredQuantity")
        ),
        "stated_reserve_total": db.float_or_none(
            detail.get("projectHoldingsTotalReserveQuantity")
        ),
    }
    row["extra"] = {k: v for k, v in extra.items() if _worth_storing(v)} or None
    return row


#: Per ledger: the date field that states when the event happened, and the
#: field carrying the registry's own words for why. An issuance has neither a
#: reason nor a beneficiary — it is the units coming into existence.
CREDIT_FIELDS = {
    ISSUANCES: ("issuanceDate", None),
    RETIREMENTS: ("retirementDate", "retirementPurpose"),
    CANCELLATIONS: ("cancellationDate", "cancellationReasonDisplayName"),
}


def _credit_key(resource: str, record: dict[str, Any], index: int) -> int:
    """A ledger row's `entity_id`, from `holdingKey` where the platform sent one.

    The key is stated on every one of the 15,440 rows measured, and is unique
    within each ledger — 3,358, 10,724 and 1,358 distinct keys against the same
    three row counts. But hashing it unguarded means an unkeyed row hashes to
    `hashed_id(REGISTRY, resource, None)`, which is **one key for all of them**:
    every such row would upsert over the last, and nothing would say so.
    `reconciled()` counts rows *yielded*, not rows written, so it would pass —
    and the sheet would simply be short.

    So an unkeyed row falls back to its own values plus its position in the
    feed, which is what `markit/api.py` does for a platform that publishes no
    ids at all. Positional keys renumber if the registry inserts a row above,
    which is why this is a fallback and not the scheme; the log line is what
    says the fallback was needed.
    """
    if _stated(record.get("holdingKey")) is not None:
        # The raw value, not the trimmed one, so every key already in the
        # database keeps hashing to what it hashed to before this guard existed.
        return hashed_id(REGISTRY, resource, record.get("holdingKey"))
    log.error(
        "An ACR %s row states no holdingKey; keying it on its own values and "
        "position %s instead. Serial %r, project %r.",
        resource,
        index,
        record.get("holdingSerialNumber"),
        record.get("projectReferenceId"),
    )
    return hashed_id(
        REGISTRY,
        resource,
        "unkeyed",
        index,
        record.get("projectReferenceId"),
        record.get("holdingSerialNumber"),
        record.get("holdingQuantity"),
        record.get("vintage"),
    )


def normalize_credit(
    resource: str, record: dict[str, Any], index: int = 0
) -> dict[str, Any]:
    """One ledger row.

    `entity_id` is hashed from `holdingKey` — see `_credit_key` for what
    happens when the platform states none. The resource is part of the seed
    because an issuance block and the holding it later became are two different
    records with two different keys, and nothing should depend on that staying
    true.
    """
    date_field, reason_field = CREDIT_FIELDS[resource]
    return {
        "entity_id": _credit_key(resource, record, index),
        "project_id": _project_key(record.get("projectReferenceId")),
        # **`holdingQuantity`, never `issuanceQuantity`.** The latter is the
        # parent issuance event's total and is repeated on every block of it:
        # 3,358 blocks belong to 2,337 events, and summing it per row reports
        # 604,312,772 units against a true 379,674,647.
        "quantity": db.float_or_none(record.get("holdingQuantity")),
        "vintage": _stated(record.get("vintage")),
        # The only unit class this registry draws: reserve units held against
        # reversal. Null on the issuance blocks — a block becomes reserve when
        # it lands in the buffer pool, which is a holding state rather than an
        # issuance property.
        "unit_type": "Reserve" if record.get("isReserve") else None,
        # The row's own state, in the registry's words: RETIRED, CANCELED, or
        # null on an issuance block. Deliberately not a value
        # `db.credit_totals()` buckets on — it only does that for the resource
        # named `credits`, and each of these resources is already one bucket.
        "status": _stated(record.get("holdingStatus")),
        "event_date": _stated(record.get(date_field)),
        # **`retirementOnBehalfOfName`, not `registryAccountName`.** The
        # account name is on all 10,724 rows and names whoever holds the
        # credits — very often an intermediary retiring for its customers, so
        # every retirement would look like a third-party sale the moment
        # `sold_equals_retired` is flipped. The on-behalf-of field is the third
        # party the registry itself names, 5,167 of 10,724. Same call as
        # BioCarbon's `final_user` over `to_name`.
        "beneficiary": _stated(record.get("retirementOnBehalfOfName")),
        # Retirements state a free-text purpose ("… to offset 2025 emissions"),
        # 10,684 of 10,724. Cancellations state a reason from a closed list, and
        # 1,166 of 1,358 of them are conversions to the ARB or Ecology
        # compliance registries rather than credits ceasing to exist — which is
        # why this column is the only copy of that distinction.
        "reason": _stated(record.get(reason_field)) if reason_field else None,
        # No equivalent. The CORSIA and SDG `registryLabels` are market
        # eligibility and a claim, exactly like Cercarbono's `elegible` list.
        "additional_certification": None,
        "serial_no": _stated(record.get("holdingSerialNumber")),
    }
