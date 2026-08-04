"""The legacy Markit Environmental Registry public view — the third platform.

`mer.markit.com/br-reg/public` is the registry S&P inherited with IHS Markit,
and it is still live and still serving data that exists nowhere else. It hosts
**21 programmes** behind one `standardId` parameter — Plan Vivo, Social Carbon,
ACRE, Climate Community and Biodiversity, Peru REDD+, Pacific Carbon Standard,
W+ and others — so this module is written the way `platts/api.py` is: the
platform lives here, and a registry on it is a subclass setting a handful of
class attributes.

Measured against the live service on 2026-07-29; see docs/api-contract-markit.md.

    GET /br-reg/public/index.jsp?entity=<entity>&standardId=<id>&start=<n>
    GET /br-reg/public/project.jsp?project_id=<id>

Everything the platform gets wrong, and how each is handled:

* **It is HTML.** There is no JSON API behind this view; the page is the
  contract. Parsed with the standard library — see `tables.py` — and read by
  column heading rather than position.
* **`limit` is silently ignored.** The form posts one, the server pins 15 rows
  a page and answers 200 whatever you ask for. Third registry in this codebase
  to accept a parameter it does not apply, after Verra's filters and Gold
  Standard's `project_id`. There is no page size to tune; do not add one.
* **No total is published anywhere, and the pager lies.** `<li id="page_no">`
  renders empty, no header carries a count, and the "Next →" link is *never*
  disabled: `start=240` on a 35-row feed answers 200 with an empty table and
  still offers a next page. Trusting the pager costs 2,000 requests and
  finishes clean. Asking past the end is not free either — the 5,034-row
  retirement feed answers a past-the-end `start` with **HTTP 500**, not an
  empty page. Paging therefore ends on **a short page**: fewer than
  `PAGE_SIZE` records means the feed is exhausted, and stopping there means
  never asking for the page that 500s. See `_pages`.
* **Some rows are not records.** A merged project emits a second `<tr>` whose
  fourth cell carries a malformed `style` attribute that has swallowed a whole
  data payload (`style="34.914551Sofala…Active…"`). Parsed positionally it
  looks like a real row with plausible values in the wrong columns. A record
  is recognised structurally instead: it has a "View" link **in its own
  cells**, not one inherited through a `rowspan`.
* **A project id is not unique.** Sofala renders twice, and Scolel té's two
  rows are two sub-projects sharing one `master-project.jsp` id. `project_id`
  is half the primary key, so rows sharing one are merged rather than allowed
  to overwrite each other — the distinct project types are joined, and the
  merge is recorded in `extra`.
* **The `standardId` filter is not trusted.** Asking for Social Carbon returns
  retirement rows whose Standard column reads "No Established Standard" —
  because Social Carbon is an *additional certification* there, not a primary
  standard. Whatever the parameter means, it is not "only this standard's
  rows", so rows are filtered again on the Standard column this side. Same
  defence as Cercarbono's CO2 filter, for the same reason: this platform's
  ancestors are where "a filter that appears applied usually is not" was first
  learned.
* **The cancellation entity is spelled `Cancelled`**, capitalised, where every
  other entity is lower-case. It is the app's own spelling.

The ledgers are named as Verra's are — `issuances`, `holdings`, `retirements`,
`cancellations` — deliberately and not descriptively: `db.credit_totals()`
branches on the exact string `credits` to select Gold Standard's
bucket-by-status semantics, and these records want the one-ledger-per-resource
path that any other name selects.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any

from ... import db, settings
from ...http_client import RegistryClient, RetryableStatus
from ..base import ClientOwner, reconciled
from ..text import hashed_id, stated
from . import tables

log = logging.getLogger(__name__)

BASE = "https://mer.markit.com/br-reg/public"
INDEX = f"{BASE}/index.jsp"
DETAIL = f"{BASE}/project.jsp"
#: Grouped projects have their own detail page, and their rows carry the
#: master's id. Sending a master id to `project.jsp` asks for a project that
#: does not exist under that key.
MASTER_DETAIL = f"{BASE}/master-project.jsp"

#: The server's own page size. Not a knob: `limit` is accepted and ignored.
PAGE_SIZE = 15

#: A page count this side of which something is wrong with the loop rather
#: than with the registry. 21 programmes share this view and the largest is
#: nowhere near this; hitting it means paging is not terminating.
MAX_PAGES = 2_000

PROJECTS = "project"
ISSUANCES = "issuances"
HOLDINGS = "holdings"
RETIREMENTS = "retirements"
CANCELLATIONS = "cancellations"

#: our ledger name -> (the view's entity name, its sort column)
#: `Cancelled` is capitalised in the app and lower-cased nowhere.
ENTITIES: dict[str, tuple[str, str]] = {
    ISSUANCES: ("issuance", "account_name"),
    HOLDINGS: ("holding", "account_name"),
    RETIREMENTS: ("retirement", "account_name"),
    CANCELLATIONS: ("Cancelled", "account_name"),
}

#: The ISO 3166 names that contain a comma of their own. The Country column is
#: a single "<country>, <state>" cell, so the state is what follows the last
#: comma — except for these, where the comma is part of the country and there
#: is no state at all. Without the guard "Korea, Republic of" becomes the
#: country "Korea" in the state "Republic of": a plausible-looking row stating
#: a place the registry never published.
COMMA_COUNTRY_NAMES = (
    "Bolivia, Plurinational State of",
    "Bonaire, Sint Eustatius and Saba",
    "Congo, the Democratic Republic of the",
    "Iran, Islamic Republic of",
    "Korea, Democratic People's Republic of",
    "Korea, Republic of",
    "Macedonia, the former Yugoslav Republic of",
    "Micronesia, Federated States of",
    "Moldova, Republic of",
    "Palestine, State of",
    "Saint Helena, Ascension and Tristan da Cunha",
    "Taiwan, Province of China",
    "Tanzania, United Republic of",
    "Venezuela, Bolivarian Republic of",
    "Virgin Islands, British",
    "Virgin Islands, U.S.",
)

#: Values the view renders to mean "nothing here", which are not values.
NOT_STATED = {"", "-", "--", "n/a", "na", "none", "not applicable"}


def _stated(value: Any) -> str | None:
    """Registry text, or None where the registry stated nothing."""
    return stated(value, NOT_STATED)


def _number(value: Any) -> float | None:
    """A quantity as rendered: `2,043`, `1 234`, `12.5`."""
    text = _stated(value)
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("\xa0", "").replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def split_country(value: Any) -> tuple[str | None, str | None]:
    """`"Bolivia, Plurinational State of, Cochabamba"` -> country, state."""
    text = _stated(value)
    if text is None:
        return None, None
    for name in COMMA_COUNTRY_NAMES:
        if text == name:
            return name, None
        if text.startswith(f"{name},"):
            return name, _stated(text[len(name) + 1 :])
    head, comma, tail = text.rpartition(",")
    if not comma:
        return text, None
    return _stated(head), _stated(tail)


def is_record(row: tables.Row) -> bool:
    """Whether a parsed `<tr>` is a record at all.

    Two kinds of row are not. A `rowspan` continuation inherits the cells of
    the row above it, including its "View" link. And this view emits rows whose
    `style` attribute has swallowed a data payload, which parse into plausible
    values in the wrong columns and carry no link of their own.

    A record links to its own detail page **from its own cells**. That is the
    structural difference, and it is what page-size arithmetic has to count:
    a full page is `PAGE_SIZE` records, not `PAGE_SIZE` `<tr>`s.
    """
    return tables.link_id(row, parameter="project_id", own_only=True) is not None


#: A stable numeric key for a ledger row, which has no id of its own. The
#: registry name is part of the hash, so a Markit key cannot collide with the
#: platform-issued `entityId` of the same registry's rows on S&P — Plan Vivo's
#: ledgers hold both. See `registries.text.hashed_id`.
event_id = hashed_id


class MarkitPublicAPI(ClientOwner):
    """Reads one programme off the legacy Markit public view.

    Implements registries.base.RegistryAdapter. Subclasses set the class
    attributes below and nothing else; everything measured about the platform
    is shared, so a fix here fixes every registry on it.
    """

    #: Value stored in our `registry` column (settings.PLAN_VIVO, ...).
    registry: str = ""
    #: Names this programme's fixture files. Read by no code — `discover` is
    #: S&P-only and refuses this platform — so it documents the convention a
    #: human follows when capturing one, and keeps two programmes' fixtures
    #: from colliding.
    slug: str = ""
    #: The `standardId` query parameter. Read from the view's own programme
    #: dropdown, never invented.
    standard_id: str = ""
    #: The wording the Standard column carries for this programme. Rows
    #: claiming anything else are dropped — the parameter alone is not trusted.
    standard_names: tuple[str, ...] = ()
    #: What `standard_name` is stored as. The Standard column says "Plan Vivo"
    #: for both V4 and V5 eras; the stored value is what tells a row's system
    #: apart once two of them share one registry.
    standard_label: str = ""
    #: Shown in progress messages, so a two-system registry says which half is
    #: running.
    system_label: str = ""

    ledgers: tuple[str, ...] = (ISSUANCES, HOLDINGS, RETIREMENTS, CANCELLATIONS)

    #: The per-project page is the only source of Additional Certification.
    #: One request per project, so it is a class switch rather than a given.
    fetch_detail: bool = True

    detail_url_template: str = f"{DETAIL}?project_id={{project_id}}"
    master_url_template: str = f"{MASTER_DETAIL}?project_id={{project_id}}"

    def __init__(
        self,
        client: RegistryClient | None = None,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self._bind_client(client, cancel)
        self._rows: dict[str, list[tables.Row]] = {}

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """What the site's own visitor sends.

        A browser `User-Agent` is not optional on this host either — the same
        lesson Gold Standard's Cloudflare 403 taught. `Origin`/`Referer` are
        this registry's own, because a shared one is what Cercarbono answered
        with a generic 500.
        """
        headers = dict(settings.BROWSER_HEADERS)
        headers.update(
            {
                "Accept": "text/html,application/xhtml+xml",
                "Origin": "https://mer.markit.com",
                "Referer": f"{INDEX}",
            }
        )
        return headers

    def _params(self, entity: str, sort: str, start: int) -> dict[str, str]:
        """Every parameter the view's own form submits, in its own spelling.

        Sent whole and empty-valued rather than omitted, because that is what
        the page does and this platform has already been caught treating an
        unrecognised request as an unfiltered one.
        """
        return {
            "entity": entity,
            "srd": "false",
            "sort": sort,
            "dir": "ASC",
            "start": str(start),
            "entity_domain": "Markit",
            "standardId": self.standard_id,
            "acronym": "",
            "additionalCertificationId": "",
            "categoryId": "",
            "unitClass": "",
            "name": "",
        }

    def _pages(self, entity: str, sort: str) -> Iterator[tables.Table]:
        """Walk the feed until a page comes back short.

        Three end-of-list signals were measured, and only one of them is safe
        to use:

        * **The pager is useless.** "Next →" is rendered enabled forever,
          including 200 pages past the end of a 35-row feed.
        * **Asking past the end is not free.** A 35-row feed answers `start=45`
          with an empty table and HTTP 200, but the 5,034-row retirement feed
          answers `start=5040` with **HTTP 500** — retried, backed off and
          raised, which is a failed sync over a page that does not exist.
        * **A short page is the end.** A full page holds exactly `PAGE_SIZE`
          records, so fewer than that means the feed is exhausted, and stopping
          there means never asking for the page that 500s.

        The 500 is still handled, because a feed whose length is an exact
        multiple of `PAGE_SIZE` reaches the end without ever seeing a short
        page. It is only accepted as the end after at least one page has been
        read — a 500 on the first request is a broken registry, not an empty
        one, and must not be mistaken for a registry with no records.
        """
        start = 0
        for page in range(MAX_PAGES):
            try:
                html = self.client.get_text(
                    INDEX,
                    params=self._params(entity, sort, start),
                    headers=self._headers(),
                )
            except RetryableStatus:
                if page == 0:
                    raise
                log.info(
                    "%s %s: HTTP 500 at start=%s after %s full page(s) — this "
                    "view answers a request past the end of a feed with a 500 "
                    "rather than an empty page. Treated as the end.",
                    self.registry,
                    entity,
                    start,
                    page,
                )
                return
            table = tables.results_table(html)
            if not table.rows:
                return
            yield table
            if sum(1 for row in table.rows if is_record(row)) < PAGE_SIZE:
                return
            start += PAGE_SIZE
        raise RuntimeError(
            f"{self.registry}: {entity} paging did not terminate within "
            f"{MAX_PAGES} pages, which is far past any programme on this "
            f"platform. Something is wrong with the loop, not the registry."
        )

    def _standard_of(self, row: tables.Row) -> str | None:
        return row.get("Standard Name", "Standard")

    def _on_standard(self, row: tables.Row) -> bool:
        """Whether a row really belongs to this programme.

        `standardId` is a request, not a guarantee: asking for Social Carbon
        returns rows whose Standard column reads "No Established Standard".
        A row that does not name this programme is not this programme's.
        """
        if not self.standard_names:
            return True
        stated = self._standard_of(row)
        return bool(stated) and stated in self.standard_names

    def rows(self, key: str) -> list[tables.Row]:
        """Every record row of one entity, paged, filtered and de-duplicated.

        Cached on the instance because there is no published total: `count()`
        has to read the feed to answer, and the scrape then reads the same
        feed. Reading it once is both fewer requests and the only way the two
        numbers cannot disagree.
        """
        if key in self._rows:
            return self._rows[key]

        entity, sort = (PROJECTS, "project_name") if key == PROJECTS else ENTITIES[key]
        collected: list[tables.Row] = []
        seen: set[tuple[Any, ...]] = set()
        not_records = 0
        off_standard = 0
        repeats = 0
        #: What the rejected rows actually said, so the error below can name it.
        stated_names: set[str] = set()

        for page, table in enumerate(self._pages(entity, sort)):
            for row in table.rows:
                if not is_record(row):
                    not_records += 1
                    continue
                if not self._on_standard(row):
                    off_standard += 1
                    stated_names.add(self._standard_of(row) or "")
                    continue
                fingerprint = tuple(sorted(row.values.items()))
                if fingerprint in seen:
                    # Two rows identical in every published column. Kept for
                    # ledgers, where two identical events are two events, and
                    # collapsed for projects, where one id is one project.
                    repeats += 1
                    if key == PROJECTS:
                        continue
                seen.add(fingerprint)
                collected.append(row)
            log.debug("%s %s page %s: %s rows", self.registry, key, page, len(table.rows))

        if not_records:
            log.debug(
                "%s %s: skipped %s non-record row(s) — merged continuations and "
                "the view's malformed style-attribute rows.",
                self.registry,
                key,
                not_records,
            )
        if off_standard and not collected:
            # Every single row rejected is not a feed of another programme's
            # data — it is the Standard column having moved or been renamed,
            # so `_standard_of` returns None for everything. That empties the
            # ledger, and because `count()` reads this same list it empties
            # the expected total too: the sync then reports "0 (registry
            # reports 0)" and reconciles perfectly against nothing.
            log.error(
                "%s %s: every one of %s row(s) was rejected as belonging to "
                "another standard. Expected one of %s; the feed states %s. "
                "That is the Standard column missing or renamed, not an empty "
                "registry.",
                self.registry,
                key,
                off_standard,
                ", ".join(self.standard_names) or "(none)",
                ", ".join(sorted(stated_names)) or "(nothing)",
            )
        elif off_standard:
            log.info(
                "%s %s: dropped %s row(s) belonging to another standard — the "
                "standardId filter does not narrow on its own.",
                self.registry,
                key,
                off_standard,
            )
        if repeats:
            log.info(
                "%s %s: %s repeated row(s) in the feed.", self.registry, key, repeats
            )
        self._rows[key] = collected
        return collected

    def project_rows(self) -> dict[int, list[tables.Row]]:
        """Project rows grouped by the id they will be stored under.

        `project_id` is half the primary key of `projects`, and this view
        publishes it more than once: a grouped project's sub-projects all link
        to one `master-project.jsp` id, and some projects simply render twice.
        Left alone the upsert would keep whichever row happened to be last,
        picking silently between "Olympic Forest (Mali)" and "Olympic Forest
        (Senegal)". Grouping makes the choice explicit and recorded.
        """
        grouped: dict[int, list[tables.Row]] = {}
        for row in self.rows(PROJECTS):
            project_id = tables.link_id(row, parameter="project_id", own_only=True)
            if project_id is None:  # pragma: no cover - filtered in rows()
                continue
            grouped.setdefault(project_id, []).append(row)
        return grouped

    # -- the adapter contract ----------------------------------------------

    def project_total(self) -> int:
        """How many projects this programme publishes.

        Counted from the feed, because the view states no total of its own —
        so this is emphatically **not** an independent check the way Gold
        Standard's `X-Total-Count` is. What guards a read here is `rows()`:
        an empty page ends it, a row without its own link is not a record, and
        every row must name this programme.

        Distinct ids, not rows: 35 rows are 30 projects here.
        """
        return len(self.project_rows())

    def count(self, resource: str) -> int:
        return len(self.rows(resource))

    def detail_url(self, project_id: int, *, master: bool = False) -> str:
        template = self.master_url_template if master else self.detail_url_template
        return template.format(project_id=project_id)

    def _projects(self) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        for project_id, rows in self.project_rows().items():
            raw: dict[str, Any] = {
                "rows": [dict(row.values) for row in rows],
                "master": any(
                    "master-project.jsp" in target
                    for row in rows
                    for target in tables.link_targets(row, own_only=True)
                ),
            }
            if self.fetch_detail:
                raw["detail"] = self.project_detail(
                    project_id, master=bool(raw["master"])
                )
            yield self.normalize_project(raw, project_id), raw

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        # `expected` is this adapter's own count of the same list, so it can
        # only catch a normalisation drop — the view publishes no total to
        # reconcile against. What `rows()` guards is the read itself.
        yield from reconciled(
            self._projects(),
            expected=self.project_total(),
            progress=progress,
            max_records=max_records,
            label=f"{self.registry} projects",
        )

    def project_detail(self, project_id: int, *, master: bool = False) -> dict[str, str]:
        """The per-project page, which is the only source of some fields."""
        url = MASTER_DETAIL if master else DETAIL
        html = self.client.get_text(
            url, params={"project_id": str(project_id)}, headers=self._headers()
        )
        return tables.cell_dict(tables.results_table(html))

    def normalize_project(
        self, raw: dict[str, Any], project_id: int
    ) -> dict[str, Any]:
        rows = [r for r in raw.get("rows") or [] if isinstance(r, dict)]
        detail = raw.get("detail") or {}

        def first(*headings: str) -> str | None:
            """The first value any of this project's rows states."""
            for row in rows:
                for heading in headings:
                    value = _stated(row.get(heading))
                    if value:
                        return value
            return None

        def joined(heading: str) -> str | None:
            """Every distinct value across the rows, in the order published.

            Scolel té's two sub-project rows carry different project types —
            "Forest Conservation & Avoided Deforestation" and "Afforestation /
            Reforestation" — and keeping only one would drop a vocabulary the
            derivation rules key on.
            """
            seen: list[str] = []
            for row in rows:
                value = _stated(row.get(heading))
                if value and value not in seen:
                    seen.append(value)
            return "; ".join(seen) or None

        country, state = split_country(first("Country"))

        record: dict[str, Any] = {
            "project_id": project_id,
            # No human-facing reference is published: the view's own links use
            # the numeric id, so that is what a reader would look up.
            "external_id": None,
            "project_name": first("Name"),
            # The Standard column says only "Plan Vivo" for a V4 project, which
            # is the same thing V5's registry says. The stored label is what
            # keeps the two eras apart once they share one registry.
            "standard_name": self.standard_label or first("Standard Name"),
            "status": first("Status"),
            # Tipo Macro de Projeto, carried through untranslated as every
            # registry's own vocabulary is. This platform says "REDD" and
            # "Improved forest management" where S&P says "Agriculture Forestry
            # and Other Land Use (AFOLU)".
            "sectoral_scope": joined("Project Type"),
            "proponents": first("Developer"),
            "country_name": country,
            "state_province": state,
            "additional_certification": _stated(detail.get("Additional Certification")),
            "detail_url": self.detail_url(project_id, master=bool(raw.get("master"))),
            "extra": {
                key: value
                for key, value in {
                    "category": first("Category"),
                    "validator": first("Validator"),
                    # Pending Issuance Units are a listing, not issued credits.
                    # Kept, and deliberately not written to `units_issued`.
                    "piusListed": first("PIUs Listed"),
                    "linked": _stated(detail.get("Linked")),
                    "system": self.system_label or None,
                    # Stated when this id covers more than one published row,
                    # so a merge is visible rather than inferred from a gap.
                    "mergedRows": len(rows) if len(rows) > 1 else None,
                    "names": (
                        [n for n in dict.fromkeys(
                            _stated(r.get("Name")) or "" for r in rows
                        ) if n]
                        if len(rows) > 1
                        else None
                    ),
                    # A grouped project: the id is the master's, shared by
                    # every sub-project under it.
                    "masterProject": True if raw.get("master") else None,
                }.items()
                if value
            },
        }
        return {k: v for k, v in record.items() if v is not None}

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        # Two rows of a ledger can be identical in every published column, so
        # the hash of a row's values is not unique by itself. The occurrence
        # index makes the key unique and keeps it stable across runs, which is
        # what the upsert needs.
        occurrences: dict[str, int] = {}
        rows = self.rows(resource)
        yield from reconciled(
            (self.normalize_credit(row, resource, occurrences) for row in rows),
            expected=len(rows),
            progress=progress,
            max_records=max_records,
            label=f"{self.registry} {resource}",
        )

    def normalize_credit(
        self, row: tables.Row, resource: str, occurrences: dict[str, int]
    ) -> dict[str, Any]:
        # Always a record. A row that is not one was already rejected
        # structurally by `is_record`, so there is nothing left to return None
        # for — the `| None` this used to declare made `iter_credits` carry a
        # check that could not fire.
        project_id = tables.link_id(row, parameter="project_id")
        quantity = _number(row.get("Retirement Quantity", "Units", "Quantity", "Volume"))
        seed = "|".join(
            [
                self.registry,
                resource,
                str(project_id),
                str(row.get("Vintage")),
                str(quantity),
                str(row.get("Retirement Date", "Date")),
                str(row.get("Account")),
                str(row.get("Beneficial Owner")),
            ]
        )
        occurrences[seed] = occurrences.get(seed, 0) + 1
        return {
            "entity_id": event_id(seed, occurrences[seed]),
            "project_id": project_id,
            "quantity": quantity,
            "vintage": _stated(row.get("Vintage")),
            # "PVC (tCO2e)" — the unit, and the only place a reserve or forward
            # class would show, so it is recorded rather than summarised.
            "unit_type": _stated(row.get("Measurement", "Type")),
            "status": None,
            "event_date": _stated(row.get("Retirement Date", "Date")),
            # The named third party a retirement was made for. Left empty when
            # the registry names none — the Account is the holder, not the
            # beneficiary, and copying it across would invent one.
            "beneficiary": _stated(row.get("Beneficial Owner")),
            "reason": _stated(row.get("Reason")),
            "additional_certification": None,
            "serial_no": None,
        }


__all__ = [
    "CANCELLATIONS",
    "ENTITIES",
    "HOLDINGS",
    "ISSUANCES",
    "MarkitPublicAPI",
    "PAGE_SIZE",
    "PROJECTS",
    "RETIREMENTS",
    "event_id",
    "split_country",
]
