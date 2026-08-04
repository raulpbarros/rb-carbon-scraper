"""Client for BioCarbon Registry, which publishes through Global CarbonTrace.

Contract measured against the live service on 2026-08-04; see
docs/api-contract-biocarbon.md. A Laravel API behind a Vue SPA:

    GET {api}/api/ghg/projects?per_page=1000&page=1        105 projects
    GET {api}/api/ghg/carbon-credits?per_page=1000         626 issuance blocks
    GET {api}/api/ghg/retreats?per_page=1000              11,439 retirements
    GET {api}/api/ghg/projects/{id}                    the per-project detail
    GET {api}/api/ghg/carbon-credits/project/{id}/cancellations

Every list response is a Laravel paginator — `{current_page, data, last_page,
per_page, total}` — and `total` is the registry's own count, which is what
reconciliation reads.

**Global CarbonTrace is a platform, not a registry.** Its own bundle declares
one programme (`biocarbon`) with three categories: `gei`, `biodiversity` and
`water`. Only `gei` is tCO2e, and it is the only one scraped — the same call
as Cercarbono's, where EcoRegistry's biodiversity and circular-economy
standards are left out. The other two hold 3 projects and no credits at all
today.

Four things that will cost an afternoon if skipped:

* **A public API key is required, and the refusal arrives at HTTP 200.**
  Without `x-api-key` the body is `{"status": 403, "data": [],
  "message": "You do not have permissions to perform this action."}` under a
  200 status line. Checking the status code cannot see it, and an empty `data`
  looks exactly like an empty registry — the EcoRegistry trap precisely. The
  key is shipped in the site's own `assets/api-*.js` and sent by every
  anonymous visitor, the same posture as Verra's `appkey`.
* **`/api/public/*` and `/api/ghg/*` are the same data.** Identical totals on
  all four resources. Only the `ghg` prefix has the per-project routes, so the
  adapter speaks `ghg` throughout rather than mixing two names for one thing.
* **`transferences` is not a ledger.** Its 714 rows are holder-to-holder
  moves of units already issued (20,406,121 of them). Scraping it as a credit
  event would add a fifth bucket that double-counts the issuances, which is
  the same shape as SocialCarbon's `asset` feed.
* **Two feeds disagree about cancellations, and the smaller one is the one
  with dates.** The per-project `cancellations` endpoint publishes 3 rows
  across 2 projects (477,859 units); 14 credit blocks across 9 projects carry
  a nonzero `dropouts` totalling 584,940. The rows are what the endpoint
  states and are stored as such; `iter_credit_totals(CANCELLATIONS)` states
  the `dropouts` figure, which outranks them. Same seam, same reason, as
  Cercarbono's `certificatedVerification`.

And one thing that is *not* a trap, for once: **`per_page` is honoured.**
100, 200, 500, 1000, 2000 and 5000 all return exactly what was asked for, with
`last_page` adjusted to match. This is the first registry here that does not
silently clamp or ignore a page size. 1000 is politeness, not a ceiling.
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

REGISTRY = settings.BIOCARBON

ISSUANCES = "issuances"
RETIREMENTS = "retirements"
CANCELLATIONS = "cancellations"

#: Our ledger name -> the platform's own resource path. The names on the left
#: are chosen rather than described: `db.credit_totals()` branches on the exact
#: string `credits` to select Gold Standard's bucket-by-`status` semantics, and
#: these rows carry no such status — each resource already *is* a bucket.
#:
#: `cancellations` has no bulk resource at all. It is reachable only per
#: project, so it is absent here and swept in `_cancellations()`.
LEDGER_RESOURCES = {
    ISSUANCES: "carbon-credits",
    RETIREMENTS: "retreats",
}

LEDGERS = (ISSUANCES, RETIREMENTS, CANCELLATIONS)

PROJECTS = "projects"

#: Values this registry uses to mean "not filled in yet".
NOT_STATED = {"n/a", "na", "none", "null", "no aplica", "-", "--"}

#: Asserted, not published per row — but `applicable_standard` says exactly
#: this on all 105 projects, so it is a fallback rather than an invention.
STANDARD_NAME = "BioCarbon Standard"

#: BioCarbon initiative id -> the Cercarbono reference for the same physical
#: project. **Measured, not published by this registry.** Cercarbono's own
#: `CDC-106` and `CDC-107` records carry
#: `converted_from: BioCarbon` and a `converted_from_link` naming
#: `biocarbonregistry.com/en/project/?id=24` and `?id=31`, which are these two
#: initiative ids. Both projects are still `Registered` here with credits of
#: their own — 3,945,085 and 8,029,639 issued against Cercarbono's 79,450 and
#: 170,550 — so the two registries publish different tranches of one project.
#:
#: Both rows ship (user's decision, 2026-08-04). Nothing is summed across
#: registries and nothing is dropped; this table is what makes the duplicate
#: visible from the BioCarbon side, since the linkage is published only from
#: Cercarbono's. See docs/field-mapping.md.
ALSO_REGISTERED_AS = {
    24: "CERCARBONO CDC-106",
    31: "CERCARBONO CDC-107",
}


def _stated(value: Any) -> str | None:
    """Registry text, or None where the registry stated nothing.

    `collapse=True`: several detail fields are multi-line textarea content
    (`participants` separates names with CRLF), and a run of whitespace there
    is layout rather than data.
    """
    return stated(value, NOT_STATED)


def _number(value: Any) -> float | None:
    """A quantity, which this API states as a thousands-separated string.

    `amount` reads `"299,564"` on the bulk feed and `299564` on some detail
    routes, and `verified_reductions` is `"751,927"` while `total_reductions`
    beside it is a bare int. Stripping the separator is not optional: without
    it `float("299,564")` raises and the row is silently dropped by any caller
    that catches.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    return db.float_or_none(text)


class BioCarbonAPI(ClientOwner):
    """Talks to the public Global CarbonTrace API. Knows nothing about our DB.

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
        #: platform resource -> every record of it, fetched once per run.
        self._records: dict[str, list[dict[str, Any]]] = {}
        #: platform resource -> the total the paginator stated while fetching.
        self._totals: dict[str, int] = {}
        #: initiative id -> its detail payload, fetched once per project.
        self._details: dict[int, dict[str, Any]] = {}
        #: The per-project cancellation sweep, once per run.
        self._cancellation_rows: list[dict[str, Any]] | None = None

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """What the site's own bundle sends, and nothing more.

        `settings.BROWSER_HEADERS` carries Verra's `Origin`, and sending one
        registry's origin to another has already cost an afternoon on
        Cercarbono. `x-api-key` is the public client key from this site's own
        bundle; without it every call is a 403 inside a 200.
        """
        headers = dict(settings.BROWSER_HEADERS)
        headers["Origin"] = settings.BIOCARBON_SITE
        headers["Referer"] = f"{settings.BIOCARBON_SITE}/"
        headers["x-api-key"] = settings.BIOCARBON_API_KEY
        return headers

    def _url(self, path: str) -> str:
        return f"{settings.BIOCARBON_API}/api/{settings.BIOCARBON_PROGRAM}/{path}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._url(path)
        headers = self._headers()
        body = self.client.get_json(url, params=params, headers=headers)
        # The refusal is an HTTP 200 carrying `status: 403`. Raise on it rather
        # than reading `data: []` as an empty registry, and drop it from the
        # cache first — a 4xx is not cached, but a 200 is, so the next run
        # would replay the refusal without ever reaching the network.
        if isinstance(body, dict):
            status = db.int_or_none(body.get("status"))
            if status is not None and status >= 400:
                self.client.invalidate("GET", url, params=params, headers=headers)
                raise RuntimeError(
                    f"BioCarbon refused {path} with status {status} inside an "
                    f"HTTP 200: {body.get('message')!r}"
                )
        return body

    def _page(self, resource: str, page: int) -> dict[str, Any]:
        params = {"per_page": settings.BIOCARBON_PAGE_SIZE, "page": page}
        body = self._get(resource, params)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError(f"{resource} returned no paginator: {body!r}")
        return body

    def _fetch(self, resource: str) -> list[dict[str, Any]]:
        """Every record of one platform resource, paged on `last_page`.

        Paging is by `last_page` rather than by "a short page ends it" because
        this paginator states both, and they agree — `per_page` is honoured, so
        a short page really is the last one. Reconciliation against `total`
        catches the day that stops being true.
        """
        if resource in self._records:
            return self._records[resource]

        records: list[dict[str, Any]] = []
        page = 1
        while True:
            body = self._page(resource, page)
            rows = list(body.get("data") or [])
            self._totals[resource] = db.int_or_none(body.get("total")) or len(rows)
            records.extend(rows)
            last_page = db.int_or_none(body.get("last_page")) or page
            if page >= last_page:
                break
            if not rows:
                # `last_page` promises more and this page delivered none.
                # Paging on would spin forever.
                log.error(
                    "INCOMPLETE: %s returned an empty page %s of %s.",
                    resource,
                    page,
                    last_page,
                )
                break
            page += 1

        self._records[resource] = records
        return records

    def _total(self, resource: str) -> int:
        if resource not in self._totals:
            self._fetch(resource)
        return self._totals.get(resource, 0)

    def _detail(self, initiative_id: int) -> dict[str, Any]:
        """One project's detail record, which the list does not carry.

        The only source of `total_reductions_general` (the estimate over the
        whole quantification period), of the migration provenance, and of the
        standard name. One request per project, which is the whole reason a
        sync is ~225 requests rather than 14 — the same trade Cercarbono makes
        for its crediting period.
        """
        if initiative_id in self._details:
            return self._details[initiative_id]
        body = self._get(f"projects/{initiative_id}")
        detail = (body or {}).get("initiative") if isinstance(body, dict) else None
        self._details[initiative_id] = detail if isinstance(detail, dict) else {}
        return self._details[initiative_id]

    def _cancellations(self) -> list[dict[str, Any]]:
        """Every cancellation record, swept project by project.

        There is no bulk cancellation feed and no published total, so this is
        the one ledger with nothing to reconcile against. What it *can* be
        checked against is `dropouts` on the issuance blocks, and the two
        disagree — see `iter_credit_totals`.

        Every project is asked, not only those whose blocks show a dropout.
        Narrowing on our own reading of another feed is how a registry's own
        records go missing, and the sweep is 105 requests against a registry
        that costs 14 for everything else.
        """
        if self._cancellation_rows is not None:
            return self._cancellation_rows

        rows: list[dict[str, Any]] = []
        for record in self._fetch(PROJECTS):
            initiative_id = db.int_or_none(record.get("id"))
            if initiative_id is None:
                continue
            body = self._get(f"carbon-credits/project/{initiative_id}/cancellations")
            for row in (body or {}).get("data") or []:
                rows.append({**row, "initiative_id": initiative_id})
        self._cancellation_rows = rows
        return rows

    # -- counts ------------------------------------------------------------

    def project_total(self) -> int:
        return self._total(PROJECTS)

    def count(self, resource: str) -> int:
        if resource == CANCELLATIONS:
            # No published total. The sweep's own row count is all there is,
            # which makes reconciliation vacuous — stated here rather than
            # dressed up as a check that proves something.
            return len(self._cancellations())
        try:
            return self._total(LEDGER_RESOURCES[resource])
        except KeyError:
            raise ValueError(
                f"BioCarbon has no ledger named {resource!r}"
            ) from None

    def detail_url(self, project_id: int) -> str:
        return settings.BIOCARBON_PROJECT_DETAIL_URL.format(project_id=project_id)

    # -- iteration ---------------------------------------------------------

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        records = self._fetch(PROJECTS)
        expected = {
            key
            for key in (db.int_or_none(r.get("id")) for r in records)
            if key is not None
        }
        yielded: set[int] = set()
        seen = 0

        for record in records:
            initiative_id = db.int_or_none(record.get("id"))
            if initiative_id is None:
                log.error(
                    "A BioCarbon project has no usable id and cannot be "
                    "stored: %r",
                    record.get("project_id") or record.get("project_name"),
                )
                continue
            detail = self._detail(initiative_id)
            row = normalize_project(record, detail)
            row["detail_url"] = self.detail_url(initiative_id)
            yield row, {"list": record, "detail": detail}
            seen += 1
            yielded.add(initiative_id)
            if progress is not None:
                progress(seen)
            if max_records is not None and seen >= max_records:
                return

        # Key sets, not counts: a count cannot see one project yielded twice
        # while another is dropped.
        missing = expected - yielded
        if missing:
            log.error(
                "INCOMPLETE: %s BioCarbon project(s) never reached the "
                "database.",
                len(missing),
            )

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        if resource == CANCELLATIONS:
            records = self._cancellations()
            expected = None
        else:
            platform_resource = LEDGER_RESOURCES.get(resource)
            if platform_resource is None:
                raise ValueError(f"BioCarbon has no ledger named {resource!r}")
            records = self._fetch(platform_resource)
            expected = self._total(platform_resource)

        rows = (normalize_credit(resource, record) for record in records)
        yield from reconciled(
            rows,
            expected=expected,
            progress=progress,
            max_records=max_records,
            label=f"{REGISTRY} {resource}",
        )

    def iter_credit_totals(self, resource: str) -> Iterator[tuple[int, float, Any]]:
        """Cancelled units per project, taken from the issuance blocks.

        **Only cancellations.** Issuances and retirements both sum to exactly
        what the registry publishes for itself — 85,177,570 and 50,157,520,
        matching `impact-stats` to the unit — so stating a total for either
        would restate the rows and add nothing.

        Cancellations are the one place the feeds disagree. The per-project
        `cancellations` endpoint publishes 3 rows across 2 projects; 14
        issuance blocks across 9 projects carry a nonzero `dropouts`, and
        `amount = active + outof + dropouts` holds on every one of them, so the
        block field is the registry's own arithmetic rather than a second
        opinion. The endpoint's rows are still stored — they carry the
        cancellation dates, which the blocks do not — and this figure outranks
        them in `db.credit_totals()`.

        Not to be confused with `verified_reductions`, which the detail states
        per project and which is *not* an issued total: one project
        (`BCR-TR-152-1-001`) has verified 322,687 units and issued none, and
        the registry's own emitted-credits figure agrees with the ledger, not
        with the verified sum. See docs/field-mapping.md.
        """
        if resource != CANCELLATIONS:
            return
        totals: dict[int, tuple[float, int]] = {}
        for record in self._fetch(LEDGER_RESOURCES[ISSUANCES]):
            quantity = _number(record.get("dropouts")) or 0.0
            if quantity <= 0:
                continue
            key = db.int_or_none(record.get("initiative_id"))
            if key is None:
                continue
            running, events = totals.get(key, (0.0, 0))
            totals[key] = (running + quantity, events + 1)
        for key, (quantity, events) in sorted(totals.items()):
            yield key, quantity, events


# -- normalisation ---------------------------------------------------------
# BioCarbon's field names -> the registry-neutral db.PROJECT_FIELDS.
#
# Deliberate gaps, measured over the full 105-project index on 2026-08-04:
#   * `state_province` / `city` — no structured field exists. The only
#     location beyond the country is `localitation`, a free-text sentence
#     ("The project is located in the villages of El Tigre and Alto
#     Tillavá, municipality of Puerto Gaitán…"), which goes to `extra`.
#     Reading a department out of that would be inventing one.
#   * `region_name` — only Verra publishes one.
#   * `avg_annual_vol_vcu` — no yearly estimate is published. The total is,
#     and it is NOT divided by the duration to manufacture one.
#   * `additional_certification` — no equivalent field.
#   * `afolu_names` — no sub-type code. `type_project_name` is the nearest
#     thing and it is a display name, not a vocabulary; it goes to `extra` and
#     Tipo Micro is keyed on the methodology string, exactly as Cercarbono's
#     is.
#   * `project_size`, `units_issued` — not published.


def normalize_project(
    record: dict[str, Any], detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    detail = detail or {}
    initiative_id = db.int_or_none(record.get("id"))

    row: dict[str, Any] = {
        "project_id": initiative_id,
        # `BCR-CO-319-14-002`. Unique across all 105 and the reference the
        # registry's own serials are built from — unlike SocialCarbon's, this
        # published reference really is one per project. It is still not the
        # primary key: the numeric id is what every ledger and the public URL
        # use.
        "external_id": _stated(record.get("project_id")),
        "project_name": _stated(record.get("project_name")),
        # `applicable_standard` reads "BioCarbon Standard" on all 105. The
        # constant is a fallback for a project whose detail did not load, not
        # an assertion over the registry's own field.
        "standard_name": _stated(detail.get("applicable_standard")) or STANDARD_NAME,
        # English throughout and from a closed lifecycle vocabulary:
        # Registered, Listed, Declined, De-registered, Withdrawn,
        # "Registration Request Under Review". Every one is stored — Verra's
        # rows carry Withdrawn and Rejected too, and the column is what says
        # which.
        "status": _stated(record.get("status")),
        # Tipo Macro: this registry's own sector vocabulary, untranslated like
        # every other registry's. Four values —
        # "Agriculture, forestry and other land uses (AFOLU)",
        # "Energy industries (renewable sources / energy efficiency)",
        # "Waste handling and disposal", "Transport".
        "sectoral_scope": _stated(record.get("sector_name")),
        "methodologies": _methodologies(record),
        # The list's `holder_name` is the participant list, not the account
        # holder: `holder.holder` is the account. 105/105, and it matches the
        # detail's `participants` field.
        "proponents": _stated(record.get("holder_name")),
        # The registry's own spelling, which mixes languages — "Colombia" and
        # "Nigeria" beside "Malasia", "Perú" and "Estados Unidos". Carried
        # through rather than normalised; Continent is derived from
        # `country_code`, which is ISO and complete.
        "country_name": _stated(record.get("country")),
        "country_code": _stated(record.get("country_iso")),
        "credit_period_start": _stated(record.get("quantification_period_start")),
        "credit_period_end": _stated(record.get("quantification_period_end")),
        # "The result is :total_reductions_general tCO2e during the project's
        # quantification period (:duration years)" — the registry's own
        # certificate wording, so this is the estimate over the whole period.
        # 41 of 105. Its sibling `total_reductions` is NOT the yearly figure:
        # the same template calls it "verified during this monitoring period",
        # so it goes to `extra` rather than into an ex-ante column.
        "exante_quantity": _number(detail.get("total_reductions_general")),
    }

    extra = {
        "consecutive": db.int_or_none(record.get("consecutive")),
        "project_type": _stated(record.get("type_project_name")),
        "holder": _stated((record.get("holder") or {}).get("holder")),
        "validation_body": _stated(record.get("ovv")) or _stated(detail.get("ovv")),
        "duration_years": db.int_or_none(record.get("duration")),
        "acceptance_date": _stated(record.get("acceptance_date")),
        # Verified, which is not issued: one project has verified units and no
        # issuance blocks at all. Kept because it is the registry's own figure
        # and the discrepancy is worth being able to query.
        "verified_reductions": _number(record.get("verified_reductions"))
        or _number(detail.get("verified_reductions")),
        "monitoring_period_reductions": _number(detail.get("total_reductions")),
        "location_text": _stated(detail.get("localitation")),
        "latitude": _number(detail.get("latitude")),
        "longitude": _number(detail.get("longitude")),
        "description": _stated(detail.get("description")),
        # The registry's own migration provenance: 4 projects say they came
        # from another standard, 2 of them naming the CDM. This is what a
        # double-count check reads first.
        "migrated_in": _stated(detail.get("confirm_migration")),
        "migrated_from": _stated(detail.get("migration_standard_name")),
        "unified": _stated(detail.get("unification")),
        "also_registered_as": ALSO_REGISTERED_AS.get(initiative_id),
    }
    row["extra"] = {k: v for k, v in extra.items() if v not in (None, "", [], {})} or None
    return row


def _methodologies(record: dict[str, Any]) -> str | None:
    """The methodology names, joined — code included, because they carry it.

    `BCR0002_Cuantificación de la reducción de emisiones de GEI. Proyectos
    REDD+` and `CDM - AR-ACM0003_CDM Afforestation and reforestation…`. The
    name states the code, so joining names alone loses nothing and gives the
    Tipo Micro rules both the code and the activity to match on — which is
    what lets the existing CDM rules fire on this registry unchanged.

    25 of 105 projects list more than one.
    """
    return joined(
        (item.get("name") or item.get("code") for item in record.get("methodologies") or ()),
        NOT_STATED,
    )


#: Per ledger: how one row states its quantity, its class and its date. The
#: three resources spell the same ideas differently — an issuance block's
#: `amount` against a retirement's `sold` — which is naming, not meaning.
CREDIT_FIELDS = {
    ISSUANCES: ("amount", "destination", "date_issue"),
    RETIREMENTS: ("sold", "destination", "created_at"),
    CANCELLATIONS: ("amount", None, "cancellation_date"),
}


def normalize_credit(resource: str, record: dict[str, Any]) -> dict[str, Any]:
    """One ledger row.

    `entity_id` is hashed from the serial, which is unique across every
    resource here — 626 of 626 issuance serials and 11,439 of 11,439
    retirement serials — and is the only id the bulk retirement feed carries at
    all. The resource is part of the seed so an issuance and a retirement of
    the same units can never collide.
    """
    quantity_field, unit_field, date_field = CREDIT_FIELDS[resource]
    serial = _stated(record.get("serial"))
    return {
        "entity_id": hashed_id(REGISTRY, resource, serial),
        "project_id": db.int_or_none(record.get("initiative_id")),
        "quantity": _number(record.get(quantity_field)),
        # The issuance blocks state a `year`; the retirements state a vintage
        # range in the serial and nothing structured, so theirs stays blank
        # rather than being parsed out of a string.
        "vintage": _stated(record.get("year")),
        # What the units are earmarked for, in the registry's own words and
        # its own two languages: issuances read `Impuesto`, `Reserva`,
        # `reserved` or `Voluntario`, retirements read `tax`, `backup` or
        # `voluntary`. `Reserva`/`reserved`/`backup` are buffer units, and they
        # are counted in the issued total because the registry's own published
        # figure counts them — the class is stored so a split is a
        # config/credits.yaml change and not a re-scrape.
        "unit_type": _stated(record.get(unit_field)) if unit_field else None,
        "status": _credit_status(resource, record),
        "event_date": _stated(record.get(date_field)),
        # **`final_user`, not `to_name`.** `to_name` is filled on all 11,439
        # rows and names the account the units were retired *to* — very often
        # a fuel distributor retiring on behalf of its customers, so every
        # retirement would look like a third-party sale the moment
        # `sold_equals_retired` is flipped. `final_user` is the end
        # beneficiary, 9,174 of 11,439. Same call as SocialCarbon's
        # `Beneficiary` over `Retiree`.
        "beneficiary": _stated(record.get("final_user")),
        "reason": _stated(record.get("reason")),
        "additional_certification": None,
        "serial_no": serial,
    }


def _credit_status(resource: str, record: dict[str, Any]) -> str | None:
    """What the registry says about this row's own state.

    Only retirements have one, and it is `data_visibility`: the registry marks
    7,033 of 11,439 retirements `private` and 4,406 `public`. Its public API
    returns `final_user` on both, so the flag is stored beside the name rather
    than used to drop it — which keeps the decision to honour it a query, not
    a re-scrape.

    Deliberately not a value `db.credit_totals()` buckets on: it only does that
    for the resource named `credits`.
    """
    if resource != RETIREMENTS:
        return None
    return _stated(record.get("data_visibility"))
