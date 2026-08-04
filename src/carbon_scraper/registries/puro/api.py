"""Client for Puro.earth, whose registry is a Next.js server-rendered app.

Contract measured against the live site on 2026-08-05; see
docs/api-contract-puro.md. Three requests carry the entire registry —

    GET {site}/projects        118 projects
    GET {site}/issuances       583 issuance transactions
    GET {site}/retirements   1,519 retirement transactions

— with no paging, no filtering and no API key. The data is not fetched by the
browser at all: the server renders each route and streams its React Server
Component payload inside the HTML, where the lists sit as plain JSON. Three
ways of asking for that payload on its own (`RSC: 1`, `?_rsc=`, a full
`Next-Router-State-Tree`) were each tried and all three return the same
prerendered HTML, so `flight.py` reads it out of the page.

Then one detail page per project, 118 of them, because three things are
published there and nowhere else:

    GET {site}/projects/<code>

* the country **name** — the list carries an ISO code and no name,
* `Issued credits` and `Retired credits`, the only per-project totals this
  registry states,
* nothing else worth having: no state, no city, no estimate.

Its traps, in the order they cost time:

* **A transaction is not a credit record.** Each carries a `bundles` list, and
  a retirement routinely draws from several production facilities at once —
  1,519 retirements are 2,099 bundles. The bundle is what names a facility, so
  a row per transaction would file every multi-facility retirement against
  whichever project happened to be first. Issuances are 1:1 today and that is
  a fact about the data, not a rule: they are read the same way.
* **`countryCode` is `"NA"` for Namibia.** Every other adapter here carries a
  `NOT_STATED` table, and three of them list `na`. Reusing one would delete a
  real country code and take Namibia's Continent with it. Puro states absence
  as JSON `null` throughout and uses no placeholder text, so its table is
  deliberately **empty** — see NOT_STATED below.
* **Withdrawal is a label, not a deduction.** 20 issuances carry
  `FULLY_WITHDRAWN` or `PARTIALLY_WITHDRAWN`, no withdrawn quantity is
  published for either, and the registry's own `Issued credits` counts them in
  full — checked against all 118 detail pages, not assumed. There is therefore
  no cancellation ledger; the label is stored on the issuance row and
  `Total Credits Cancelled` stays blank. See docs/field-mapping.md.
* **The registry publishes no row count anywhere.** Not on the list routes,
  not in a header, not on the home page. Reconciling `len(data)` against
  itself would prove nothing, so the guards here are the ones that can
  actually fail: every bundle's facility must resolve to a known project,
  every transaction's volume must equal its bundles', and every project's
  ledger sum must equal the total its own detail page states.

`registry.puro.earth` is the whole registry: `retired.puro.earth` and
`retirements.puro.earth` are document hosts linked from it, not second feeds.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any

from ... import db, settings
from ...http_client import RegistryClient
from ..base import ClientOwner, reconciled
from ..text import hashed_id, joined, stated
from . import flight

log = logging.getLogger(__name__)

REGISTRY = settings.PURO

ISSUANCES = "issuances"
RETIREMENTS = "retirements"

#: Our ledger name -> the route that publishes it. Both names are chosen
#: deliberately rather than descriptively: `db.credit_totals()` branches on the
#: exact string `credits` to select Gold Standard's bucket-by-`status`
#: semantics, and these rows carry no such status — each route already *is* a
#: bucket, and the two are separate tabs of one view.
LEDGER_PATHS = {
    ISSUANCES: settings.PURO_ISSUANCES_PATH,
    RETIREMENTS: settings.PURO_RETIREMENTS_PATH,
}

LEDGERS = (ISSUANCES, RETIREMENTS)

#: **Deliberately empty, and it must stay that way.** The other adapters here
#: strip registry placeholders — EcoRegistry's `No definido`, the Markit view's
#: `--`, SocialCarbon's `TBD` — and three of those tables also list `na`. Puro
#: has no placeholder vocabulary at all: an unstated field is JSON `null`. What
#: it does have is a project in Namibia, whose `countryCode` is the two-letter
#: string `NA`. Adding the usual entries would blank it, and the loss would
#: show up as one missing Continent rather than as an error.
NOT_STATED: frozenset[str] = frozenset()

#: What the registry calls a withdrawn issuance. No quantity accompanies
#: either, and `Issued credits` on the project's own page counts the units
#: regardless — verified on all 118 projects.
WITHDRAWN_LABELS = ("FULLY_WITHDRAWN", "PARTIALLY_WITHDRAWN")


def _stated(value: Any) -> str | None:
    """Registry text, or None where the registry stated nothing.

    `collapse=False`: these come out of JSON, where a run of two spaces is the
    registry's own and not markup.
    """
    return stated(value, NOT_STATED, collapse=False)


class PuroAPI(ClientOwner):
    """Reads the public Puro registry. Knows nothing about our DB.

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
        #: route -> the `data` array it published, fetched once per run.
        self._records: dict[str, list[dict[str, Any]]] = {}
        #: project code -> what its detail page states, fetched once each.
        self._details: dict[str, dict[str, Any]] = {}

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """What a browser asking for one of these pages sends.

        `settings.BROWSER_HEADERS` is built for the JSON APIs — it asks for
        `application/json` and carries Verra's `Origin`, and sending one
        registry's origin to another has already cost an afternoon on
        Cercarbono. These routes serve HTML, so they are asked for as HTML.
        """
        headers = dict(settings.BROWSER_HEADERS)
        headers.update(
            {
                "Accept": "text/html,application/xhtml+xml",
                "Origin": settings.PURO_SITE,
                "Referer": f"{settings.PURO_SITE}/",
            }
        )
        headers.pop("Content-Type", None)
        return headers

    def _flight(self, path: str) -> str:
        html = self.client.get_text(f"{settings.PURO_SITE}{path}", headers=self._headers())
        return flight.payload(html)

    def _fetch(self, path: str) -> list[dict[str, Any]]:
        """Every record of one route.

        No paging: each route renders its whole resource into one array. A
        route that ever starts paging would show up as a project whose ledger
        sum falls short of the total its own detail page states, which is what
        `iter_credit_totals` checks.
        """
        if path not in self._records:
            self._records[path] = flight.data_list(self._flight(path))
        return self._records[path]

    def _projects(self) -> list[dict[str, Any]]:
        return self._fetch(settings.PURO_PROJECTS_PATH)

    def _detail(self, code: str) -> dict[str, Any]:
        """What one project's page states that its list entry does not.

        One request per project, and the reason a sync is ~121 requests rather
        than 3 — the same trade Cercarbono makes for its crediting period and
        BioCarbon for its estimate. Everything read here is optional: a page
        that has changed shape yields `None`s, which leave cells blank and
        totals unstated rather than writing a wrong value.
        """
        if code in self._details:
            return self._details[code]
        payload = self._flight(settings.PURO_PROJECTS_PATH + f"/{code}")
        self._details[code] = {
            "flight": payload,
            "issued": flight.labelled_number(payload, "Issued credits"),
            "retired": flight.labelled_number(payload, "Retired credits"),
        }
        return self._details[code]

    # -- counts ------------------------------------------------------------

    def project_total(self) -> int:
        """How many projects the page published.

        Self-referential, and deliberately not dressed up as more: Puro states
        no total for any resource, in the body, in a header or on the site.
        The check that can actually fail is in `iter_projects`, where every
        facility named by a credit bundle has to be one of these.
        """
        return len(self._projects())

    def count(self, resource: str) -> int:
        """How many credit rows the route published — bundles, not transactions.

        Same caveat as `project_total`: this is the feed counting itself.
        `iter_credit_totals` holds the reconciliation that means something.
        """
        try:
            path = LEDGER_PATHS[resource]
        except KeyError:
            raise ValueError(f"Puro has no ledger named {resource!r}") from None
        return sum(len(_bundles(record)) for record in self._fetch(path))

    def detail_url(self, project_id: int) -> str:
        return settings.PURO_PROJECT_DETAIL_URL.format(project_id=project_id)

    # -- iteration ---------------------------------------------------------

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        records = self._projects()
        expected = {
            key for key in (_project_key(r.get("code")) for r in records) if key is not None
        }
        yielded: set[int] = set()
        seen = 0

        for record in records:
            key = _project_key(record.get("code"))
            if key is None:
                log.error(
                    "A Puro project has no usable code and cannot be stored: %r",
                    record.get("name"),
                )
                continue
            detail = self._detail(str(record["code"]))
            row = normalize_project(record, detail)
            row["detail_url"] = self.detail_url(key)
            yield row, {"list": record, "stated_totals": _stated_totals(detail)}
            seen += 1
            yielded.add(key)
            if progress is not None:
                progress(seen)
            if max_records is not None and seen >= max_records:
                return

        # Key sets, not counts: a count cannot see one project yielded twice
        # while another is dropped.
        missing = expected - yielded
        if missing:
            log.error(
                "INCOMPLETE: %s Puro project(s) never reached the database.",
                len(missing),
            )
        if max_records is None:
            self._check_facilities_resolve(expected)

    def _check_facilities_resolve(self, known: set[int]) -> None:
        """Every facility a credit bundle names must be a project we stored.

        This is the guard a self-counting feed cannot give: if the project
        route ever truncates, the ledgers keep naming facilities that are no
        longer in it. Cercarbono and the Markit view each have real projects
        that are genuinely absent from the project list, so an orphan is
        logged rather than raised — but on Puro today there are none, across
        all 2,682 bundles.
        """
        for resource, path in LEDGER_PATHS.items():
            if path not in self._records:
                continue
            named = {
                key
                for key in (
                    _project_key(bundle.get("productionFacilityCode"))
                    for record in self._records[path]
                    for bundle in _bundles(record)
                )
                if key is not None
            }
            orphans = named - known
            if orphans:
                log.error(
                    "INCOMPLETE: %s Puro %s bundle(s) name a facility that is "
                    "not in the project list: %s",
                    len(orphans),
                    resource,
                    ", ".join(str(o) for o in sorted(orphans)[:10]),
                )

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        try:
            path = LEDGER_PATHS[resource]
        except KeyError:
            raise ValueError(f"Puro has no ledger named {resource!r}") from None

        records = self._fetch(path)
        rows = (
            normalize_credit(resource, record, bundle)
            for record in records
            for bundle in _checked_bundles(resource, record)
        )
        yield from reconciled(
            rows,
            expected=sum(len(_bundles(record)) for record in records),
            progress=progress,
            max_records=max_records,
            label=f"{REGISTRY} {resource}",
        )

    def iter_credit_totals(self, resource: str) -> Iterator[tuple[int, float, Any]]:
        """Per-project totals the registry states on the project's own page.

        The only counts Puro publishes anywhere, and the only check here that
        can fail: a truncated transaction feed shows up as a ledger sum below
        the stated total, and the stated one then wins in `db.credit_totals()`
        rather than the sheet quietly under-reporting.

        Measured on 2026-08-05: all 118 projects agree exactly with their
        bundles, for both ledgers — 1,819,251 issued and 1,041,121 retired.
        Any disagreement is logged, because on this registry it means the page
        payload changed, not that the registry keeps two sets of books.

        A stated zero is not yielded. 861867 is certified with no credits at
        all, and Plan Vivo's genuinely empty ledgers set the convention: an
        absent figure is blank, not `0`.
        """
        if resource not in LEDGER_PATHS:
            return

        label = "Issued credits" if resource == ISSUANCES else "Retired credits"
        key = "issued" if resource == ISSUANCES else "retired"
        summed = self._ledger_sums(resource)

        for record in self._projects():
            project_id = _project_key(record.get("code"))
            if project_id is None:
                continue
            detail = self._detail(str(record["code"]))
            quantity = detail.get(key)
            if quantity is None:
                log.error(
                    "INCOMPLETE: Puro project %s publishes no %r on its detail "
                    "page; its %s total is left to the ledger rows.",
                    project_id,
                    label,
                    resource,
                )
                continue
            rows, ledger_total = summed.get(project_id, (0, 0.0))
            if ledger_total != quantity:
                log.error(
                    "INCOMPLETE: Puro project %s states %s %s but its bundles "
                    "sum to %s.",
                    project_id,
                    quantity,
                    resource,
                    ledger_total,
                )
            if quantity <= 0:
                continue
            yield project_id, float(quantity), rows

    def _ledger_sums(self, resource: str) -> dict[int, tuple[int, float]]:
        """(bundle count, units) per project, from the rows we are storing."""
        sums: dict[int, tuple[int, float]] = {}
        for record in self._fetch(LEDGER_PATHS[resource]):
            for bundle in _bundles(record):
                project_id = _project_key(bundle.get("productionFacilityCode"))
                if project_id is None:
                    continue
                count, running = sums.get(project_id, (0, 0.0))
                sums[project_id] = (count + 1, running + (db.float_or_none(bundle.get("volume")) or 0.0))
        return sums


def _bundles(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in record.get("bundles") or () if isinstance(b, dict)]


def _checked_bundles(resource: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """This transaction's bundles, having checked they account for all of it.

    A transaction states its own `volume` and its bundles state theirs. The two
    agree on all 2,102 transactions today; the day they stop, a bundle list has
    been truncated and the per-project figures are short by exactly the missing
    rows. Nothing is dropped or invented on a mismatch — the rows are yielded
    and the discrepancy logged, because the registry's per-project totals are
    the authority either way.
    """
    bundles = _bundles(record)
    total = db.float_or_none(record.get("volume"))
    summed = sum(db.float_or_none(b.get("volume")) or 0.0 for b in bundles)
    if total is not None and total != summed:
        log.error(
            "INCOMPLETE: Puro %s %s states %s units but its %s bundle(s) sum "
            "to %s.",
            resource,
            record.get("id"),
            total,
            len(bundles),
            summed,
        )
    return bundles


def _project_key(code: Any) -> int | None:
    """The integer primary key for a project, which is its published code.

    Puro's `projectId` and `code` are the same value on all 118 projects, the
    code is unique across them — checked, not assumed — and it is what the
    public URL, the credit bundles' `productionFacilityCode` and the serials
    are all keyed on. Unlike SocialCarbon's `SOCIALCARBON-N`, this published
    reference really is a key, so nothing is hashed.
    """
    return db.int_or_none(code)


def _stated_totals(detail: dict[str, Any]) -> dict[str, Any]:
    """The detail page's own figures, for the raw snapshot.

    The page payload itself is deliberately not kept: it is ~900 KB per
    project, almost all of it markup and an inline map, and `raw_snapshots`
    would grow by 100 MB to store the same numbers twice.
    """
    return {"issued": detail.get("issued"), "retired": detail.get("retired")}


# -- normalisation ---------------------------------------------------------
# Puro's field names -> the registry-neutral db.PROJECT_FIELDS.
#
# Deliberate gaps, measured over the full 118-project index on 2026-08-05:
#   * `state_province` / `city` — no sub-national field exists anywhere, on
#     the list or on the detail page. A latitude/longitude pair is published
#     for 31 of 118 and goes to `extra`; reading a state out of a coordinate
#     would be inventing one.
#   * `region_name` — only Verra publishes one.
#   * `avg_annual_vol_vcu` / `exante_quantity` — no estimate is published, and
#     none is back-computed from the issuances. Puro certifies removals that
#     have already happened.
#   * `additional_certification` — no equivalent field. The `sdgs` list is a
#     Sustainable Development Goal claim, not a co-certification, and goes to
#     `extra`; this is the same call Cercarbono's `elegible` list gets.
#   * `afolu_names` — no sub-type code, and no AFOLU projects to give one to.
#   * `status` — not published on the list. Every detail page sampled renders
#     the same "Certified project" pill, which is what the registry *is*
#     rather than a per-project state.
#   * `project_size`, `units_issued`, `credit_period_term` — not published.


def normalize_project(
    record: dict[str, Any], detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    detail = detail or {}
    methodology = record.get("methodology") or {}
    general_rules = record.get("generalRules") or {}
    country_code = _stated(record.get("countryCode"))

    row: dict[str, Any] = {
        "project_id": _project_key(record.get("code")),
        # The same number as the key. Puro publishes one reference and uses it
        # everywhere — unlike BioCarbon, where the human reference and the
        # numeric id are different values.
        "external_id": _stated(record.get("code")),
        "project_name": _stated(record.get("name")),
        # Asserted. The registry names a rule set version per project and
        # never names the standard; the version goes to `extra`.
        "standard_name": settings.PURO_STANDARD_NAME,
        # **Tipo Macro and Metodologia are the same string here, deliberately.**
        # Puro publishes exactly one classification of what a project does —
        # the methodology — and no sector vocabulary at all. Carried through
        # untranslated like every other registry's: "Biochar, 2022", "Wooden
        # Building Elements", "Enhanced Rock Weathering, 2022", "Carbonated
        # Materials", "Terrestrial Storage of Biomass", "Geologically stored
        # carbon", "Soil Amendment". The edition year is part of some names and
        # not others — the registry's own inconsistency, which is why the
        # derivation rules match on a stem rather than on equality.
        "sectoral_scope": _methodology_name(record),
        "methodologies": _methodology_name(record),
        "proponents": _stated(record.get("supplierName")),
        # **Published only on the detail page**, and read there beside the
        # flag its own ISO code builds. The list route carries the code alone.
        "country_name": flight.flagged_country(detail.get("flight") or "", country_code),
        "country_code": country_code,
        "credit_period_start": _stated(record.get("creditingPeriodStart")),
        "credit_period_end": _stated(record.get("creditingPeriodEnd")),
    }

    extra = {
        # A Global Service Relation Number — the energy-market identifier the
        # platform issues a production facility. 79 of 118, and not a
        # replacement for the code: it is absent on a third of the registry.
        "gsrn": _stated(record.get("gsrn")),
        "methodology_code": _stated(record.get("methodologyCode")),
        "methodology_edition": _stated(methodology.get("edition")),
        "methodology_url": _stated(methodology.get("url")),
        "general_rules_version": _stated(general_rules.get("version")),
        "general_rules_url": _stated(general_rules.get("url")),
        "supplier_listing_url": _stated(record.get("supplierListingUrl")),
        "latitude": db.float_or_none(record.get("latitude")),
        "longitude": db.float_or_none(record.get("longitude")),
        "sdgs": _sdgs(record),
    }
    row["extra"] = {k: v for k, v in extra.items() if v not in (None, "", [], {})} or None
    return row


def _methodology_name(record: dict[str, Any]) -> str | None:
    """The methodology's published name, or its code if the name is missing.

    The name is what carries the vocabulary; the code (`C03000000`) says
    nothing to anyone reading the sheet. Both are kept — the code in `extra` —
    because the code is the stable half: `Geologically stored carbon` and
    `Geologically stored carbon, 2024` are two editions of one methodology and
    two different names.
    """
    methodology = record.get("methodology") or {}
    return _stated(methodology.get("name")) or _stated(record.get("methodologyCode"))


def _sdgs(record: dict[str, Any]) -> str | None:
    """The Sustainable Development Goals claimed, as `13 Climate action`.

    111 of 118 projects claim at least one. Not an additional certification and
    deliberately not written into that column.
    """
    return joined(
        (
            f"{goal.get('goal')} {goal.get('name')}".strip()
            for goal in record.get("sdgs") or ()
            if isinstance(goal, dict)
        ),
        NOT_STATED,
    )


def normalize_credit(
    resource: str, record: dict[str, Any], bundle: dict[str, Any]
) -> dict[str, Any]:
    """One credit bundle — **not** one transaction.

    `entity_id` is hashed from the bundle's certificate serial, which is unique
    across the feed: 583 of 583 issuance serials and 2,099 of 2,099 retirement
    serials. The resource is part of the seed so an issuance and a retirement
    of the same units can never collide.
    """
    details = record.get("retirementDetails") or {}
    serial = _stated(bundle.get("certificates"))
    return {
        "entity_id": hashed_id(REGISTRY, resource, serial),
        "project_id": _project_key(bundle.get("productionFacilityCode")),
        "quantity": db.float_or_none(bundle.get("volume")),
        "vintage": _stated(bundle.get("vintage")),
        # The credit class, which is Puro's durability claim in a word:
        # `CORC20+`, `CORC100+`, `CORC1000+`, and a bare `CORC` for the credits
        # issued before the classes existed. `CORC_100` appears on 15
        # retirement bundles and nowhere else — an older spelling of the same
        # class, carried through as published rather than normalised.
        "unit_type": _stated(bundle.get("creditType")),
        "status": _credit_status(resource, record),
        "event_date": _event_date(resource, record, bundle),
        # The third party the units were retired for, which is **not** the
        # account holder: those agree on only 88 of 1,519 rows. Same call as
        # BioCarbon's `final_user` over `to_name` and SocialCarbon's
        # `Beneficiary` over `Retiree`.
        #
        # Blank on 51 rows, all of them under an embargo the registry states
        # and honours — see `beneficiaryHiddenUntil` in the contract doc. The
        # name appears on a later sync, which is why nothing is invented here.
        "beneficiary": _stated(details.get("beneficiaryName")),
        # The retirement's own words. `credit_events` keeps no raw payload, so
        # this column is the only copy — the same reason SocialCarbon's `Notes`
        # are stored whole.
        "reason": _stated(details.get("retirementPurpose")),
        "additional_certification": None,
        "serial_no": serial,
    }


def _credit_status(resource: str, record: dict[str, Any]) -> str | None:
    """What the registry says about this row's own state.

    Two different things, one per ledger, and both are the registry's own
    closed vocabulary:

    * an issuance carries `FULLY_WITHDRAWN` or `PARTIALLY_WITHDRAWN` when the
      units have been withdrawn — 20 of 583. **No withdrawn quantity is
      published**, and the registry's own issued total counts them anyway, so
      this label is the only record that it happened.
    * a retirement carries its `usageType`: `GENERIC_COMPENSATION`,
      `BUNDLED_WITH_PRODUCT_OR_SERVICE`, `SPECIFIC_ACTIVITY_LIKE_FLIGHTS`,
      `DISCLOSURE`, `SUPPORT` or `OTHER`, on 1,519 of 1,519.

    Deliberately not a value `db.credit_totals()` buckets on: it only does that
    for the resource named `credits`.
    """
    if resource == RETIREMENTS:
        return _stated((record.get("retirementDetails") or {}).get("usageType"))
    labels = [
        label.get("type")
        for label in record.get("transactionAdditionalLabels") or ()
        if isinstance(label, dict)
    ]
    return joined((l for l in labels if l in WITHDRAWN_LABELS), NOT_STATED)


def _event_date(
    resource: str, record: dict[str, Any], bundle: dict[str, Any]
) -> str | None:
    """When the transaction completed.

    An issuance states its date twice — `issuanceDetails.issuanceDate` as a
    timestamp and `bundle.issuanceDate` as a plain date — and the bundle's is
    preferred because it is the one the registry's own table shows. A
    retirement states only `completedOn`.
    """
    if resource == ISSUANCES:
        issuance = record.get("issuanceDetails") or {}
        return _stated(bundle.get("issuanceDate")) or _stated(issuance.get("issuanceDate"))
    return _stated(record.get("completedOn"))
