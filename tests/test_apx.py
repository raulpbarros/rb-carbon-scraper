"""The APX report platform: the CSRF refusal, the clamping pager, the bad CSV.

Runs against `tests/fixtures/car-*.html` and `car-*.csv`, captured from the
live Climate Action Reserve public reports on 2026-08-05. No network.

The fixtures are trimmed — grid data rows 50 -> 10, the projects export
1,277 -> 60 — while the printed range still reads `1 - 50 : 1277`, because
that line is what the module prints and not something the trim could honestly
rewrite. **So no test here may assert a row count against the printed total.**
That mismatch is itself useful: it is exactly the shape of a partial read, and
it is why the paging tests below assert on the printed *range* rather than on
how many rows came back.

Three things get most of the attention, because each is a bug that would not
have raised:

* **The home page wearing an HTTP 200.** A POST with a missing or stale
  `c16e` returns the site's home page, which parses as a report with no
  records. `FakeClient` models the session properly — it mints a token on the
  GET and answers any POST carrying a different one with the home page — so
  the stale-token path is exercised rather than described.
* **The pager clamps instead of ending.** Asking past the end returns the last
  page again, at HTTP 200, full. A loop that stops on an empty or a short page
  never stops or stops early.
* **The CSV is not correctly quoted.** `car-retirements.csv` carries all 13 of
  the malformed rows on purpose, and the tests assert both halves: that
  `csv.reader` gets them wrong, and that this reader does not.
"""

from __future__ import annotations

import csv
import io
import logging

import pytest

from carbon_scraper import http_client, settings
from carbon_scraper.registries import base
from carbon_scraper.registries.apx import api as apx
from carbon_scraper.registries.text import hashed_id

from conftest import RecordingProgress

FIXTURES = settings.FIXTURES_DIR

ISSUANCES = "issuances"
RETIREMENTS = "retirements"
CANCELLATIONS = "cancellations"

#: resource -> the module's own `r=` report id.
REPORT_IDS = {
    apx.PROJECTS: 111,
    ISSUANCES: 112,
    RETIREMENTS: 206,
    CANCELLATIONS: 308,
}

#: What each fixture's own `<first> - <last> : <total>` line prints.
PRINTED_TOTALS = {111: 1277, 112: 5170, 206: 11044, 308: 2277}


def read_fixture(name: str) -> str:
    """A captured page or export, as text.

    `errors="replace"` because the export already carries U+FFFD where the
    registry's own pipeline lost a character — "Carbono, Agua y Biodiversidad
    Indígena" arrives mangled from the live service. Guessing a codepage back
    would invent characters the registry never published.
    """
    return (FIXTURES / name).read_bytes().decode("utf-8", errors="replace")


PROJECTS_PAGE_1 = read_fixture("car-projects.html")
PROJECTS_PAGE_2 = read_fixture("car-projects-page2.html")
ISSUANCES_PAGE = read_fixture("car-issuances.html")
RETIREMENTS_PAGE = read_fixture("car-retirements.html")
CANCELLATIONS_PAGE = read_fixture("car-cancellations.html")
PROJECTS_CSV = read_fixture("car-projects.csv")
RETIREMENTS_CSV = read_fixture("car-retirements.csv")

#: `prjView.asp?id1=1957` — the only source of a crediting period, and the
#: only source of the cross-registry transfer flag. Zero `<th>` and 50 `<td>`,
#: so it is the `Label:` convention that pairs it.
DETAIL_PAGE = read_fixture("car-detail.html")

#: `prjView.asp?id1=999999` — **HTTP 200**, 3 KB, and an apology. Captured
#: from the live service beside the page above, so the two differ in their
#: body and in nothing else.
DETAIL_NOT_FOUND = read_fixture("car-detail-notfound.html")


# -- synthesised pages -----------------------------------------------------
#
# Small enough to keep here rather than commit: the home page is the refusal
# and the detail page has no capture of its own. See the module report.

#: What a POST with a missing or stale `c16e` actually returns: HTTP 200, the
#: public home page, and a table that reads as a report with no records. It
#: carries no `submitform2` sort link anywhere, which is the whole of how it
#: is told apart from a real report.
HOME_PAGE = """
<html><body>
<table><tr><td>Login</td><td><input name="userid"></td></tr></table>
<table>
  <tr bgcolor="#92b7d6"><td>Message Type</td><td>Message</td><td>Receive Date</td></tr>
  <tr><td colspan="3">No Records!</td></tr>
</table>
</body></html>
"""

#: A detail page in the layout that states its labels without a colon, which
#: only a stated vocabulary can pair. Synthesised: the live page uses the
#: colon convention, and this is the branch that would carry another tenant.
DETAIL_PAGE_NO_COLON = """
<html><body><table>
<tr><td>Project ID</td><td>CAR1957</td></tr>
<tr><td>Crediting Period Expires</td><td>04/28/2121</td></tr>
</table></body></html>
"""

#: A page that is neither a project nor the known soft 404. Parsed hopefully
#: it yields `{}` — a project with no crediting period, which is a real thing
#: this registry could publish and this is not it.
BLANK_DETAIL = "<html><body><p></p></body></html>"

#: The paging form's own token is appended by script, so a build that carries
#: it nowhere else has to be read out of the `<SCRIPT>` block.
SCRIPT_ONLY_TOKEN = """
<html><body><SCRIPT>
function addhiddenCsrfInputToQTable() {
    const hiddenInput = document.createElement('input');
    hiddenInput.setAttribute('name', "c16e");
    hiddenInput.setAttribute('value', 'cde72a6cfa7f72b986a8cba1aa01e181');
}
</SCRIPT></body></html>
"""


# -- the fake client -------------------------------------------------------


class Response:
    """A response the way the platform sends one: bytes, and no charset.

    `text` is what httpx would hand back — UTF-8 with `errors="replace"` — so
    a double that only carried a `str` would hide the very bug
    `http_client.decoded` exists for. The module states no charset anywhere,
    so `charset_encoding` is None and the bytes are Windows-1252.
    """

    def __init__(self, text: str, *, encoding: str = "cp1252") -> None:
        self.content = text.encode(encoding, "replace")
        self.status_code = 200
        self.charset_encoding = None

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """Serves the APX module from fixtures, and models its session.

    Two behaviours are deliberate rather than convenient:

    * **A POST carrying the wrong `c16e` gets the home page**, exactly as the
      live module does. The token is minted per GET, so a POST that reuses one
      from an earlier conversation fails the way it fails in production.
    * **Asking for a page past the end returns the last page again**, full and
      at HTTP 200. `X999whichpage=27` on a 26-page report returned page 26.
    """

    def __init__(self) -> None:
        self.gets: list[tuple[str, bool]] = []
        self.posts: list[tuple[str, dict]] = []
        self.pages: dict[int, list[str]] = {
            111: [PROJECTS_PAGE_1, PROJECTS_PAGE_2],
            112: [ISSUANCES_PAGE],
            206: [RETIREMENTS_PAGE],
            308: [CANCELLATIONS_PAGE],
        }
        self.exports: dict[int, str] = {111: PROJECTS_CSV, 206: RETIREMENTS_CSV}
        self.details: dict[int, str] = {}
        self.default_detail = DETAIL_PAGE
        #: The session the last GET minted. A POST must carry this token.
        self.session_token: str | None = None
        #: Answer this many POSTs with the home page whatever they carry.
        self.refuse_posts = 0
        #: The `fallback_encoding` each GET asked for. The module declares no
        #: charset, so asking for none is a silent data loss — see
        #: `test_every_get_states_the_encoding_the_module_does_not`.
        self.encodings: list[str | None] = []
        self.closed = False

    # -- the module's two routes ------------------------------------------

    def get_text(self, url, *, headers=None, params=None, use_cache=True, **kw):
        self.gets.append((url, use_cache))
        self.encodings.append(kw.get("fallback_encoding"))
        if "prjView.asp" in url:
            project_id = int(url.rsplit("id1=", 1)[1])
            return self.details.get(project_id, self.default_detail)
        page = self.pages[self._report_id(url)][0]
        self.session_token = apx.csrf_token(page)
        return page

    def request(self, method, url, *, form_body=None, headers=None, use_cache=True, **kw):
        assert method == "POST", method
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        self.posts.append((url, dict(form_body or {})))
        if self.refuse_posts:
            self.refuse_posts -= 1
            return Response(HOME_PAGE)
        if (form_body or {}).get("c16e") != self.session_token:
            # A missing or stale token is not an error here. It is the home
            # page, at HTTP 200, and it parses as an empty report.
            return Response(HOME_PAGE)
        if "rptdownload.asp" in url:
            return Response(self.exports[int(form_body["r"])] if "r" in form_body else "")
        pages = self.pages[int(form_body["r"])]
        wanted = int(form_body["X999whichpage"])
        return Response(pages[min(wanted, len(pages)) - 1])

    def _report_id(self, url: str) -> int:
        return int(url.rsplit("r=", 1)[1].split("&", 1)[0])

    def close(self) -> None:
        self.closed = True


class FakeExportClient(FakeClient):
    """`rptdownload.asp` posts no `r`, so the export is keyed on the session.

    The live export form carries `Data=Stamp_0`, a handle to the recordset the
    server stored when it rendered *that* page — there is no report id in the
    body at all. This models that: the export served is the one for whichever
    report was last fetched.
    """

    def __init__(self) -> None:
        super().__init__()
        self.last_report = 111

    def get_text(self, url, **kw):
        if "prjView.asp" not in url:
            self.last_report = self._report_id(url)
        return super().get_text(url, **kw)

    def request(self, method, url, *, form_body=None, **kw):
        if "rptdownload.asp" in url and form_body is not None:
            form_body = dict(form_body)
            form_body["r"] = str(self.last_report)
        return super().request(method, url, form_body=form_body, **kw)


class Tenant(apx.APXReportAPI):
    """The identity half, as a real tenant subclass writes it."""

    registry = "TEST_APX"
    site = "https://thereserve2.example"
    standard_name = "Test Standard"
    ledgers = (ISSUANCES, RETIREMENTS, CANCELLATIONS)
    REPORTS = REPORT_IDS
    fetch_detail = False

    def _project_row(self, raw, detail):
        row = {
            "project_id": apx.numeric_id(raw.get("Project ID")),
            "external_id": raw.get("Project ID"),
            "project_name": raw.get("Project Name") or None,
            "standard_name": self.standard_name,
            "credit_period_end": detail.get("Crediting Period Expires"),
        }
        return {k: v for k, v in row.items() if v is not None}, {
            "row": raw,
            "detail": detail,
        }

    def _credit_row(self, resource, raw):
        serial = raw.get("Offset Credit Serial Numbers")
        return {
            "entity_id": hashed_id(self.registry, resource, serial),
            "project_id": apx.numeric_id(raw.get("Project ID")),
            "quantity": float(raw.get("Quantity of Offset Credits") or 0),
            "serial_no": serial,
        }


@pytest.fixture()
def client():
    return FakeExportClient()


@pytest.fixture()
def api(client):
    with Tenant(client) as adapter:
        yield adapter


# -- the printed total -----------------------------------------------------


def test_printed_total_is_read_from_every_report():
    """The registry's own count, and the only one published anywhere."""
    for page, report_id in (
        (PROJECTS_PAGE_1, 111),
        (ISSUANCES_PAGE, 112),
        (RETIREMENTS_PAGE, 206),
        (CANCELLATIONS_PAGE, 308),
    ):
        first, last, total = apx.printed_range(page)
        assert (first, last) == (1, 50)
        assert total == PRINTED_TOTALS[report_id]


def test_printed_total_survives_the_trim():
    """Ten rows under a range that says fifty is the fixture, not the module.

    Pinned because the temptation is to "fix" one against the other, and the
    thing that would break is the only reconciliation this platform offers.
    """
    _, rows = apx.grid(PROJECTS_PAGE_1)
    assert len(rows) == 10
    assert apx.printed_range(PROJECTS_PAGE_1) == (1, 50, 1277)


def test_report_total_and_count_read_the_same_line(api, client):
    assert api.project_total() == 1277
    assert api.count(RETIREMENTS) == 11044
    assert api.report_total(ISSUANCES) == 5170
    # One GET per report, and the page is kept: a total is not worth a second
    # fetch of a 5 MB report.
    assert len(client.gets) == 3


def test_an_unknown_resource_names_the_reports_it_knows(api):
    with pytest.raises(ValueError, match="no report id"):
        api.report_total("holdings")


# -- the grid --------------------------------------------------------------


def test_grid_is_found_structurally_not_by_colour():
    """Its header row is `<td>`, and the sort links are what identify it.

    Climate Forward is the identical module rendered in `#F9B25F` where the
    Reserve renders `#92b7d6`, so a parser keyed on the colour breaks on the
    next tenant.
    """
    headings, rows = apx.grid(PROJECTS_PAGE_1)
    assert headings[0] == "Project ID"
    assert headings[-1] == "Project Website"
    assert len(headings) == 24
    assert rows[0]["Project ID"] == "CAR1957"
    assert rows[0]["Project Type"] == "Reforestation - ARB Compliance"


def test_every_report_has_the_column_set_the_contract_records():
    assert len(apx.grid(PROJECTS_PAGE_1)[0]) == 24
    assert len(apx.grid(ISSUANCES_PAGE)[0]) == 33
    assert len(apx.grid(RETIREMENTS_PAGE)[0]) == 22
    assert len(apx.grid(CANCELLATIONS_PAGE)[0]) == 15


def test_the_home_page_has_no_grid():
    """The refusal is recognised structurally, not by looking for words."""
    assert apx.grid(HOME_PAGE) is None


# -- the forms -------------------------------------------------------------


def test_export_form_is_scraped_by_name_never_by_id():
    """`id="SortOrder"` but `name="SortORder"` — a capital R mid-word."""
    fields = apx.form_fields(PROJECTS_PAGE_1, apx.DOWNLOAD_FORM)
    assert "SortORder" in fields
    assert "SortOrder" not in fields
    assert len(fields) == 15


def test_export_form_values_are_html_unescaped():
    """`Stamp&#95;0` is not the value the browser posts."""
    fields = apx.form_fields(PROJECTS_PAGE_1, apx.DOWNLOAD_FORM)
    assert fields["Data"] == "Stamp_0"
    assert fields["Title"] == "Projects"
    assert fields["Exclude"].startswith(",fiID,ahID,")


def test_paging_form_is_read_not_the_requery_form():
    """The page carries two sets of `X999*` inputs and only one is the pager.

    `frmRequery`'s copy states `X999action=search` and an `X999mydata`; posting
    those asks the module to run a search rather than turn a page.
    """
    fields = apx.form_fields(PROJECTS_PAGE_1, apx.PAGING_FORM)
    assert fields["X999action"] == ""
    assert "X999mydata" not in fields
    assert "X999myquery" in fields


def test_csrf_token_is_read_from_a_sibling_form():
    """It is not in `xxxx2`'s markup; the other four forms carry it plainly."""
    assert apx.csrf_token(PROJECTS_PAGE_1) == "cde72a6cfa7f72b986a8cba1aa01e181"
    assert apx.csrf_token(PROJECTS_PAGE_2) != apx.csrf_token(PROJECTS_PAGE_1)


def test_csrf_token_falls_back_to_the_script_block():
    assert apx.csrf_token(SCRIPT_ONLY_TOKEN) == "cde72a6cfa7f72b986a8cba1aa01e181"


def test_a_build_with_no_token_states_none():
    """Climate Forward publishes none, and it must be omitted, not sent empty."""
    assert apx.csrf_token(HOME_PAGE) is None


def test_form_actions_are_scraped_rather_than_retyped():
    assert apx.form_action(PROJECTS_PAGE_1, apx.PAGING_FORM) == "/myModule/rpt/myrpt.asp"
    assert (
        apx.form_action(PROJECTS_PAGE_1, apx.DOWNLOAD_FORM)
        == "/myModule/include/rptdownload.asp"
    )


# -- the session, and the refusal ------------------------------------------


def test_the_minting_get_bypasses_the_response_cache(api, client):
    """A token from a cached GET belongs to a session that ended hours ago.

    The bug this prevents only appears on the *second* run, which is the worst
    kind: the first run mints both the cookie and the token for real and
    everything works.
    """
    api.report_total(apx.PROJECTS)
    assert client.gets == [(f"{api.site}/myModule/rpt/myrpt.asp?r=111", False)]


def test_the_detail_page_is_cached(client):
    """It is 99.4% of a sync's requests and needs no cookie at all."""
    with Tenant(client) as api:
        api.detail(1957)
    assert client.gets[-1] == (f"{api.site}/mymodule/reg/prjView.asp?id1=1957", True)


def test_a_stale_token_gets_the_home_page(api, client):
    """The live failure, before anything recovers from it.

    HTTP 200, 16 KB, the site's public home page, and nothing raised. A
    scraper counting rows sees an empty report and a scraper looking for a
    heading finds one.
    """
    report = api._report(apx.PROJECTS)
    client.session_token = "a-later-session"
    body = apx.form_fields(report.page, apx.DOWNLOAD_FORM)
    refused = api._post(f"{api.site}/myModule/include/rptdownload.asp", body)
    assert "No Records!" in refused
    assert apx.grid(refused) is None


def test_a_session_that_stays_broken_raises(api, client):
    """Two sessions, two home pages: not an empty report, and not a key."""
    client.refuse_posts = 2
    with pytest.raises(apx.APXRefused, match="not a CSV export"):
        api.export_csv(apx.PROJECTS)
    assert len(client.posts) == 2


def test_a_refused_page_post_raises_rather_than_reading_as_empty(api, client):
    api.report_total(apx.PROJECTS)
    client.refuse_posts = 99
    with pytest.raises(apx.APXRefused, match="results grid"):
        api.page_rows(apx.PROJECTS, 2)


def test_the_export_re_mints_once_and_recovers(api, client, caplog):
    """A classic-ASP session can expire mid-run, and the symptom is a zero."""
    client.refuse_posts = 1
    with caplog.at_level(logging.WARNING):
        rows = api.report_rows(apx.PROJECTS)
    assert len(rows) == 60
    assert "stale session" in caplog.text
    # Two GETs: the original mint and the re-mint. Two POSTs: the refused one
    # and the one that worked.
    assert sum(1 for url, _ in client.gets if "myrpt.asp" in url) == 2
    assert len(client.posts) == 2


def test_a_page_with_no_printed_range_is_a_refusal(client):
    client.pages[111] = [HOME_PAGE]
    with Tenant(client) as api:
        with pytest.raises(apx.APXRefused):
            api.report_total(apx.PROJECTS)


def test_the_token_is_sent_with_every_post(api, client):
    api.report_rows(apx.PROJECTS)
    url, form = client.posts[0]
    assert form["c16e"] == "cde72a6cfa7f72b986a8cba1aa01e181"
    assert form["FormatType"] == "csv"
    assert form["Data"] == "Stamp_0"


# -- paging ----------------------------------------------------------------


def test_paging_is_a_form_post_never_a_query_parameter(api, client):
    """`?r=111&pg=2` returns page 1, at HTTP 200."""
    api.page_rows(apx.PROJECTS, 2)
    url, form = client.posts[-1]
    assert url == f"{api.site}/myModule/rpt/myrpt.asp"
    assert form["X999paging"] == "On"
    assert form["X999whichpage"] == "2"
    # `r` is set from our own table: the module's markup for that input is
    # broken (`</tr>name="r" value="111"/>`) and no input reader can see it.
    assert form["r"] == "111"
    assert not any("pg=" in url for url, _ in client.gets)


def test_page_two_shares_no_project_with_page_one(api):
    first = {row["Project ID"] for row in api.page_rows(apx.PROJECTS, 1)}
    second = {row["Project ID"] for row in api.page_rows(apx.PROJECTS, 2)}
    assert first and second
    assert not (first & second)


def test_paging_advances_on_the_printed_range_and_stops_on_the_clamp(client, caplog):
    """Two real pages, then the module hands page 2 back for page 3.

    The rows must not be collected twice and the loop must not spin. A pager
    that stopped on an empty or a short page would do neither correctly: the
    clamped page is full, and every page here is short because of the trim.
    """
    api = Tenant(client)
    api.use_csv = False
    with caplog.at_level(logging.ERROR):
        rows = api.report_rows(apx.PROJECTS)
    assert len(rows) == 20
    assert len({row["Project ID"] for row in rows}) == 20
    assert "repeated the range" in caplog.text
    # Page 1 came from the GET; only pages 2 and 3 cost a POST.
    assert len(client.posts) == 2


def test_paging_stops_when_the_range_reaches_the_total(client):
    """A report that fits on one page costs no POST at all."""
    api = Tenant(client)
    api.use_csv = False
    client.pages[112] = [ISSUANCES_PAGE.replace("1 - 50 : 5170", "1 - 50 : 50")]
    rows = api.report_rows(ISSUANCES)
    assert len(rows) == 10
    assert client.posts == []


def test_short_reads_are_reported_not_swallowed(api, caplog):
    """60 export rows against a printed 1,277 is what a partial read looks like."""
    with caplog.at_level(logging.ERROR):
        api.report_rows(apx.PROJECTS)
    assert "INCOMPLETE" in caplog.text


# -- the CSV ---------------------------------------------------------------


def test_a_standard_csv_reader_mis_columns_thirteen_rows():
    """Half the point: the malformation is real and silent.

    `csv.reader` raises nothing on any of them — the row simply arrives with
    one field more than the header has.
    """
    rows = list(csv.reader(io.StringIO(RETIREMENTS_CSV)))
    header = rows[0]
    wrong = [row for row in rows[1:] if len(row) != len(header)]
    assert len(wrong) == 13
    assert all(len(row) == len(header) + 1 for row in wrong)


def test_this_reader_gets_all_thirteen_right():
    rows = apx.read_csv(RETIREMENTS_CSV)
    assert len(rows) == 21
    assert all(len(row) == 22 for row in rows)


def test_the_embedded_quotes_and_commas_survive():
    """The registry's own value has literal quotes; the exporter wrapped it
    again, and RFC 4180 then ends the field at the comma inside it."""
    rows = apx.read_csv(RETIREMENTS_CSV)
    doubled = rows[8]["Retirement Reason Details"]
    assert doubled.startswith('"Retirement of Carbon Offsets in behalf of Palo Alto')
    assert doubled.endswith('Mexico Forest Protocol"')
    assert "Utilities, from CARBIOIN" in doubled

    inline = rows[14]["Retirement Reason Details"]
    assert inline == 'On behalf of "We Are Neutral, Inc."'

    mid = rows[12]["Retirement Reason Details"]
    assert '"Industria Grafica Eurostampa Spa"' in mid
    assert "scopo 1, anno 2024" in mid

    # And the columns before the break stayed aligned, which is what a
    # csv.reader row loses.
    assert rows[8]["Account Holder"].startswith("INTEGRADORA")
    assert rows[8]["Retirement Reason"] == "Environmental Benefit"
    assert rows[8]["Quantity of Offset Credits"] == "207"


def test_the_trailing_unnamed_column_is_dropped():
    """The exporter emits a final comma; the grid has no such column."""
    rows = apx.read_csv(PROJECTS_CSV)
    assert "" not in rows[0]
    assert len(rows[0]) == 24


def test_a_record_may_span_physical_lines():
    """`r=308` is 2,417 physical lines and 2,277 records. Never count `\\n`."""
    text = (
        '"Vintage","Project ID","Cancellation Reason",\n'
        '"2020","CAR1","ARB",\n'
        '"2021","CAR2","Canceled for\nregulatory reasons",\n'
        '"2022","CAR3","WA ECO",\n'
    )
    rows = apx.read_csv(text)
    assert len(rows) == 3
    assert rows[1]["Cancellation Reason"] == "Canceled for\nregulatory reasons"
    assert rows[2]["Project ID"] == "CAR3"


def test_a_header_that_is_not_a_header_says_so():
    with pytest.raises(ValueError, match="not a quoted CSV header"):
        apx.read_csv(HOME_PAGE)


def test_surplus_fields_are_reported_rather_than_repaired(caplog):
    """Guessing which column absorbed a break is the assumption to avoid."""
    text = '"A","B",\n"one","two","three",\n'
    with caplog.at_level(logging.ERROR):
        rows = apx.read_csv(text)
    assert rows == [{"A": "one", "B": "two"}]
    assert "surplus is dropped" in caplog.text


# -- the two paths must agree ----------------------------------------------


def test_csv_and_html_produce_the_same_keys():
    """A fallback that keys the same data differently changes the data.

    The export header spells `Total Number of Offset Credits Registered ` with
    a trailing space that the grid does not, which is exactly the sort of
    difference that survives review and breaks a column.
    """
    headings, _ = apx.grid(PROJECTS_PAGE_1)
    rows = apx.read_csv(PROJECTS_CSV)
    assert set(rows[0]) == set(headings)


def test_csv_and_html_produce_the_same_values():
    _, html_rows = apx.grid(PROJECTS_PAGE_1)
    csv_rows = apx.read_csv(PROJECTS_CSV)
    for html_row, csv_row in zip(html_rows, csv_rows):
        assert html_row["Project ID"] == csv_row["Project ID"]
        assert html_row == {key: csv_row[key] for key in html_row}


def test_an_export_of_another_report_is_a_refusal(client):
    """No column in common is not a schema drift; it is the wrong recordset."""
    client.exports[111] = '"Nothing","At","All",\n"a","b","c",\n'
    with Tenant(client) as api:
        with pytest.raises(apx.APXRefused, match="shares no column"):
            api.report_rows(apx.PROJECTS)


def test_missing_grid_columns_are_reported(client, caplog):
    """Read by heading they come back empty rather than wrong — but empty."""
    trimmed = "\n".join(
        line.replace('"Project Website",', "") for line in PROJECTS_CSV.splitlines()
    )
    client.exports[111] = trimmed
    with Tenant(client) as api, caplog.at_level(logging.ERROR):
        api.report_rows(apx.PROJECTS)
    assert "Project Website" in caplog.text


# -- the detail page -------------------------------------------------------


def test_the_detail_url_is_derived_from_the_reference(api):
    """The CSV strips every hyperlink, so the id cannot come from the markup."""
    assert apx.numeric_id("CAR1957") == 1957
    assert apx.numeric_id("") is None
    assert apx.numeric_id(None) is None
    assert api.detail_url(1957) == f"{api.site}/mymodule/reg/prjView.asp?id1=1957"


def test_the_detail_page_is_the_only_source_of_the_dates(api):
    """All three date fields, and the registry publishes them nowhere else.

    The projects report carries none of them, which is the whole reason a sync
    is ~1,285 requests rather than 8.
    """
    values = api.detail(1957)
    assert values["Project Commencement Date"] == "2/8/2023"
    assert values["Project Reporting Start Date"] == "1/30/2027"
    assert values["Crediting Period Expires"] == "1/29/2053"


def test_the_live_detail_page_pins_every_label_it_publishes():
    """Eighteen labels off the real `prjView.asp?id1=1957`, exactly."""
    assert apx.label_values(DETAIL_PAGE) == {
        "Project ID": "CAR1957",
        "ARB ID": "CAFR6957",
        "Offset Project Operator": "Sierra Pacific Industries",
        "Authorized Project Designee": "None",
        "Project Name": "2021 Fire Refo",
        "Project Description": (
            "The project consists of areas that were significantly burned in "
            "the Salt, Antelope, Monument, and River Complex wildfires of 2021"
        ),
        "Project is Being Transferred From Another Registry": "No",
        "Crediting Period": "Initial",
        "Project Type": "Reforestation - ARB Compliance",
        "Project Commencement Date": "2/8/2023",
        "Project Reporting Start Date": "1/30/2027",
        "Project Site Location": "Shasta, Siskiyou, and Trinity Counties",
        "State/Province/Department": "CALIFORNIA",
        "Country": "US",
        "Project Status": "Listed",
        "Crediting Period Expires": "1/29/2053",
        "Project Listed Date": "04/05/2024",
        "Documents": "View",
    }


def test_the_detail_page_has_no_table_headings():
    """Zero `<th>` and 50 `<td>`, so the `Label:` convention is what fires.

    Worth pinning rather than assuming: `tables.cell_dict` pairs a heading row
    against a value row and would return nothing here, and the day this page
    grows a `<th>` the fallback order changes underneath the parser.
    """
    assert DETAIL_PAGE.lower().count("<th") == 0
    assert DETAIL_PAGE.lower().count("<td") == 50


def test_crediting_period_on_the_detail_is_a_label_not_a_date_range():
    """`Initial` — and now the trap bites in two places.

    On the issuance report `Crediting Period` is a label (`Initial` 291,
    `Renewed-Second` 30, blank on 4,848 of 5,170) and the contract doc warns
    that the column name is the trap. The **detail page uses the same name for
    the same kind of value**, next to the three fields that really are dates.
    A tenant reaching for the obviously-named column on either surface gets a
    vocabulary where it wanted a range.
    """
    values = apx.label_values(DETAIL_PAGE)
    assert values["Crediting Period"] == "Initial"
    assert "-" not in values["Crediting Period"]
    # The dates live under their own names, on this page only.
    assert values["Crediting Period Expires"] == "1/29/2053"


def test_the_cross_registry_transfer_flag_is_passed_through(api):
    """`Project is Being Transferred From Another Registry`, per project.

    It is in neither the contract doc nor the field mapping, and it is a
    registry stating an overlap about itself — the question every registry
    added here has had to be asked before scraping rather than after. Two
    known duplicates are already live in the database (Cercarbono/BioCarbon,
    ACR/Verra), and both were found by asking. `detail()` passes it through
    untouched so a tenant can put it in `extra` and a cross-check can read it.
    """
    values = api.detail(1957)
    assert values["Project is Being Transferred From Another Registry"] == "No"


def test_labels_with_no_value_are_dropped_not_returned_empty():
    """Deliberate: an empty cell is not a publication.

    `Project Website`, `Project Registered Date` and `Verification Bodies` are
    all rendered on this page with an empty value cell, and none of them comes
    back. The cost is that a caller cannot tell "the registry published
    nothing" from "the label was not on the page" — and that is fine here,
    because the answer is the same either way: `stated()` maps both to None
    and the column is blank. Never invent data is the rule; distinguishing two
    flavours of absence would not change a single cell.
    """
    values = apx.label_values(DETAIL_PAGE)
    for label in ("Project Website", "Project Registered Date", "Verification Bodies"):
        assert f"{label}:" in DETAIL_PAGE
        assert label not in values


def test_labels_commented_out_of_the_markup_never_reach_the_parser():
    """A different absence from the one above, and it is not empty values.

    The three Early Action labels the contract doc lists are inside an HTML
    comment on this page — the module comments whole `<tr>` blocks out — so
    they are never cells and no pairing rule can reach them. Stating them in a
    tenant's `detail_labels` would not help. That is the registry's own
    decision about its page, not a parser gap, and it is pinned so the two
    kinds of missing label do not get conflated when someone reads the doc's
    label list and finds four of them absent.
    """
    early = (
        "Reporting Periods Eligible for Early Action",
        "Reporting Periods Approved for Early Action",
        "Early Action Offset Quantification Methodology",
    )
    for label in early:
        assert label in DETAIL_PAGE  # present in the markup...
    assert apx.label_values(DETAIL_PAGE, early) == {}  # ...and not in the page


def test_a_stated_label_vocabulary_pairs_without_a_colon():
    assert apx.label_values(DETAIL_PAGE_NO_COLON) == {}
    assert apx.label_values(
        DETAIL_PAGE_NO_COLON, ("Project ID", "Crediting Period Expires")
    ) == {"Project ID": "CAR1957", "Crediting Period Expires": "04/28/2121"}


def test_a_soft_404_raises_rather_than_returning_nothing(client):
    """The live `id1=999999`: HTTP 200, 3 KB, and no project.

    Returned as `{}` it reaches the pipeline as a real project whose crediting
    period the registry never published — and this is the only page that could
    have published one, so nothing downstream could ever notice.
    """
    client.details[999999] = DETAIL_NOT_FOUND
    with Tenant(client) as api:
        with pytest.raises(apx.APXNotFound, match="Invalid URL"):
            api.detail(999999)
        assert apx.label_values(DETAIL_NOT_FOUND) == {}


def test_the_status_code_is_not_what_tells_the_two_pages_apart(client):
    """Both fixtures were captured at HTTP 200. Only the body differs.

    `raise_for_status()` sees nothing on either, which is the same shape as
    EcoRegistry's `ERROR_401` inside a 200 and the Platts platform's
    `totalEntities: 0`. Checking the status code cannot do this job.
    """
    served = Response(DETAIL_NOT_FOUND)
    assert served.status_code == 200
    assert served.raise_for_status() is None

    client.details[1957] = DETAIL_PAGE
    client.details[999999] = DETAIL_NOT_FOUND
    with Tenant(client) as api:
        assert api.detail(1957)["Project ID"] == "CAR1957"
        with pytest.raises(apx.APXNotFound):
            api.detail(999999)


def test_a_detail_with_no_pairs_also_raises(client):
    client.details[999998] = BLANK_DETAIL
    with Tenant(client) as api:
        with pytest.raises(apx.APXNotFound, match="no label/value pairs"):
            api.detail(999998)


def test_one_dud_id_does_not_end_a_sync(client, caplog):
    """~1,285 requests must not be lost to a single unpublished project."""
    client.details[1957] = DETAIL_NOT_FOUND
    api = Tenant(client)
    api.fetch_detail = True
    with caplog.at_level(logging.ERROR):
        rows = list(api.iter_projects(max_records=2))
    assert len(rows) == 2
    assert "Invalid URL" in caplog.text
    # The row still ships, with its dates blank and an error in the log.
    assert rows[0][0]["external_id"] == "CAR1957"
    assert "credit_period_end" not in rows[0][0]


# -- the adapter contract --------------------------------------------------


def test_it_is_a_registry_adapter(api):
    assert isinstance(api, base.RegistryAdapter)


def test_iter_projects_normalises_and_reports_cumulative_progress(api):
    progress = RecordingProgress()
    rows = list(api.iter_projects(progress=progress))
    assert len(rows) == 60
    progress.assert_cumulative(60)
    row, raw = rows[0]
    assert row["project_id"] == 1957
    assert row["external_id"] == "CAR1957"
    assert row["standard_name"] == "Test Standard"
    # The raw payload is the registry's own row, unnormalised.
    assert raw["row"]["Project Type"] == "Reforestation - ARB Compliance"


def test_max_records_stops_before_the_detail_fan_out(client):
    """The fan-out is 99.4% of the cost, so `--limit` has to be lazy."""
    api = Tenant(client)
    api.fetch_detail = True
    rows = list(api.iter_projects(max_records=3))
    assert len(rows) == 3
    assert sum(1 for url, _ in client.gets if "prjView" in url) == 3


def test_iter_credits_normalises_and_reconciles(client, caplog):
    api = Tenant(client)
    progress = RecordingProgress()
    with caplog.at_level(logging.ERROR):
        rows = list(api.iter_credits(RETIREMENTS, progress=progress))
    assert len(rows) == 21
    progress.assert_cumulative(21)
    assert rows[0]["project_id"] == 1480
    assert rows[0]["quantity"] == 1.0
    # 21 rows against the 11,044 the report prints — the trim, and exactly
    # what a partial read looks like, so it must be reported.
    assert "INCOMPLETE" in caplog.text


def test_colliding_event_keys_are_reported(client, caplog):
    """A hashed key that repeats is a silent upsert-over-itself."""

    class Colliding(Tenant):
        def _credit_row(self, resource, raw):
            return {"entity_id": 1, "project_id": 1, "quantity": 1.0}

    api = Colliding(client)
    with caplog.at_level(logging.ERROR):
        list(api.iter_credits(RETIREMENTS))
    assert "hashed to an entity_id another row already used" in caplog.text


def test_the_platform_states_no_per_project_totals(api):
    """1,277 of 1,277 projects agree with the ledger, to the unit.

    `base.credit_totals_of` returning None is the measured answer, not an
    omission: nothing here is stated twice with two different answers, so
    there is nothing for a totals seam to outrank. Do not add one.
    """
    assert base.credit_totals_of(api, ISSUANCES) is None


def test_the_normalisation_hooks_belong_to_the_tenant(client):
    platform = apx.APXReportAPI(client)
    with pytest.raises(NotImplementedError):
        platform._project_row({}, {})
    with pytest.raises(NotImplementedError):
        platform._credit_row(ISSUANCES, {})


def test_the_rate_is_left_to_the_global_setting():
    """28 requests with no 429 is not evidence of no limit; it is 28 requests.

    A full sync here is ~1,285, which is the volume that earned ACR an
    hour-long Cloudflare ban. This attribute may only ever be lowered.
    """
    assert apx.APXReportAPI.requests_per_second is None


def test_an_injected_client_is_not_closed(client):
    with Tenant(client):
        pass
    assert not client.closed


def test_every_get_states_the_encoding_the_module_does_not(api, client):
    """The module sends Windows-1252 and declares nothing, on every route.

    Measured on the live Reserve, 2026-08-05: no `charset` on `Content-Type`,
    no `<meta charset>`, no BOM. httpx therefore assumes UTF-8 and decodes
    with `errors="replace"`, which turns `STATE OF MÉXICO` into
    `STATE OF M�XICO` at HTTP 200 with nothing in the log — and 491 of
    the 1,277 projects on this tenant are Mexican. A GET that forgets to say
    so loses the accents of whatever it fetched, so this asserts the platform
    states it rather than trusting each call site to.
    """
    list(api.iter_projects())
    assert client.encodings, "no GET was made"
    assert set(client.encodings) == {"cp1252"}


def test_the_export_is_decoded_the_same_way(api):
    """The CSV is where the Spanish project names and reasons are.

    It is fetched by POST, so it never travels through `get_text` — the two
    routes are decoded separately and this is what says the second one is.
    """
    assert http_client.decoded(Response("Oaxaca de Juárez"), api.encoding) == (
        "Oaxaca de Juárez"
    )
    assert Response("Oaxaca de Juárez").text != "Oaxaca de Juárez", (
        "the double must reproduce httpx's replacement, or it hides the bug"
    )
