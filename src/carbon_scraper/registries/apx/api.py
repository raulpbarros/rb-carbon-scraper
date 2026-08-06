"""The Xpansiv/APX report module: form-posted paging, and a bulk CSV export.

Measured against the live service on 2026-08-05; see docs/api-contract-car.md.

APX is **a platform, not a registry** — the fifth here, after S&P Platts,
legacy Markit, EcoRegistry and ICE GreenTrace. The same classic-ASP module
(`myrpt.asp`, the `xxxx2` form, `submitform2`, `rptdownload.asp`) serves every
tenant on `*.apx.com`: Climate Action Reserve, Climate Forward, and the
renewable-energy and fuel-certificate registries that are out of scope because
their units are not tCO2e. Everything measured about the module lives here;
`registries/car/api.py` is identity plus the fields that tenant populates.

    GET  {site}/myModule/rpt/myrpt.asp?r=<id>   the report, its own printed
                                                total, a session cookie and the
                                                CSRF token
    POST {site}/myModule/rpt/myrpt.asp          form `xxxx2` — one page
    POST {site}/myModule/include/rptdownload.asp  form `frmDownload` — the whole
                                                report as CSV, in one request

**The CSV export is the fetch path and the HTML pager is the fallback.** Every
report downloads complete in one POST, so the whole ledger set is 8 requests
rather than ~400 pages. Both paths are reconciled against the same number: the
`<first> - <last> : <total>` the report prints on every page of every view.

Six things that will cost an afternoon if skipped:

* **`c16e` is a CSRF token, it is not in the `xxxx2` markup, and a missing or
  stale one is not an error.** The POST answers HTTP 200 with the site's
  **home page** — nav, login box, and a table reading "No Records!". A scraper
  counting rows sees an empty report and nothing raises. It is appended to the
  form by script one second after `window.load`; the value is ordinary markup
  in the four *other* forms, which is where it is read from here. It is **per
  session**, and the session cookie comes from the same GET — so the mint and
  the POST must belong to one conversation. See `_report` for what that means
  in a process with a response cache.
* **The refusal has to be recognised structurally.** A report response that
  carries no results grid is a refusal, not an empty registry, and
  `APXRefused` says so — the `GreenTraceBlocked` precedent, for the same
  reason: a caller reading "HTTP 200, zero rows" goes looking for a registry
  that has no projects.
* **`SortORder` has a capital R mid-word in its `name` while its `id` is
  `SortOrder`.** Every field of the export form is scraped off the page rather
  than retyped, and by `name`, never by `id`.
* **`?r=111&pg=2` returns page 1, at HTTP 200.** Paging is a form POST and
  `myrpt.asp` reads nothing from the query string but `r`. The
  silently-ignored-parameter family again.
* **There is no page-size lever and the pager clamps past the end instead of
  ending.** 50 rows, fixed; a probe carrying six plausible page-size names at
  once returned 50 rows and HTTP 200. And `X999whichpage=27` on a 26-page
  report returns page 26 again — so a pager that stops on "an empty page"
  never stops and one that stops on "a short page" stops early. Paging here
  advances on the **printed range** (`<last> >= <total>`) and stops the moment
  a range repeats.
* **The grid must be found structurally.** Its header row is `<td>`, not
  `<th>`, and the only thing that identifies it is that its cells carry
  `submitform2('Asc', …)` sort links. Climate Forward is the same module with
  a different build — different colours, different platform version, and **no
  `c16e` at all** — so a parser keyed on a hex colour breaks on the next
  tenant.

And two things that are *not* traps. The per-project detail page
(`prjView.asp?id1=<n>`) is public, needs no cookie, and its id is derivable
from the project reference alone — checked on 227 grid rows with 0 mismatches
— so it survives the CSV stripping every hyperlink. And **no
`iter_credit_totals` is needed here**: the issuance ledger agrees with the
project list's own `Total Number of Offset Credits Registered` on **1,277 of
1,277 projects, to the unit** (268,262,717 both ways). Nothing is stated twice
with two different answers, so there is nothing for a totals seam to outrank.
Do not add one speculatively.

Ledger names are the caller's to choose and they are load-bearing:
`db.credit_totals()` branches on the exact string `credits` to select Gold
Standard's bucket-by-status semantics, and these records want the
one-ledger-per-resource path that any other name selects.
"""

from __future__ import annotations

import html as html_module
import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ... import settings
from ...http_client import RegistryClient, decoded
from .. import tables
from ..base import ClientOwner, reconciled

log = logging.getLogger(__name__)

#: The resource name of the project report. Ledger names belong to the tenant.
PROJECTS = "projects"

#: The module's own grid size. Not a knob: every page-size name probed was
#: accepted and ignored, and there is no `<select>` on the page at all.
PAGE_SIZE = 50

#: A page count this side of which the loop is wrong rather than the registry.
#: The largest report measured is 11,044 rows — 221 pages — and the CSV route
#: means paging is the fallback, not the normal path.
MAX_PAGES = 2_000

#: The paging form, and the export form. Both are found by `id`; only their
#: *fields* are read by `name` (see `SortORder`).
PAGING_FORM = "xxxx2"
DOWNLOAD_FORM = "frmDownload"

#: What tells the results grid apart from the layout tables around it. Its
#: header cells are `<td>` and carry a sort link; nothing else on the page
#: does. Deliberately not a colour — Climate Forward renders `#F9B25F` where
#: the Reserve renders `#92b7d6`, and both are the same module.
SORT_LINK = re.compile(r"submitform2\(\s*'Asc'")

#: `<td align="center">1 - 50 : 1277</td>`, the registry's own count. There is
#: no total in the CSV, in a header, or anywhere else, which is why the HTML
#: GET is not optional even on the CSV route.
PRINTED_RANGE = re.compile(r">\s*([\d,]+)\s*-\s*([\d,]+)\s*:\s*([\d,]+)\s*<")

#: The token the paging script appends after `window.load`, read out of the
#: `<SCRIPT>` block as a last resort. The four other forms carry the same value
#: as ordinary markup and are tried first.
SCRIPT_TOKEN = re.compile(
    r"setAttribute\(\s*['\"]value['\"]\s*,\s*['\"]([0-9a-fA-F]{16,})['\"]"
)

_FORM = r"<form[^>]*\bid\s*=\s*[\"']{id}[\"'][^>]*>(?P<body>.*?)</form>"
_INPUT = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"([\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))")


class APXRefused(RuntimeError):
    """The module answered a POST with its home page instead of the report.

    Measured 2026-08-05, and it is the reason this is trap number one. A POST
    carrying a missing or a **stale** `c16e` returns HTTP 200, 16 KB, and the
    site's public home page — nav, login box, and a three-column table reading
    "No Records!". `raise_for_status()` sees nothing, a scraper counting rows
    sees an empty report, and a scraper looking for a heading finds one.

    The token is per session and the session cookie is minted by the same GET
    that publishes the token, so this is what a broken *conversation* looks
    like — not a registry with no records, and not a credential we are
    missing. There is no key here: `GET myrpt.asp?r=111` works with no cookie
    at all.

    Raised as itself so nobody reads "HTTP 200, zero rows" as an empty
    registry, which is exactly the shape of the ignored-filter bug this
    codebase keeps meeting.
    """


class APXNotFound(RuntimeError):
    """A detail page that answered HTTP 200 and is not a project.

    `prjView.asp?id1=999999` returns HTTP 200 and a 3 KB page reading
    *"Invalid URL, the Reserve Administrator has been notified!"*. Parsed
    hopefully it yields `{}`, which reaches the pipeline as a real project with
    no crediting period and no dates — and the detail page is the **only**
    source of those. Same shape as EcoRegistry's `ERROR_401` inside a 200.
    """


# -- page reading ----------------------------------------------------------


def _text(value: str) -> str:
    return " ".join(value.split()).strip()


def _int(value: str) -> int:
    return int(value.replace(",", "").strip())


def printed_range(page: str) -> tuple[int, int, int] | None:
    """`(first, last, total)` from the report's own printed range.

    The last plausible match wins, not the first: the pager block sits *below*
    the grid, and a data cell rendering something shaped like `1 - 5 : 9`
    would otherwise be read as the registry's count. "Plausible" is
    `first <= last <= total`, which the pager always satisfies and a
    coincidence rarely does.
    """
    found: tuple[int, int, int] | None = None
    for match in PRINTED_RANGE.finditer(page):
        try:
            first, last, total = (_int(group) for group in match.groups())
        except ValueError:  # pragma: no cover - the regex only matches digits
            continue
        if first <= last <= total:
            found = (first, last, total)
    return found


def form_body(page: str, form_id: str) -> str | None:
    """The markup of one form, by `id`."""
    match = re.search(_FORM.format(id=re.escape(form_id)), page, re.IGNORECASE | re.DOTALL)
    return match.group("body") if match else None


def form_action(page: str, form_id: str) -> str | None:
    """A form's own `action`, so no endpoint is retyped from the doc."""
    match = re.search(
        r"<form[^>]*\bid\s*=\s*[\"']" + re.escape(form_id) + r"[\"'][^>]*>",
        page,
        re.IGNORECASE,
    )
    if not match:
        return None
    for name, *values in _ATTR.findall(match.group(0)):
        if name.lower() == "action":
            return html_module.unescape("".join(values)).strip() or None
    return None


def form_fields(page: str, form_id: str) -> dict[str, str]:
    """Every `<input>` in one form, keyed by its **`name`**.

    By `name` and never by `id`, because the export form spells one field
    `id="SortOrder" name="SortORder"` — a capital R in the middle of the word
    — and posting `SortOrder` posts a field the server does not read. Values
    are HTML-unescaped: the module writes `&#44;` for a comma and `&#95;` for
    the underscore in `Stamp_0`, so a value copied verbatim out of the markup
    is not the value the browser sends.
    """
    body = form_body(page, form_id)
    if body is None:
        return {}
    fields: dict[str, str] = {}
    for tag in _INPUT.findall(body):
        attributes = {
            name.lower(): html_module.unescape("".join(values))
            for name, *values in _ATTR.findall(tag)
        }
        name = attributes.get("name")
        if name:
            fields[name] = attributes.get("value", "")
    return fields


def csrf_token(page: str) -> str | None:
    """The `c16e` value this page's session expects, or None if it has none.

    None is a real answer, not a failure: Climate Forward is the identical
    module and publishes no token at all, so it must be **omitted** there
    rather than sent empty.
    """
    for form in (DOWNLOAD_FORM, "frmSearch", "frmPrint", "frmRequery"):
        value = form_fields(page, form).get("c16e")
        if value:
            return value
    # The paging form's own token is injected by script, so the literal in the
    # `<SCRIPT>` block is the last resort rather than the first read.
    match = SCRIPT_TOKEN.search(page)
    return match.group(1) if match else None


def grid(page: str) -> tuple[list[str], list[dict[str, str]]] | None:
    """The results grid: its headings, and its rows keyed by heading.

    Found structurally — the table whose first row's cells carry
    `submitform2('Asc', …)` sort links — because the header row is `<td>`, not
    `<th>`, and the only other candidate is a colour that differs per tenant.

    None means the page carries no grid at all, which on a report response is
    a refusal rather than an empty report. See `APXRefused`.
    """
    for table in tables.parse_tables(page):
        if not table.rows:
            continue
        header = table.rows[0]
        if not any(SORT_LINK.search(link) for link in header.links):
            continue
        headings = [_text(value) for value in header.values.values()]
        rows: list[dict[str, str]] = []
        for row in table.rows[1:]:
            cells = list(row.values.values())
            if len(cells) != len(headings):
                # Read positionally this is a table full of plausible nonsense
                # — the failure `tables.py` exists to make loud rather than
                # silent. Keyed by heading the surplus is simply dropped and
                # the shortfall comes back empty, and this says so.
                log.error(
                    "APX grid row has %s cell(s) against %s heading(s); "
                    "columns beyond the headings are dropped.",
                    len(cells),
                    len(headings),
                )
            rows.append(
                {
                    heading: _text(cells[index]) if index < len(cells) else ""
                    for index, heading in enumerate(headings)
                }
            )
        return headings, rows
    return None


def label_values(page: str, labels: tuple[str, ...] = ()) -> dict[str, str]:
    """A label/value page (the project detail) read as one mapping.

    The detail page is not a record list: it renders a label beside its value.
    Two layouts are handled because only one of them has been seen — a caller
    that states its `labels` gets exact pairing, and one that does not falls
    back to the `Label:` convention and then to `tables.cell_dict`.
    """
    cells: list[str] = []
    for table in tables.parse_tables(page):
        for row in table.rows:
            cells.extend(_text(value) for value in row.values.values())

    wanted = {label.casefold() for label in labels}
    values: dict[str, str] = {}
    for index, cell in enumerate(cells[:-1]):
        label = cell.rstrip(":").strip()
        if not label:
            continue
        if wanted:
            if label.casefold() not in wanted:
                continue
        elif not cell.endswith(":"):
            continue
        value = cells[index + 1]
        if value and label not in values:
            values[label] = value

    if not values:
        table = tables.results_table(page)
        # Only when the page states real headings. Falling back to a
        # positional read would key an error page's prose as `column0` and
        # hand back a dict that looks like a project.
        if table.headings:
            values = tables.cell_dict(table)
    return values


# -- the CSV export --------------------------------------------------------


def csv_fields(record: str) -> list[str] | None:
    """One CSV record's fields, or None if the record is not finished.

    **The export is not correctly quoted and `csv.reader` mis-columns it
    without raising.** Every field is wrapped in `"`, an embedded `"` is *not*
    doubled, and the registry's own values contain both quotes and commas — so
    RFC 4180 reads a leading `""` as one escaped quote, ends the field at the
    next internal comma, and hands back 24 fields where the header has 23.
    Measured: 13 of the 11,044 retirement rows. Nothing raises, and in all 13
    the break fell in the last populated column, which is why the orphan and
    quantity checks still came out clean. **That position is not a rule.**

    The rule that *is* structural: a record is `"f1","f2",…,"fn",` — every
    field wrapped, the delimiter is the three characters `","`, and the
    trailing comma emits one unnamed empty column which is dropped here. Read
    that way, all 13 malformed rows parse to exactly the header's field count
    and keep their embedded quotes and commas intact.

    None means the record does not end where the line does. Some
    `Cancellation Reason` and `Export Notes` values contain newlines, correctly
    quoted — `r=308` is 2,417 physical lines and 2,277 records — so **never
    count `\\n` to count rows.**
    """
    text = record.rstrip("\r\n")
    if text.endswith(","):
        text = text[:-1]
    if not text.startswith('"') or not text.endswith('"'):
        return None
    return text[1:-1].split('","')


def read_csv(text: str) -> list[dict[str, str]]:
    """The export as heading-keyed rows.

    Heading names are stripped, which is also what makes the CSV path and the
    HTML path produce the same keys: the projects report's
    `Total Number of Offset Credits Registered ` carries a trailing space in
    the CSV header and does not in the grid, where the HTML reader has already
    squashed it. Two paths that key the same data differently is a fallback
    that silently changes the data.
    """
    lines = text.splitlines()
    if not lines:
        return []

    headings = csv_fields(lines[0])
    if headings is None:
        raise ValueError(
            "The export's first line is not a quoted CSV header row: "
            f"{lines[0][:120]!r}"
        )
    headings = [heading.strip() for heading in headings]

    rows: list[dict[str, str]] = []
    buffer = ""
    for line in lines[1:]:
        buffer = line if not buffer else f"{buffer}\n{line}"
        fields = csv_fields(buffer)
        if fields is None or len(fields) < len(headings):
            # The record continues onto the next physical line.
            continue
        if len(fields) > len(headings):
            # Should not happen once the delimiter rule above is applied, so
            # it is logged rather than repaired: guessing which column
            # absorbed the break is the assumption the doc says not to make.
            log.error(
                "APX export row has %s field(s) against %s heading(s); the "
                "surplus is dropped. First field: %s",
                len(fields),
                len(headings),
                fields[0][:60],
            )
        rows.append(dict(zip(headings, fields)))
        buffer = ""
    if buffer.strip():
        log.error("APX export ended mid-record: %s", buffer[:120])
    return rows


def numeric_id(reference: Any) -> int | None:
    """`CAR1957` -> `1957`, the id every per-project route takes.

    The CSV strips every hyperlink — `Documents` and `Data` both read the
    literal string `View` on all 1,277 rows — so the detail URL cannot come
    from the grid's markup. It does not have to: the id is the numeric part of
    the published reference, checked on 227 grid rows across five pages with
    **0 mismatches**.
    """
    digits = "".join(c for c in str(reference or "") if c.isdigit())
    return int(digits) if digits else None


@dataclass
class Report:
    """One report page, and the session it was rendered in.

    The GET is not just the first page: it mints the session cookie, publishes
    the CSRF token the POSTs need, and prints the only total anywhere in this
    platform.
    """

    resource: str
    report_id: int
    page: str
    token: str | None
    first: int
    last: int
    total: int
    headings: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)


class APXReportAPI(ClientOwner):
    """Reads one tenant's public reports off the APX module.

    Implements registries.base.RegistryAdapter. A subclass sets the class
    attributes below and implements the two normalisation hooks; everything
    measured about the platform is shared, so a fix here fixes every tenant.
    """

    #: Value stored in our `registry` column; set by the subclass.
    registry: str = ""
    #: The tenant's host, e.g. "https://thereserve2.apx.com".
    site: str = ""
    #: What `standard_name` is stored as. The module publishes a *compliance
    #: programme* per project, which is not the standard, so this is asserted
    #: by the tenant rather than read.
    standard_name: str = ""
    #: Credit resources, in the order they are scraped. Named deliberately,
    #: not descriptively — see the module docstring.
    ledgers: tuple[str, ...] = ()
    #: resource -> the `r=` report id. `PROJECTS` is required.
    REPORTS: dict[str, int] = {}

    #: Names this tenant's fixture files. Read by no code; it keeps two
    #: tenants' captures from colliding when a human takes one.
    slug: str = ""

    #: The column carrying the published project reference.
    project_id_column: str = "Project ID"

    #: The per-project page is the only source of a crediting period, and it
    #: is 99.4% of a sync's requests. A class switch, so a smoke run can skip
    #: it without the caller inventing a second code path.
    fetch_detail: bool = True

    #: The labels the detail page publishes, if the tenant states them. Empty
    #: means `label_values` pairs on the `Label:` convention instead.
    detail_labels: tuple[str, ...] = ()

    #: What a soft 404 says. `prjView.asp?id1=999999` is HTTP 200 and reads
    #: "Invalid URL, the Reserve Administrator has been notified!".
    soft_404_marker: str = "Invalid URL"

    #: The CSV export is the fetch path: 2 requests a report against ~26 pages
    #: for the smallest of them. Set False to page the HTML grid instead —
    #: both routes reconcile against the same printed total, and the fallback
    #: is a deliberate switch rather than something that happens silently
    #: after a refusal. ~400 extra requests is not a thing to do by accident.
    use_csv: bool = True

    #: Deliberately unset. 28 requests in six minutes were all HTTP 200 with
    #: no `429`, no `Retry-After` and no Cloudflare interstitial — which is
    #: **not** evidence of no limit, it is 28 requests, and a full sync here is
    #: ~1,285. The global ~1 req/s stands until a long run says otherwise. An
    #: adapter may only ever lower this; there is no version of "make it
    #: faster" that ends well. See ACR.
    requests_per_second: float | None = None

    #: What the module actually sends, since it declares nothing at all: no
    #: `charset` on `Content-Type`, no `<meta charset>` in the markup, and no
    #: BOM on the CSV. Measured on the live Reserve, 2026-08-05 — the bytes are
    #: Windows-1252, which is classic ASP's default codepage.
    #:
    #: **Without this every accent in the registry becomes `U+FFFD`**, because
    #: httpx falls back to UTF-8 and decodes with `errors="replace"`: 491 of
    #: CAR's 1,277 projects are Mexican, so `STATE OF MÉXICO`, `MICHOACÁN`,
    #: `Oaxaca de Juárez` and `ASOCIACIÓN DE SILVICULTORES…` all arrived
    #: damaged, at HTTP 200, with nothing in the log. It is not recoverable
    #: after the fact — every accented character collapses onto one replacement
    #: character — which is why it is a platform attribute and not a repair.
    #: `http_client.decoded` still tries UTF-8 strictly first, so a tenant on a
    #: newer build is unaffected.
    encoding: str = "cp1252"

    report_path: str = "/myModule/rpt/myrpt.asp"
    detail_path: str = "/mymodule/reg/prjView.asp"

    def __init__(
        self,
        client: RegistryClient | None = None,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self._bind_client(client, cancel)
        self._reports: dict[str, Report] = {}
        self._rows: dict[str, list[dict[str, str]]] = {}

    # -- plumbing ----------------------------------------------------------

    def _headers(self, *, form: bool = False) -> dict[str, str]:
        """What the site's own visitor sends, and nothing more.

        `settings.BROWSER_HEADERS` carries Verra's `Origin`, and sending one
        registry's origin to another has already cost an afternoon on
        Cercarbono. The content type is stated explicitly on a form post
        because the shared client header says `application/json` and wins over
        the one httpx would infer from `data=` — the same fix ICE GreenTrace
        needed.
        """
        headers = dict(settings.BROWSER_HEADERS)
        headers["Accept"] = "text/html,application/xhtml+xml,*/*"
        headers["Origin"] = self.site
        headers["Referer"] = f"{self.site}{self.report_path}"
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            headers.pop("Content-Type", None)
        return headers

    def report_id(self, resource: str) -> int:
        try:
            return self.REPORTS[resource]
        except KeyError:
            raise ValueError(
                f"{self.registry}: no report id for {resource!r}; "
                f"known reports are {sorted(self.REPORTS)}."
            ) from None

    def report_url(self, resource: str) -> str:
        return f"{self.site}{self.report_path}?r={self.report_id(resource)}"

    def detail_url(self, project_id: int) -> str:
        return f"{self.site}{self.detail_path}?id1={project_id}"

    def _absolute(self, action: str | None, fallback: str) -> str:
        """A form's own action, resolved against the tenant's host."""
        path = (action or fallback).strip()
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.site}{path if path.startswith('/') else '/' + path}"

    # -- the session -------------------------------------------------------

    def _report(self, resource: str, *, refresh: bool = False) -> Report:
        """GET the report page: the session, the token, the total, page 1.

        **This GET deliberately bypasses the response cache**, and the reason
        is the nastiest bug available here. The token is per *session* and the
        session cookie is set by this same response — a token read from a
        cached body belongs to a session that ended hours ago, and the live
        POST that uses it returns the home page at HTTP 200. Worse, that only
        happens on the *second* run: the first one mints both for real and
        everything works. A cache is an optimisation; a dead session is a
        silently empty report.

        Four GETs a sync is the whole cost of getting this right, against
        1,277 detail pages that *are* cached and are 99.4% of the run. Do not
        "optimise" this back — if it is ever changed, the cache must stop
        short-circuiting `httpx`'s cookie jar too, which it cannot, because a
        cached response is constructed rather than received.
        """
        if not refresh and resource in self._reports:
            return self._reports[resource]

        url = self.report_url(resource)
        page = self.client.get_text(
            url,
            headers=self._headers(),
            use_cache=False,
            fallback_encoding=self.encoding,
        )
        found = grid(page)
        if found is None:
            raise APXRefused(
                f"{self.registry}: GET {url} returned a page with no results "
                f"grid ({len(page)} bytes). On this platform that is the site's "
                f"home page wearing an HTTP 200, not a report with no records."
            )
        headings, rows = found
        printed = printed_range(page)
        if printed is None:
            raise APXRefused(
                f"{self.registry}: GET {url} printed no `<first> - <last> : "
                f"<total>` range. That line is the registry's only published "
                f"count and every report prints one on every page, so a page "
                f"without it is not this report."
            )
        first, last, total = printed
        report = Report(
            resource=resource,
            report_id=self.report_id(resource),
            page=page,
            token=csrf_token(page),
            first=first,
            last=last,
            total=total,
            headings=headings,
            rows=rows,
        )
        self._reports[resource] = report
        return report

    def _post(self, url: str, body: dict[str, str]) -> str:
        """A form POST, returned as text.

        Not cached, and not for politeness: the body carries `c16e`, a
        per-session nonce, so every run hashes to a new cache key and no entry
        could ever be re-read. Writing 7 MB of exports that can only ever miss
        is a cost with no benefit — and the request that matters for a re-run,
        the detail page, is cached.
        """
        response = self.client.request(
            "POST",
            url,
            form_body=body,
            headers=self._headers(form=True),
            use_cache=False,
        )
        response.raise_for_status()
        # The export declares no charset either, and it is where the Spanish
        # project names and retirement reasons live. See `encoding`.
        return decoded(response, self.encoding)

    def _with_token(self, report: Report, body: dict[str, str]) -> dict[str, str]:
        """Attach the session's CSRF token, if this build publishes one.

        Sent when the page has one and **omitted** otherwise, rather than sent
        empty: Climate Forward is the identical module with no `c16e` in its
        markup at all.
        """
        form = dict(body)
        if report.token:
            form["c16e"] = report.token
        else:
            form.pop("c16e", None)
        return form

    # -- paging ------------------------------------------------------------

    def _page_html(self, resource: str, page: int) -> str:
        """One page of the grid, by POSTing the report's own form back at it.

        Never `?pg=<n>`: that parameter is accepted, ignored, and answered
        with page 1 at HTTP 200.
        """
        report = self._report(resource)
        body = form_fields(report.page, PAGING_FORM)
        # `r` is set from our own table rather than scraped. It is *our* input
        # — the report we asked for — and the module's markup for it is
        # broken: it emits `</tr>name="r" value="111"/>` with the opening
        # `<input` swallowed, so no input-tag reader can see it.
        body["r"] = str(report.report_id)
        body["X999paging"] = "On"
        body["X999whichpage"] = str(page)
        url = self._absolute(form_action(report.page, PAGING_FORM), self.report_path)
        return self._post(url, self._with_token(report, body))

    def page_rows(self, resource: str, page: int) -> list[dict[str, str]]:
        """One page of the report, heading-keyed."""
        report = self._report(resource)
        if page == 1:
            return list(report.rows)
        html = self._page_html(resource, page)
        found = grid(html)
        if found is None:
            raise APXRefused(
                f"{self.registry}: page {page} of {resource} came back without "
                f"a results grid. A missing or stale `c16e` is answered with "
                f"the site's home page at HTTP 200 — re-mint the session "
                f"rather than reading this as an empty page."
            )
        return found[1]

    def _paged_rows(self, resource: str) -> list[dict[str, str]]:
        """Every row, by walking the grid — the fallback path.

        Stops on the **printed range**, never on the row count. Asking for a
        page past the end returns the *last* page again, at HTTP 200 with a
        full complement of rows, so "an empty page ends it" never ends and "a
        short page ends it" ends early on any report whose last page is
        partial. A repeated range is the clamp, and it stops the loop before
        those rows are yielded a second time.
        """
        report = self._report(resource)
        collected: list[dict[str, str]] = []
        seen: set[tuple[int, int]] = set()
        page_number = 1
        page_html = report.page
        while page_number <= MAX_PAGES:
            if page_number > 1:
                page_html = self._page_html(resource, page_number)
            found = grid(page_html)
            if found is None:
                raise APXRefused(
                    f"{self.registry}: page {page_number} of {resource} came "
                    f"back without a results grid — the home page at HTTP 200, "
                    f"not an empty page."
                )
            printed = printed_range(page_html)
            if printed is None:
                raise APXRefused(
                    f"{self.registry}: page {page_number} of {resource} printed "
                    f"no range, so there is nothing to page against."
                )
            first, last, total = printed
            if (first, last) in seen:
                log.error(
                    "%s %s: page %s repeated the range %s - %s of %s. This "
                    "pager clamps past the end instead of ending, so those "
                    "rows are dropped rather than collected twice.",
                    self.registry,
                    resource,
                    page_number,
                    first,
                    last,
                    total,
                )
                break
            seen.add((first, last))
            collected.extend(found[1])
            if last >= total:
                break
            page_number += 1
        else:
            raise RuntimeError(
                f"{self.registry}: {resource} paging did not terminate within "
                f"{MAX_PAGES} pages. Something is wrong with the loop, not the "
                f"registry."
            )
        return collected

    # -- the export --------------------------------------------------------

    def export_csv(self, resource: str) -> str:
        """The whole report as CSV, in one POST, with one re-mint on refusal.

        The re-mint is the second half of the cache answer above. The GET is
        uncached so the token is always freshly minted, but a session can also
        simply expire — this is classic ASP and its sessions time out — and
        the symptom is indistinguishable from an empty report. One retry with
        a new session costs one GET and turns a silent zero into either data
        or a named exception.
        """
        report = self._report(resource)
        for attempt in (1, 2):
            body = form_fields(report.page, DOWNLOAD_FORM)
            if not body:
                raise APXRefused(
                    f"{self.registry}: the {resource} report page carries no "
                    f"{DOWNLOAD_FORM!r} form, so there is no export to ask for."
                )
            # `downloadnow(1)` sets exactly this and submits. Everything else
            # in the form is the session's own state — `Data=Stamp_0` is a
            # handle to the recordset the server stored when it rendered the
            # page — and is posted back untouched.
            body["FormatType"] = "csv"
            url = self._absolute(
                form_action(report.page, DOWNLOAD_FORM),
                "/myModule/include/rptdownload.asp",
            )
            text = self._post(url, self._with_token(report, body))
            # A CSV export opens with a quoted header; the home page opens with
            # markup. `﻿` because an export may or may not carry a BOM.
            if text.lstrip("﻿ \t\r\n").startswith('"'):
                return text
            if attempt == 1:
                log.warning(
                    "%s %s: the export came back as markup rather than CSV — "
                    "a stale session. Re-minting once.",
                    self.registry,
                    resource,
                )
                report = self._report(resource, refresh=True)
                continue
            raise APXRefused(
                f"{self.registry}: POST {url} for {resource} returned "
                f"{len(text)} bytes that are not a CSV export, twice, across "
                f"two sessions. This platform answers a missing or stale "
                f"`c16e` with its home page at HTTP 200 — it is not an empty "
                f"report and it is not a credential we are missing."
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _csv_rows(self, resource: str) -> list[dict[str, str]]:
        rows = read_csv(self.export_csv(resource))
        report = self._report(resource)
        # The export is a superset of the grid, not a different report: it
        # pads one unnamed trailing column (dropped on read) and, on the
        # cancellation report only, ships an `Export Notes` the grid's own
        # `Exclude` list hides. No overlap at all means the POST fetched
        # something else entirely, which is worth more than a log line.
        if rows:
            missing = [h for h in report.headings if h not in rows[0]]
            if len(missing) == len(report.headings) and report.headings:
                raise APXRefused(
                    f"{self.registry}: the {resource} export shares no column "
                    f"with the report it was asked for. Grid: "
                    f"{report.headings[:3]}…; export: {sorted(rows[0])[:3]}…"
                )
            if missing:
                log.error(
                    "%s %s: the export is missing %s column(s) the grid "
                    "publishes: %s. Read by heading, those come back empty "
                    "rather than wrong — but they come back empty.",
                    self.registry,
                    resource,
                    len(missing),
                    ", ".join(missing[:5]),
                )
        return rows

    # -- the adapter contract ----------------------------------------------

    def report_total(self, resource: str) -> int:
        """What the report itself says it holds.

        The registry's own count, printed in a `<td>` of its own above the
        pager on every page of every view, and the only one published
        anywhere: there is no total in the CSV, in a header or on the site.
        """
        return self._report(resource).total

    def report_rows(self, resource: str) -> list[dict[str, str]]:
        """Every row of one report, and reconciled against its printed total.

        Cached on the instance: `count()` and the scrape read the same list, so
        the two numbers cannot disagree, and a report is fetched once per run.
        """
        if resource in self._rows:
            return self._rows[resource]
        rows = self._csv_rows(resource) if self.use_csv else self._paged_rows(resource)
        total = self.report_total(resource)
        if len(rows) != total:
            log.error(
                "INCOMPLETE: %s %s returned %s row(s) against the %s the "
                "report prints for itself.",
                self.registry,
                resource,
                len(rows),
                total,
            )
        self._rows[resource] = rows
        return rows

    def project_total(self) -> int:
        return self.report_total(PROJECTS)

    def count(self, resource: str) -> int:
        return self.report_total(resource)

    def detail(self, project_id: int) -> dict[str, str]:
        """One project's detail page — the only source of a crediting period.

        Public and served to a cold client with no cookie, so it is fetched
        through the ordinary cached path: it is 99.4% of a sync's requests and
        the cache is what makes a re-run cheap.

        **A bad id is a soft 404 at HTTP 200.** `raise_for_status()` sees
        nothing and a hopeful parse yields `{}`, which reaches the pipeline as
        a project with no dates — indistinguishable from a real project whose
        crediting period the registry never published.
        """
        url = self.detail_url(project_id)
        page = self.client.get_text(
            url, headers=self._headers(), fallback_encoding=self.encoding
        )
        if self.soft_404_marker and self.soft_404_marker in page:
            raise APXNotFound(
                f"{self.registry}: {url} answered HTTP 200 with "
                f"{self.soft_404_marker!r} — this platform states a missing "
                f"project as a soft 404, so it is not a project with no "
                f"crediting period."
            )
        values = label_values(page, self.detail_labels)
        if not values:
            raise APXNotFound(
                f"{self.registry}: {url} answered HTTP 200 with no label/value "
                f"pairs ({len(page)} bytes). An empty detail is never a valid "
                f"answer here — the page is the only source of the dates."
            )
        return values

    def iter_projects(
        self, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        yield from reconciled(
            self._projects(),
            expected=self.project_total(),
            progress=progress,
            max_records=max_records,
            label=f"{self.registry} projects",
        )

    def _projects(self) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """Lazily, so `--limit` stops before 1,277 detail pages are fetched."""
        for raw in self.report_rows(PROJECTS):
            detail: dict[str, str] = {}
            project_id = numeric_id(raw.get(self.project_id_column))
            if self.fetch_detail and project_id is not None:
                try:
                    detail = self.detail(project_id)
                except APXNotFound as exc:
                    # Logged rather than raised: one dud id must not end a
                    # ~1,285-request sync, and every write is an idempotent
                    # upsert so a resumed run repeats the attempt. The row
                    # still ships — loudly, with its dates blank.
                    log.error("%s: %s", self.registry, exc)
            yield self._project_row(raw, detail)

    def iter_credits(
        self, resource: str, *, max_records: int | None = None, progress: Any = None
    ) -> Iterator[dict[str, Any]]:
        rows = self.report_rows(resource)
        yield from reconciled(
            self._credits(resource, rows),
            expected=self.count(resource),
            progress=progress,
            max_records=max_records,
            label=f"{self.registry} {resource}",
        )

    def _credits(
        self, resource: str, rows: list[dict[str, str]]
    ) -> Iterator[dict[str, Any]]:
        """Normalise, and say so if two rows claim one key.

        `entity_id` is half the primary key of `credit_events`, and this
        platform publishes no id for a ledger row — so the key is hashed from
        the row's own values and two identical rows collide. That is a silent
        upsert-over-itself: the run finishes, reconciliation is happy, and the
        ledger is one row short with nothing anywhere to say so.
        """
        seen: set[Any] = set()
        collisions = 0
        for raw in rows:
            record = self._credit_row(resource, raw)
            key = record.get("entity_id")
            if key in seen:
                collisions += 1
            seen.add(key)
            yield record
        if collisions:
            log.error(
                "%s %s: %s row(s) hashed to an entity_id another row already "
                "used. Those rows overwrite each other in `credit_events`; the "
                "key needs something more that distinguishes them.",
                self.registry,
                resource,
                collisions,
            )

    # -- what the tenant knows ---------------------------------------------

    def _project_row(
        self, raw: dict[str, str], detail: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """(normalised row, raw payload) for one project. Tenant's to write.

        The column set differs per tenant — Climate Forward says
        `Project Proponent` where the Reserve says `Project Developer` and has
        no compliance columns at all — so which field carries what is not a
        platform fact.
        """
        raise NotImplementedError

    def _credit_row(self, resource: str, raw: dict[str, str]) -> dict[str, Any]:
        """One ledger row, normalised to db.CREDIT_EVENT_FIELDS."""
        raise NotImplementedError


__all__ = [
    "APXNotFound",
    "APXRefused",
    "APXReportAPI",
    "DOWNLOAD_FORM",
    "MAX_PAGES",
    "PAGE_SIZE",
    "PAGING_FORM",
    "PROJECTS",
    "Report",
    "csrf_token",
    "csv_fields",
    "form_action",
    "form_fields",
    "grid",
    "label_values",
    "numeric_id",
    "printed_range",
    "read_csv",
]
