"""Client for the Climate Action Reserve, which publishes through Xpansiv/APX.

Contract measured against the live service on 2026-08-05; see
docs/api-contract-car.md. The platform's own traps — the `c16e` CSRF token
that is not in the form, the session cookie the GET mints, the CSV that is not
correctly quoted, the pager that clamps past the end instead of ending — live
in `registries/apx/api.py`. This module is CAR's identity and the fields CAR
actually populates.

    GET  {site}/myModule/rpt/myrpt.asp?r=<id>        the report, and its total
    POST {site}/myModule/include/rptdownload.asp     the whole report as CSV
    GET  {site}/mymodule/reg/prjView.asp?id1=<n>     one project's detail

**There is a bulk CSV export and it is unauthenticated**, so the ledger half of
a sync is 8 requests rather than ~400 pages: 1,277 projects, 5,170 issuance
rows, 11,044 retirements and 2,277 cancellations, each in one POST. The
~1,277 remaining requests are per-project detail pages, which are the only
place a crediting period appears.

Six things measured here that are CAR's rather than the platform's:

* **The crediting period is on the detail page and nowhere else.** The
  issuance report has a column literally called `Crediting Period` and it is
  **not a date** — it is a label (`Initial`, `Renewed-Second`,
  `Renewed-Third`) and blank on 4,848 of 5,170 rows. Reading it into
  `credit_period_start` would fill a date column with the word "Initial", and
  nothing would raise. The column name is the trap.
* **`Protocol and Version` is published on issuance rows, not project rows.**
  It is the registry's methodology, 5,170/5,170, so `Metodologia` is built
  from the issuance report and joined per project. A project with no
  issuances — 376 of 1,277 — gets a **blank**, because the registry publishes
  no methodology for it anywhere. Nothing is inferred from the project type,
  even though the two nearly agree (see below).
* **The protocol name restates the project type and adds an edition.**
  "Forestry - MX - Version 2.0", "Improved Forest Management - Version 3.1",
  "Mine Methane Capture - ARB Compliance - ARB Compliance Offset Protocol Mine
  Methane Capture, April 25, 2014". So unlike ACR — where one `projectType`
  covered 352 forest projects and only the protocol told them apart — CAR's
  own `Project Type` is already the fine-grained vocabulary, 32 values deep.
  The derivation rules therefore key on the **type**, not the protocol, which
  is what keeps the 376 projects with no issuance classified.
* **No country name is published anywhere**, only an ISO code — `US` (774),
  `MX` (491), `CA` (7), `CN` (3), `AR` (2). ACR's gap exactly, in a second
  registry. `country_code` is 1,277/1,277 so Continent derives cleanly (the
  Gold Standard path) and `País` stays blank rather than being invented; the
  user's answer on ACR was blank and this is the same question.
* **`Compliance Program ID` says `N/A` where there is none.** It is populated
  on all 1,277 rows and 'not applicable' is one of its values, so a naive
  `stated()` keeps the literal string. It matters because it is the first
  thing a cross-registry check reads: 2,035 of 2,277 cancellations here are
  conversions out to California's ARB or Washington's Ecology registry, and
  this id is what the units are called once they get there.
* **`Account Holder` is not a beneficiary, and there is no beneficiary field
  at all.** It is on 11,044 of 11,044 retirements and names whoever held the
  credits — 5,894 rows say `On Behalf of Third Party` and name that third
  party only in the free-text `Retirement Reason Details`. Filling
  `beneficiary` from it would make **every** retirement read as a third-party
  sale the moment `config/credits.yaml`'s `sold_equals_retired` is flipped,
  which is the call ACR and BioCarbon both made against a *structured*
  alternative this registry does not have. So `beneficiary` stays null and the
  prose lands in `reason`, which is the only copy — `credit_events` keeps no
  raw payload. Parsing the prose is a business decision, not the adapter's.

And one that is not a trap: the issuance rows summed per project agree with
the project list's own `Total Number of Offset Credits Registered` on **1,277
of 1,277 projects, to the unit** — 268,262,717 both ways. So there is no
`iter_credit_totals` here, the same conclusion ACR reached and for the same
measured reason: nothing is stated twice with two different answers. The
registry's own figure is kept in `extra.stated_registered_credits` anyway, so
the day the two disagree is answerable from the database rather than by
re-scraping.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ... import db, settings
from ..apx.api import APXReportAPI, numeric_id
from ..text import hashed_id, joined, stated

log = logging.getLogger(__name__)

REGISTRY = settings.CAR

ISSUANCES = "issuances"
RETIREMENTS = "retirements"
CANCELLATIONS = "cancellations"

#: **Never `credits`.** `db.credit_totals()` branches on that exact string to
#: select Gold Standard's bucket-by-status semantics, and each of these
#: resources is already one bucket. Chosen deliberately, not descriptively.
LEDGERS = (ISSUANCES, RETIREMENTS, CANCELLATIONS)

#: Values this registry uses to mean "nothing here". `N/A` is its own word for
#: it on `Compliance Program ID` (all 1,277 rows are populated and that is what
#: 'none' looks like) and on the two CORSIA eligibility columns.
NOT_STATED = {"n/a", "na", "none", "null", "-", "--", "not applicable"}

#: The same table with the one entry that is also a real ISO country code
#: removed. **`NA` is Namibia.** This registry states a country as a code and
#: never as a name, so running the placeholder table over `country_code` would
#: delete a Namibian project's only country field and take its Continent with
#: it. CAR publishes no `NA` today — its five codes are US, MX, CA, CN, AR —
#: which is exactly why this guard is written now rather than after the first
#: African project lands. Puro met the same trap; ACR carries the same pair.
NOT_STATED_CODE = NOT_STATED - {"na"}

#: CAR reference -> the Verra project publishing the same units.
#:
#: **The registry states this itself**, in `Offset Credits Converted to VCUs`
#: on the issuance report — populated on **2 of 5,173 rows** and naming no
#: registry, exactly as ACR's `hasAnotherCarbonProgram` names no programme. The
#: two rows were checked against the database and both are there: `CAR400`
#: (South Jordan landfill, Utah, 45,730 converted) is Verra 1528 and `CAR498`
#: (Wolf Creek landfill, Georgia, 4,270) is Verra 1527, each named
#: `… - CER Conversion`, each with status *Units transferred from approved GHG
#: program*, and each issuing **one** VCU block of exactly the converted
#: quantity at vintage 2014.
#:
#: **This is the third cross-registry overlap in the database and the first
#: where the units are the same units.** Cercarbono/BioCarbon and ACR/Verra are
#: disjoint tranches of one physical project — neither row restates the other.
#: Here Verra's whole issued total for both projects *is* CAR's converted
#: figure, so summing the two registries counts 50,000 tCO2e twice. And CAR
#: does not net them out: neither project has a single cancellation row, so the
#: units stay on CAR's own book as issued.
#:
#: Nothing is subtracted. Both registries' figures are reported as published —
#: the same call ACR's conversions to ARB got — and this table is what makes
#: the overlap visible from the CAR side. Raised in `docs/field-mapping.md`,
#: where it is the business's to decide, not the adapter's.
ALSO_REGISTERED_AS = {
    "CAR400": "VERRA 1528",
    "CAR498": "VERRA 1527",
}

#: What a published reference looks like here: `CAR1957`, unique across all
#: 1,277 — the registry's own CSV export has no duplicate. This is a **check,
#: not the key**. The key comes from `apx.numeric_id`, the same function the
#: platform uses to build the detail URL, so that a row's stored id and the
#: page it links to cannot be derived two different ways and disagree. A
#: reference that is not CAR-shaped still gets a key and gets a log line: this
#: registry has never published one, and the day it does is a change to see
#: rather than a row to lose.
REFERENCE = re.compile(r"^CAR0*\d+$", re.IGNORECASE)

#: Columns whose value is a hyperlink in the grid and the bare word `View` in
#: the CSV. They carry no information in either form; the project's own detail
#: URL is derived from its reference instead. See docs/api-contract-car.md
#: trap 4.
LINK_PLACEHOLDERS = {"view"}


def _stated(value: Any) -> str | None:
    return stated(value, NOT_STATED)


def _country_code(value: Any) -> str | None:
    """An ISO country code, with `NA` left alone. See NOT_STATED_CODE."""
    return stated(value, NOT_STATED_CODE)


def _number(value: Any) -> float | None:
    """A quantity, thousands-separated or not.

    **The two transports disagree and only one of them raises.** The CSV
    exports `45802`; the HTML grid renders the same value `45,802`, and
    `float("45,802")` is a ValueError that `db.float_or_none` turns into
    `None` — a 45,802-unit retirement stored as a null quantity, with nothing
    in the log. Third registry here to publish a separated number (BioCarbon's
    `"299,564"`, Markit's ledgers), and the first where it depends on which
    route the row arrived by.
    """
    text = _stated(value)
    if text is None:
        return None
    return db.float_or_none(text.replace(",", ""))


#: `10/7/2018` — month first, and **the only registry here that does not
#: publish an ISO date**. Every other adapter stores what it was given because
#: what it was given is `2018-10-07`; storing this one as published puts a
#: string in the date columns that `db.parse_date` cannot read, and the failure
#: is completely silent: `Data de Início`, `Data de Término` **and `Duração`**
#: come out blank on all 1,277 rows while the database looks populated. That is
#: what a first full sync caught, after the whole 1,277-request detail fan-out
#: was built to fetch exactly those dates.
#:
#: It is fixed **here rather than in `db.DATE_FORMATS`** on purpose. Adding
#: `%m/%d/%Y` globally would make every registry's dates month-first by
#: assumption, and `03/04/2020` is a valid date under either reading — so the
#: first registry to publish day-first would be misread by three-hundred-odd
#: days with nothing to say so. The month-first reading is CAR's own, and it is
#: measured, not assumed: across all 2,103 published crediting-period dates the
#: first component never exceeds 12 and the second reaches 31.
US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _date(value: Any) -> str | None:
    """A published date as ISO, or the raw string if it is not one.

    A value that stops matching is **kept and logged**, never dropped: this
    column is the evidence that the registry changed its format, and a silent
    None is what the whole fix above is about.
    """
    text = _stated(value)
    if text is None:
        return None
    match = US_DATE.match(text)
    if match is None:
        log.error(
            "A CAR date reads %r, which is not `M/D/YYYY`. Storing it as "
            "published; the date columns will not parse until the format is "
            "read again.",
            text,
        )
        return text
    month, day, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _project_key(reference: Any) -> int | None:
    text = str(reference or "").strip()
    key = numeric_id(text)
    if text and not REFERENCE.match(text):
        log.error(
            "A CAR row states the reference %r, which is not `CARnnnn`. Keying "
            "it on %r; check whether the registry has changed its reference "
            "shape before trusting the row.",
            text,
            key,
        )
    return key


def _linked(value: Any) -> str | None:
    """A cell the CSV reduced to the word `View`."""
    text = _stated(value)
    if text is None or text.casefold() in LINK_PLACEHOLDERS:
        return None
    return text


def _worth_storing(value: Any) -> bool:
    """Whether an `extra` value says anything, keeping numeric zero.

    `0 == False` in Python, so a membership test that drops `False` also drops
    `0` and `0.0` — which would delete a stated registered total of zero on one
    of the 376 projects that have issued nothing, leaving it
    indistinguishable from a project the registry published no figure for.
    That distinction is the whole reason the stated totals are kept.
    """
    if value is None or isinstance(value, bool):
        return value is True
    if isinstance(value, (int, float)):
        return True
    return bool(value)


def _row(raw: dict[str, str]) -> dict[str, str]:
    """A report row, keyed on trimmed header names.

    `Total Number of Offset Credits Registered ` carries a **trailing space**
    in the header, in both the grid and the CSV. Trimming here rather than
    retyping the space into a lookup means a key that is right today cannot be
    got subtly wrong by the next person to copy it out of the contract doc.
    """
    return {str(key).strip(): value for key, value in raw.items()}


class CARAPI(APXReportAPI):
    """Talks to the public APX report module as the Climate Action Reserve.

    Implements registries.base.RegistryAdapter; the CSRF token, the session
    cookie, the CSV quoting and the paging are the platform's, in
    APXReportAPI.
    """

    registry = REGISTRY
    slug = "car"
    site = settings.CAR_SITE
    standard_name = settings.CAR_STANDARD_NAME
    ledgers = LEDGERS
    REPORTS = settings.CAR_REPORTS

    #: The detail page's own labels, verbatim. Stating them makes
    #: `apx.label_values` pair exactly rather than fall back to the `Label:`
    #: convention — this page does **not** end its labels with a colon, so the
    #: fallback would find nothing and the soft-404 guard would raise on every
    #: real project. Only the labels that are read are listed; the page
    #: publishes more.
    detail_labels = (
        "Project ID",
        "ARB ID",
        "Crediting Period",
        "Project Type",
        "Project Commencement Date",
        "Project Reporting Start Date",
        "Project Site Location",
        "State/Province/Department",
        "Country",
        "Project Status",
        "Crediting Period Expires",
        "Project Listed Date",
        "Project Registered Date",
        "Verification Bodies",
        "Project is Being Transferred From Another Registry",
        "Early Action Offset Quantification Methodology",
    )

    # No `requests_per_second` override, deliberately. **28 requests in six
    # minutes is not evidence of no rate limit** — 28 of 28 were HTTP 200 with
    # no `429`, no `Retry-After` and no `cf-ray`, but ACR's Cloudflare rule
    # only bit at ~100 requests in ten minutes and a full sync here is ~1,285.
    # The default ~1 req/s is unproven-safe rather than proven-safe; if a long
    # run starts taking 429s this registry gets its own rate the way ACR did.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: project id -> what its issuance rows say about it. Built once, from
        #: the issuance report, on the first project row. See `_issuance_facts`.
        self._facts: dict[int, dict[str, Any]] | None = None
        #: How many rows of each ledger have been normalised, which is a row's
        #: position in the feed. **The issuance report publishes no serial
        #: number and no id of any kind**, so position is part of what
        #: distinguishes one of its rows from the next; without it two rows
        #: alike in project, vintage, quantity and date would hash to one key,
        #: each upserting over the last, and `reconciled()` would still pass
        #: because it counts rows *yielded*, not rows written. See
        #: `_credit_key`.
        self._seen: dict[str, int] = {}

    # -- the issuance report, read from the project side --------------------

    def _issuance_facts(self) -> dict[int, dict[str, Any]]:
        """Per project, the things only the issuance rows publish.

        Four columns the sheet or a later question needs and the project report
        does not carry:

        * `Protocol and Version` — the methodology, and the only one published.
        * `Offset Credits Converted to VCUs` — populated on **2 of 5,170
          rows**, and the registry stating a cross-registry overlap with Verra
          itself. It is summed to a number rather than kept as text so the
          overlap check that runs after this is a query.
        * `Offset Credits Currently in Reserve Buffer Pool` — the buffer-pool
          balance, 1,781 rows.
        * the ARB / WA ECO / CORSIA / ICVCM eligibility flags and
          `Corresponding Adjustment` — market eligibility, which is **not** an
          additional certification (Cercarbono's `elegible` list and Puro's
          `sdgs` are the same shape) but is what a compliance question reads.

        Two extra requests, once per run, against a ledger the sync fetches
        anyway. `credit_events` has no `extra` column — only `projects` do — so
        the project row is the only place any of this can be kept.
        """
        if self._facts is None:
            facts: dict[int, dict[str, Any]] = {}
            for raw in self.report_rows(ISSUANCES):
                row = _row(raw)
                project_id = _project_key(row.get("Project ID"))
                if project_id is None:
                    continue
                fact = facts.setdefault(
                    project_id,
                    {
                        "protocols": [],
                        "converted_to_vcus": 0.0,
                        "reserve_buffer_pool": 0.0,
                        "eligibility": [],
                    },
                )
                fact["protocols"].append(row.get("Protocol and Version"))
                fact["converted_to_vcus"] += (
                    _number(row.get("Offset Credits Converted to VCUs")) or 0.0
                )
                fact["reserve_buffer_pool"] += (
                    _number(row.get("Offset Credits Currently in Reserve Buffer Pool"))
                    or 0.0
                )
                for column in (
                    "ARB Eligible",
                    "WA ECO Eligible",
                    "Eligible for CORSIA 2021-2023 Compliance Period",
                    "Eligible for CORSIA 2024-2026 Compliance Period",
                    "ICVCM CCP Eligible",
                ):
                    if _stated(row.get(column)):
                        fact["eligibility"].append(f"{column}: {row[column].strip()}")
            self._facts = facts
        return self._facts

    # -- the two hooks the platform calls ----------------------------------

    def _project_row(
        self, raw: dict[str, str], detail: dict[str, str] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        row = _row(raw)
        detail = _row(detail or {})
        project_id = _project_key(row.get("Project ID"))
        facts = self._issuance_facts().get(project_id or -1, {})
        return normalize_project(row, detail, facts), {"list": raw, "detail": detail}

    def _credit_row(self, resource: str, raw: dict[str, str]) -> dict[str, Any]:
        index = self._seen.get(resource, 0)
        self._seen[resource] = index + 1
        return normalize_credit(resource, _row(raw), index)


# -- normalisation ---------------------------------------------------------
# CAR's column headings -> the registry-neutral db.PROJECT_FIELDS.
#
# Deliberate gaps, measured over the full 1,277-project index on 2026-08-05:
#   * `country_name` — no country name is published anywhere. The list states
#     an ISO code, the detail states the same code, and the report's own
#     filters offer no country field at all. `country_code` is 1,277/1,277, so
#     Continent derives from it and País stays blank rather than being
#     invented. ACR's gap, and the user's answer there was blank.
#   * `city` — `Project Site Location` is a **county list** ("Shasta,
#     Siskiyou, and Trinity Counties", "Wood County, Bowling Green, Ohio"), not
#     a city. Reading a city out of it would be inventing one; it goes to
#     `extra` whole.
#   * `avg_annual_vol_vcu` / `exante_quantity` — no estimate is published on
#     either the report or the detail page. This registry certifies reductions
#     that have already been verified.
#   * `region_name` — only Verra publishes one.
#   * `afolu_names` — no sub-type code. `Project Type` is already the
#     fine-grained vocabulary (32 values), so Tipo Micro keys on it.
#   * `project_size`, `area`, `units_issued` — not published.


def normalize_project(
    record: dict[str, str],
    detail: dict[str, str] | None = None,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail = detail or {}
    facts = facts or {}

    row: dict[str, Any] = {
        "project_id": _project_key(record.get("Project ID")),
        # `CAR1957`, unique across all 1,277 — checked against the registry's
        # own CSV, not assumed — and the reference every serial number is built
        # from.
        "external_id": _stated(record.get("Project ID")),
        "project_name": _stated(record.get("Project Name")),
        # Asserted. What the registry publishes per project is a **protocol**
        # ("Forestry - MX - Version 2.0"), which is the methodology; it never
        # names a standard.
        "standard_name": settings.CAR_STANDARD_NAME,
        # Registered (540), Completed (461), Listed (268), Transitioned (8).
        "status": _stated(record.get("Status")) or _stated(detail.get("Project Status")),
        # Tipo Macro: this registry's own vocabulary, untranslated like every
        # other registry's. 32 values, led by "Forestry - MX" (485), "Improved
        # Forest Management - ARB Compliance" (141) and "Landfill Gas
        # Capture/Combustion" (126).
        "sectoral_scope": _stated(record.get("Project Type"))
        or _stated(detail.get("Project Type")),
        # **From the issuance rows, because the project rows do not carry it.**
        # A project with no issuances has no methodology published anywhere and
        # gets a blank; 376 of 1,277 are in that position. Nothing is inferred
        # from the project type, even though the protocol name is that type
        # plus a version — an inferred methodology would be indistinguishable
        # from a published one in the sheet.
        "methodologies": joined(facts.get("protocols") or (), NOT_STATED),
        "proponents": _stated(record.get("Project Developer")),
        # `_country_code`, not `_stated`: `NA` would be Namibia, not "not
        # stated". No country *name* is published; see the note above.
        "country_code": _country_code(record.get("Project Site Country"))
        or _country_code(detail.get("Country")),
        # Uppercase US state names ("CALIFORNIA"), uppercase Mexican states
        # ("OAXACA", "QUINTANA ROO") and ordinary-case elsewhere ("Santa Fe").
        # Carried through as published, like every registry's vocabulary.
        "state_province": _stated(record.get("Project Site State"))
        or _stated(detail.get("State/Province/Department")),
        # **The detail page only.** The projects report carries none of the
        # three dates, and the issuance report's `Crediting Period` column is a
        # label — `Initial`, `Renewed-Second` — not a date. That, and only
        # that, is what makes a sync ~1,285 requests instead of 8.
        # `_date`, not `_stated`: this registry publishes `10/7/2018` and every
        # column below feeds `db.parse_date`, which reads ISO only. See `_date`.
        "credit_period_start": _date(detail.get("Project Reporting Start Date"))
        or _date(detail.get("Project Commencement Date")),
        "credit_period_end": _date(detail.get("Crediting Period Expires")),
        "project_start_date": _date(detail.get("Project Commencement Date")),
        # Published on the project row, and empty on all 1,277 today. Read
        # anyway: the column exists, and the day it fills nothing needs
        # changing.
        "additional_certification": _stated(
            record.get("Additional Certification(s)")
        ),
    }

    extra = {
        # The id the same project carries in California's ARB or Washington's
        # Ecology registry. **Queryable, not prose**: 2,035 of 2,277
        # cancellations here are conversions out to those programmes, and this
        # is what the units are called once they arrive. `N/A` on the projects
        # that have none is the registry's own word and is dropped by
        # NOT_STATED, so a populated value means a real compliance id.
        "compliance_program_id": _stated(record.get("Compliance Program ID")),
        "compliance_program_status": _stated(record.get("Compliance Program Status")),
        # A county list, not a city. See the deliberate-blanks note above.
        "project_site_location": _stated(record.get("Project Site Location")),
        # The ISO code again, beside the location it belongs to, so the
        # "no country name is published" gap is answerable from `extra` alone
        # without joining back to the column.
        "country_iso": _country_code(record.get("Project Site Country")),
        "cooperative_id": _stated(record.get("Cooperative/ Aggregate ID")),
        "project_owner": _stated(record.get("Project Owner")),
        "offset_project_operator": _stated(record.get("Offset Project Operator")),
        "authorized_project_designee": _stated(
            record.get("Authorized Project Designee")
        ),
        "verification_body": _stated(record.get("Verification Body")),
        "sdg_impact": _stated(record.get("SDG Impact")),
        "project_notes": _stated(record.get("Project Notes")),
        "project_listed_date": _stated(record.get("Project Listed Date")),
        "project_registered_date": _stated(record.get("Project Registered Date")),
        # `Documents` and `Data` are hyperlinks in the grid and the literal
        # word `View` in the CSV, so neither carries anything. `_linked` drops
        # the placeholder rather than storing 1,277 copies of "View".
        "website": _linked(record.get("Project Website")),
        # **The registry's own per-project figure**, and the reason there is no
        # `iter_credit_totals`: summing the issuance rows agrees with it on all
        # 1,277 projects to the unit. Kept so the day they disagree is
        # answerable from the database.
        "stated_registered_credits": _number(
            record.get("Total Number of Offset Credits Registered")
        ),
        # From the issuance rows. See `_issuance_facts` for why each is here.
        # `converted_to_vcus` is the registry stating a cross-registry overlap
        # itself, on 2 of 5,173 rows; `also_registered_as` is which project
        # those units turned into, which it does not state. See
        # ALSO_REGISTERED_AS — and note that unlike the other two overlaps in
        # this database, these are the *same units* in both registries.
        "converted_to_vcus": facts.get("converted_to_vcus") or None,
        "also_registered_as": ALSO_REGISTERED_AS.get(
            _stated(record.get("Project ID")) or ""
        ),
        "reserve_buffer_pool": facts.get("reserve_buffer_pool") or None,
        "eligibility": joined(facts.get("eligibility") or (), NOT_STATED),
        # From the detail page.
        "arb_id": _stated(detail.get("ARB ID")),
        "project_status_detail": _stated(detail.get("Project Status")),
        # The label the issuance report also states, and the reason that
        # column is not a date: `Initial`, `Renewed-Second`, `Renewed-Third`.
        "crediting_period_label": _stated(detail.get("Crediting Period")),
        "transferred_from_another_registry": _stated(
            detail.get("Project is Being Transferred From Another Registry")
        ),
        "verification_bodies": _stated(detail.get("Verification Bodies")),
        "early_action_methodology": _stated(
            detail.get("Early Action Offset Quantification Methodology")
        ),
    }
    row["extra"] = {k: v for k, v in extra.items() if _worth_storing(v)} or None
    return row


#: Per ledger: the date column that states when the event happened, and the
#: columns carrying the registry's own words for why. An issuance has neither a
#: reason nor a beneficiary — it is the units coming into existence.
CREDIT_FIELDS = {
    ISSUANCES: ("Date Issued", ()),
    RETIREMENTS: ("Status Effective", ("Retirement Reason", "Retirement Reason Details")),
    CANCELLATIONS: ("Status Effective", ("Cancellation Reason", "Export Notes")),
}

#: Per ledger, the column holding the row's quantity. The issuance report calls
#: it something else from the other two, and it is **not**
#: `Offset Credits Converted to VCUs` or either buffer-pool column, which are
#: subsets of the same units and would double-count if summed alongside.
QUANTITY_FIELDS = {
    ISSUANCES: "Total Offset Credits Issued",
    RETIREMENTS: "Quantity of Offset Credits",
    CANCELLATIONS: "Quantity of Offset Credits",
}


def _credit_key(resource: str, record: dict[str, str], index: int) -> int:
    """A ledger row's `entity_id`.

    **No ledger row here carries an id of its own.** What it carries is a
    serial-number range (`CAR-1-US-1480-46-1156-FL-2026-10272-852724 to
    860723`), which is unique per row in practice and is what the registry
    itself identifies units by — so it seeds the hash. Hashing rather than
    counting is what keeps the upsert idempotent: a positional key renumbers
    every row the moment the registry inserts one above.

    The issuance report publishes no serial at all, so those rows fall back to
    their own values plus their position in the feed, exactly as
    `markit/api.py` does for a platform that publishes no ids anywhere. Two
    identical issuance rows are two events, which is why the index is in the
    seed and not merely a tiebreak.
    """
    serial = _stated(record.get("Offset Credit Serial Numbers"))
    if serial is not None:
        return hashed_id(REGISTRY, resource, serial)
    return hashed_id(
        REGISTRY,
        resource,
        index,
        record.get("Project ID"),
        record.get("Vintage"),
        record.get(QUANTITY_FIELDS[resource]),
        record.get(CREDIT_FIELDS[resource][0]),
    )


def normalize_credit(
    resource: str, record: dict[str, str], index: int = 0
) -> dict[str, Any]:
    """One ledger row."""
    date_field, reason_fields = CREDIT_FIELDS[resource]
    return {
        "entity_id": _credit_key(resource, record, index),
        "project_id": _project_key(record.get("Project ID")),
        # Thousands-separated in the grid and bare in the CSV. See `_number`.
        "quantity": _number(record.get(QUANTITY_FIELDS[resource])),
        "vintage": _stated(record.get("Vintage")),
        # `Reduction/Removal` is the unit class this registry draws, and it is
        # the distinction that matters commercially: a removal builds a carbon
        # store, a reduction is an emission that never happened.
        "unit_type": _stated(record.get("Reduction/Removal")),
        # **The `Crediting Period` label, and it is deliberately not a date.**
        # `Initial` / `Renewed-Second` / `Renewed-Third`, blank on 4,848 of
        # 5,170 issuance rows. It is the registry's own word for the row and
        # `status` is the only per-row column that can hold one — writing it
        # into `event_date` or `vintage` would put the word "Initial" in a
        # date column, at HTTP 200, with nothing to say so. Not a value
        # `db.credit_totals()` buckets on: it only does that for the resource
        # named `credits`, and each of these is already one bucket.
        "status": _stated(record.get("Crediting Period")),
        # ISO, like every other registry's — see `_date`. The registry writes
        # `07/13/2016`, and a ledger date that sorts as a string has to sort
        # the way a date does.
        "event_date": _date(record.get(date_field)),
        # **No beneficiary field exists.** `Account Holder` is on 11,044 of
        # 11,044 retirements and names the *holder*: 5,894 of them read `On
        # Behalf of Third Party` and name that party only in the free text
        # below. Filling this from it would make every retirement read as a
        # third-party sale the moment `sold_equals_retired` is flipped. Same
        # call ACR and BioCarbon made — against a structured alternative this
        # registry does not have. Parsing the prose is the business's decision.
        "beneficiary": None,
        # The registry's own reason, and the free text beside it, which is the
        # only place a third party is ever named. `credit_events` keeps no raw
        # payload, so this column is the only copy — which is what makes the
        # decision above reversible with a query rather than a re-scrape.
        # 2,035 of 2,277 cancellations read `ARB`, `Canceled for ARB` or
        # `WA ECO`: conversions out to a compliance registry where the units
        # continue to exist, not credits destroyed. ACR's trap in a second
        # registry, at a higher proportion.
        "reason": joined((record.get(f) for f in reason_fields), NOT_STATED, separator=" — "),
        # Published on every ledger row, and empty on all of them today. The
        # CORSIA and ICVCM columns beside it are **market eligibility**, not a
        # co-certification — Cercarbono's `elegible` list and Puro's `sdgs`
        # exactly — so they are not read into this column. They reach `extra`
        # on the project row instead.
        "additional_certification": _stated(record.get("Additional Certification(s)")),
        "serial_no": _stated(record.get("Offset Credit Serial Numbers")),
    }


__all__ = [
    "CANCELLATIONS",
    "CARAPI",
    "ISSUANCES",
    "LEDGERS",
    "REGISTRY",
    "RETIREMENTS",
    "normalize_credit",
    "normalize_project",
]
