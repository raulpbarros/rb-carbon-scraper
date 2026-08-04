"""Client for SocialCarbon, which runs on its own Bubble.io application.

Contract measured against the live service on 2026-08-04; see
docs/api-contract-socialcarbon.md. It is the friendliest registry here by a
distance — an open, unauthenticated Data API, no key, no browser `User-Agent`
required, no Cloudflare, and the whole registry in four requests:

    GET {api}/meta                                the readable types
    GET {api}/obj/project?limit=100&cursor=0       19 projects
    GET {api}/obj/issuance                         17 issuances
    GET {api}/obj/retirement                       81 retirements
    GET {api}/obj/cancellations                     2 cancellations

Every response is `{"response": {cursor, results, count, remaining}}`, and
`count + remaining` is the registry's own total — stated on every page, which
is what reconciliation reads.

Three traps, each the shape of one already in CLAUDE.md:

* **`Project ID` is not unique and is not a key.** Two entirely different
  projects both publish `SOCIALCARBON-19` — a peatland programme in Poland and
  a forest project in Brazil — and `SOCIALCARBON-15` is absent. 19 records,
  18 distinct references. This is Markit's "35 rows are 30 projects" inverted:
  there the repeated id *was* one project and rows had to be merged, here
  merging would fuse two countries into one row. The primary key is hashed
  from Bubble's own `_id`; see `project_key`.
* **`asset` is not a ledger.** Its 22 rows are 17 mirrors of the issuances,
  summing to the identical 189,794 units, plus 5 rows reading
  `"Standard": "VCS"` with no project link — Verra credits deposited into this
  platform's tokenisation layer. Scraping it would double every issued figure
  *and* import another registry's credits. It is deliberately not in LEDGERS.
* **`limit` clamps to 100 in silence.** Asking 200 of a 147-row type returns
  100 with `remaining: 47` at HTTP 200, no marker. Third registry here to
  ignore a page size, so `_fetch` pages on `remaining` rather than on having
  got what it asked for.

What is *not* a trap, unusually: Bubble validates `constraints`. A bogus field
name answers HTTP 404 `Field not found <name> for type project` rather than
returning the whole index, so this is the first registry here that refuses a
filter loudly. No filter is used regardless — the registry fits in one page per
type — but it is worth knowing before anyone needs to partition it.

Four of the readable types are account data (`billing`, `accountmanager`,
`organisationdetails`, `user`). Nothing in the spreadsheet asks for them and
they are not read.
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

log = logging.getLogger(__name__)

REGISTRY = settings.SOCIAL_CARBON

ISSUANCES = "issuances"
RETIREMENTS = "retirements"
CANCELLATIONS = "cancellations"

#: Our ledger name -> the Bubble type behind it. The names on the left are
#: chosen deliberately rather than descriptively: `db.credit_totals()` branches
#: on the exact string `credits` to select Gold Standard's bucket-by-`status`
#: semantics, and these rows carry no such status — each Bubble type already
#: *is* a bucket. Any other name selects the one-ledger-per-resource path,
#: which is what Verra's and Cercarbono's ledgers use.
#:
#: Bubble spells two of them singular and one plural. That is the registry's
#: own naming, kept here rather than in the ledger names, which are ours.
LEDGER_TYPES = {
    ISSUANCES: "issuance",
    RETIREMENTS: "retirement",
    CANCELLATIONS: "cancellations",
}

LEDGERS = (ISSUANCES, RETIREMENTS, CANCELLATIONS)

#: Values this registry uses to mean "not filled in yet". `TBD`/`TBC` sit in
#: `validator` and `verifier` on every project that has not been through
#: validation, and they are a placeholder, not the name of a body.
NOT_STATED = {"tbd", "tbc", "n/a", "na", "none", "not applicable"}


def _stated(value: Any) -> str | None:
    """Registry text, or None where the registry stated nothing.

    `collapse=False`: these come out of JSON, where a run of two spaces is the
    registry's own and not markup.
    """
    return stated(value, NOT_STATED, collapse=False)


def _joined(values: Any) -> str | None:
    """Distinct values in first-seen order, joined.

    Bubble returns several project fields as lists even where only one entry
    has ever been used — `Project Proponent(s)_TEXT` among them.
    """
    if isinstance(values, (str, bytes)) or values is None:
        return _stated(values)
    return joined(values, NOT_STATED, collapse=False)


def project_key(bubble_id: Any) -> int | None:
    """The integer primary key for a project, hashed from Bubble's record id.

    **Not derived from `Project ID`.** That is the registry's human reference
    (`SOCIALCARBON-7`) and it is *not unique*: two different projects publish
    `SOCIALCARBON-19`. Keying on it would collapse them into one row, which is
    exactly the failure the Markit adapter guards against from the other
    direction.

    Hashed rather than counted so the key survives the registry inserting a
    project: `credit_events.project_id` is an integer, Bubble's id is a string,
    and a positional key would renumber every row on the next sync. The
    registry name is part of the seed so a hashed key can never collide with a
    platform-issued numeric id from another adapter.
    """
    text = _stated(bubble_id)
    if text is None:
        return None
    return hashed_id(REGISTRY, text)


class SocialCarbonAPI(ClientOwner):
    """Talks to the public Bubble Data API. Knows nothing about our database.

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
        #: Bubble type -> every record of it, fetched once per run.
        self._records: dict[str, list[dict[str, Any]]] = {}
        #: Bubble type -> the total the registry stated while fetching it.
        self._totals: dict[str, int] = {}

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """This registry's own site in Origin/Referer.

        No header is *required* here — the API answers a request with no
        User-Agent at all — but `settings.BROWSER_HEADERS` carries Verra's
        site, and sending one registry's Origin to another has already cost an
        afternoon on Cercarbono. Send what this registry's own page sends.
        """
        headers = dict(settings.BROWSER_HEADERS)
        headers["Origin"] = settings.SOCIALCARBON_SITE
        headers["Referer"] = f"{settings.SOCIALCARBON_SITE}/"
        return headers

    def _page(self, bubble_type: str, cursor: int) -> dict[str, Any]:
        url = f"{settings.SOCIALCARBON_API}/obj/{bubble_type}"
        params = {"limit": settings.SOCIALCARBON_PAGE_SIZE, "cursor": cursor}
        body = self.client.get_json(url, params=params, headers=self._headers())
        response = (body or {}).get("response") if isinstance(body, dict) else None
        if not isinstance(response, dict):
            # Bubble reports a bad type or field as a JSON error object rather
            # than an envelope. Drop it from the cache: a 4xx is not cached,
            # but a malformed 200 would be, and the next run would replay it.
            self.client.invalidate(
                "GET", url, params=params, headers=self._headers()
            )
            raise ValueError(f"{bubble_type} returned no response envelope: {body!r}")
        return response

    def _fetch(self, bubble_type: str) -> list[dict[str, Any]]:
        """Every record of one Bubble type, paged on `remaining`.

        Paging is driven by the registry's own `remaining` rather than by
        comparing the page size to what was asked for, because `limit` is
        clamped to 100 without saying so. Today every type answers in one page;
        this is written for the day one does not.
        """
        if bubble_type in self._records:
            return self._records[bubble_type]

        records: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = self._page(bubble_type, cursor)
            results = list(page.get("results") or [])
            count = db.int_or_none(page.get("count")) or len(results)
            remaining = db.int_or_none(page.get("remaining")) or 0
            # Stated on every page: what came back plus what is still to come.
            self._totals[bubble_type] = cursor + count + remaining
            records.extend(results)
            if remaining <= 0:
                break
            if not results:
                # `remaining` promises more and the page delivered none. Paging
                # on regardless would spin forever.
                log.error(
                    "INCOMPLETE: %s returned an empty page at cursor %s with %s "
                    "records still promised.",
                    bubble_type,
                    cursor,
                    remaining,
                )
                break
            cursor += len(results)

        self._records[bubble_type] = records
        return records

    def _total(self, bubble_type: str) -> int:
        """The registry's own count for a type — `count + remaining`."""
        if bubble_type not in self._totals:
            self._fetch(bubble_type)
        return self._totals.get(bubble_type, 0)

    # -- counts ------------------------------------------------------------

    def project_total(self) -> int:
        return self._total("project")

    def count(self, resource: str) -> int:
        return self._total(self._bubble_type(resource))

    @staticmethod
    def _bubble_type(resource: str) -> str:
        try:
            return LEDGER_TYPES[resource]
        except KeyError:
            raise ValueError(
                f"SocialCarbon has no ledger named {resource!r}"
            ) from None

    def detail_url(self, bubble_id: str) -> str:
        """The public page for one project.

        Keyed on Bubble's record id, which is what the app's own route uses —
        not on our hashed `project_id` and not on the `SOCIALCARBON-N`
        reference. This is why every project row carries a `detail_url`: the
        fallback in `settings.PROJECT_DETAIL_URLS` formats `project_id`, and
        for this registry that is a hash.
        """
        return settings.SOCIALCARBON_PROJECT_DETAIL_URL.format(project_id=bubble_id)

    # -- iteration ---------------------------------------------------------

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        records = self._fetch("project")
        expected = {
            key
            for key in (project_key(record.get("_id")) for record in records)
            if key is not None
        }
        yielded: set[int] = set()
        seen = 0

        for record in records:
            key = project_key(record.get("_id"))
            if key is None:
                log.error(
                    "A SocialCarbon project has no usable id and cannot be "
                    "stored: %r",
                    record.get("Project ID") or record.get("Project Name"),
                )
                continue
            row = normalize_project(record)
            row["detail_url"] = self.detail_url(str(record.get("_id")))
            yield row, record
            seen += 1
            yielded.add(key)
            if progress is not None:
                progress(seen)
            if max_records is not None and seen >= max_records:
                return

        # Compared as key sets rather than as counts, the way Cercarbono's is.
        # A count cannot see one project yielded twice under one key while
        # another is dropped — and this registry has already been caught
        # publishing one reference for two projects, so that is not a
        # hypothetical here.
        missing = expected - yielded
        if missing:
            log.error(
                "INCOMPLETE: %s SocialCarbon project(s) never reached the "
                "database.",
                len(missing),
            )

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        bubble_type = self._bubble_type(resource)
        rows = (
            normalize_credit(resource, record) for record in self._fetch(bubble_type)
        )
        yield from reconciled(
            rows,
            expected=self._total(bubble_type),
            progress=progress,
            max_records=max_records,
            label=f"{REGISTRY} {resource}",
        )

    def iter_credit_totals(self, resource: str) -> Iterator[tuple[int, float, Any]]:
        """Issued units per project, counting only what the registry approved.

        Every issuance row is stored, because it is published and dropping it
        would make the ledger disagree with the registry. But an issuance
        carries two of the registry's own flags — `Approved` and `Issuance
        complete` — and a row with either unset is a *request*, not units in
        existence. Summing the ledger blindly would report those as issued
        credits.

        All 17 rows are approved and complete today, so this states exactly
        what the rows sum to (189,794) and changes no figure. It exists so the
        first pending request does not quietly inflate a project's issued
        total, which is the same reason `credit_totals` outranks row sums
        everywhere else in this codebase.
        """
        if resource != ISSUANCES:
            return
        totals: dict[int, tuple[float, int]] = {}
        for record in self._fetch(LEDGER_TYPES[ISSUANCES]):
            if not (record.get("Approved") and record.get("Issuance complete")):
                continue
            key = project_key(record.get("Project"))
            if key is None:
                continue
            quantity = db.float_or_none(record.get("Quantity requested")) or 0.0
            running, events = totals.get(key, (0.0, 0))
            totals[key] = (running + quantity, events + 1)
        for key, (quantity, events) in sorted(totals.items()):
            yield key, quantity, events


# -- normalisation ---------------------------------------------------------
# SocialCarbon's field names -> the registry-neutral db.PROJECT_FIELDS.
#
# Deliberate gaps, measured over the full 19-project index on 2026-08-04:
#   * `state_province` / `city` — not published. `Address` is a free-text
#     string plus a lat/lng pair (14/19); both go to `extra`. Reading a state
#     out of "XG3P+H8, South Africa" would be inventing one.
#   * `country_code` — no ISO code anywhere, so Continent is derived from the
#     country name; see config/derivation/continent.yaml.
#   * `exante_quantity` — not published. `Estimated Annual Emission
#     Reductions` is the yearly figure and derive.py builds the total from it.
#   * `additional_certification` — no equivalent field. `CORSIA eligible` on
#     an issuance is market eligibility, exactly like Cercarbono's `elegible`
#     list, and is deliberately not used as a co-certification.
#   * `region_name` — only Verra publishes one.
#   * `afolu_names`, `project_size` — not published.


def normalize_project(record: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "project_id": project_key(record.get("_id")),
        # The human reference, published as-is including its duplicate. Two
        # projects really do read SOCIALCARBON-19; that is the registry's, and
        # it is raised in docs/field-mapping.md rather than repaired here.
        "external_id": _stated(record.get("Project ID")),
        "project_name": _stated(record.get("Project Name")),
        "standard_name": _stated(record.get("Standard")),
        "status": _stated(record.get("Project Status")),
        # Tipo Macro: this registry's own vocabulary, carried through
        # untranslated like every other registry's. Three wordings appear —
        # "Agriculture Forestry and Other Land Use", the bare "AFOLU", and
        # "Harmful Algae Bloom Treatment". The first two are the biome
        # ruleset's business; see config/derivation/biome.yaml.
        "sectoral_scope": _stated(record.get("Project Type")),
        "methodologies": _stated(record.get("Methodology")),
        "proponents": _joined(record.get("Project Proponent(s)_TEXT")),
        "country_name": _stated(record.get("Country")),
        "area": db.float_or_none(record.get("Total Project Area")),
        "credit_period_start": _stated(record.get("Crediting period start")),
        "credit_period_end": _stated(record.get("Crediting period end")),
        "project_start_date": _stated(record.get("Start Date")),
        "project_end_date": _stated(record.get("End Date")),
        "avg_annual_vol_vcu": db.float_or_none(
            record.get("Estimated Annual Emission Reductions")
        ),
    }

    address = record.get("Address") or {}
    extra = {
        # The only unique identifier this registry publishes, and the one its
        # own project page is routed off. Keep it: `project_id` is a hash of
        # it and cannot be read back.
        "bubble_id": _stated(record.get("_id")),
        "address": _stated(address.get("address")),
        "latitude": db.float_or_none(record.get("Latitude")),
        "longitude": db.float_or_none(record.get("Longitude")),
        "validator": _stated(record.get("validator")),
        "verifier": _stated(record.get("verifier")),
        "sdgs": record.get("SDGs") or None,
        "documents_approved": record.get("Project documents approved"),
        "created_date": _stated(record.get("Created Date")),
    }
    row["extra"] = {k: v for k, v in extra.items() if v not in (None, "", [], {})} or None
    return row


#: Per ledger: (quantity field, unit-type field, serial field). The three
#: Bubble types spell the same ideas three slightly different ways —
#: `Quantity requested` against `Quantity`, `Asset type` against `Asset Type`,
#: `Serial Number Batch` against `Serial Numbers` — which is a naming
#: accident, not a difference in meaning.
CREDIT_FIELDS = {
    ISSUANCES: ("Quantity requested", "Asset type", "Serial Number Batch"),
    RETIREMENTS: ("Quantity", "Asset Type", "Serial Numbers"),
    CANCELLATIONS: ("Quantity", "Asset Type", "Serial Numbers"),
}


def normalize_credit(resource: str, record: dict[str, Any]) -> dict[str, Any]:
    """One ledger row.

    `entity_id` is hashed from Bubble's own record id rather than from the
    row's values: unlike the Markit ledgers these rows *do* carry a unique id,
    so nothing has to be reconstructed. The resource is part of the seed so an
    issuance and a retirement can never collide.

    `event_date` is `Created Date` throughout. It is the only date every ledger
    publishes — an issuance additionally states its monitoring period, which is
    what the units are *for* rather than when they were issued, and that is
    kept in the raw snapshot.
    """
    quantity_field, unit_field, serial_field = CREDIT_FIELDS[resource]
    bubble_id = _stated(record.get("_id"))
    return {
        "entity_id": hashed_id(REGISTRY, resource, bubble_id),
        "project_id": project_key(record.get("Project")),
        "quantity": db.float_or_none(record.get(quantity_field)),
        "vintage": _stated(record.get("Vintage")),
        # "SCU - Removal" throughout today. Stored rather than assumed, so a
        # future avoidance unit is visible without a re-scrape.
        "unit_type": _stated(record.get(unit_field)),
        "status": _credit_status(resource, record),
        "event_date": _stated(record.get("Created Date")),
        # Structured fields only (user's decision, 2026-08-04). 20 of 81
        # retirements fill `Beneficiary`; the rest carry the same text as prose
        # inside `Notes` ("Beneficiary: …, Purpose: …"), which is not parsed —
        # a wording change would stop matching in silence.
        #
        # **Not `Retiree`.** That is the account that retired the units, filled
        # on all 81 rows, and it is not the third party they were retired for.
        # Falling back to it would make every retirement look like a
        # third-party sale the moment `sold_equals_retired` is flipped.
        "beneficiary": _stated(record.get("Beneficiary")),
        # The whole note, not just `Purpose`. `credit_events` keeps no raw
        # payload — only projects do — so this column is the only place the
        # prose survives, and it is what makes the decision above reversible
        # without a re-scrape.
        "reason": _stated(record.get("Notes")) or _stated(record.get("Purpose")),
        "additional_certification": None,
        "serial_no": _stated(record.get(serial_field)),
    }


def _credit_status(resource: str, record: dict[str, Any]) -> str | None:
    """What the registry says about this row's own state.

    Only issuances have one: `Approved` and `Issuance complete` together say
    whether units exist or whether this is still a request. Retirements and
    cancellations are the event itself, so the ledger name is the state — and
    deliberately not a value `db.credit_totals()` would bucket on, which it
    only does for the resource named `credits`.
    """
    if resource != ISSUANCES:
        return None
    if record.get("Approved") and record.get("Issuance complete"):
        return "Issued"
    return "Pending"
