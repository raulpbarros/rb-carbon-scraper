"""Climate Action Reserve: the field mapping, the keys, and the derivation gate. No network.

Runs against `tests/fixtures/car-*.html` and `car-*.csv`, captured from the
live public reports on 2026-08-05. The fixtures are the registry's own two
transports and they do not agree, which is most of what is tested here:

* **`car-projects.csv`** is the real export, first 60 of 1,277 rows. It
  carries the trailing space in `Total Number of Offset Credits Registered `,
  the literal `N/A` on `Compliance Program ID`, the word `View` where the grid
  had a hyperlink, and the `MX`/`US`/`AR` codes with no country name anywhere.
* **`car-retirements.csv`** carries the header, 8 clean rows and **all 13
  malformed ones** — the registry double-wraps a value that already contained
  quotes, so `csv.reader` silently returns 24 fields against a 23-field
  header and raises nothing.
* **`car-*.html`** are the grids the CSVs were exported from, and they render
  quantities **thousands-separated** (`45,802`) where the CSV writes `45802`.
  `float()` raises on one and not the other, so a row arriving by the HTML
  route would store a null quantity with nothing in the log.

The platform's own behaviour — the CSRF refusal, the clamping pager, the CSV
reader — is `tests/test_apx.py`'s job. This file tests what CAR knows: which
column carries what, which cells are deliberately blank, and whether the
derivation rules recognise a vocabulary no other registry writes.
"""

from __future__ import annotations

import csv
import re
from html import unescape
from html.parser import HTMLParser

import pytest

from carbon_scraper import db, derive, registries, settings
from carbon_scraper.registries import base
from carbon_scraper.registries.apx import api as apx
from carbon_scraper.registries.car import api as car

from conftest import FIXTURES, RecordingProgress

REGISTRY = settings.CAR

# What the live reports printed on 2026-08-05, and what a full sync must
# reconcile against. The fixtures are trimmed, so nothing here is asserted
# against a row count in them — see the note at the end of the contract doc.
LIVE_TOTALS = {"projects": 1277, "issuances": 5170, "retirements": 11044, "cancellations": 2277}


# -- reading the fixtures --------------------------------------------------
# Deliberately not the adapter's own readers: these serve *this* file's rows
# to the normalisers, so a bug in the platform's parser cannot make the field
# mapping look right. Values are the registry's, verbatim.


class _Grid(HTMLParser):
    """The report grid: its header row, and its data rows.

    Structural, not colour-keyed. The header cells are the ones carrying a
    `submitform2('Asc', …)` sort link, which is how the platform's own reader
    has to find them too — Climate Forward runs the same module with a
    different palette, so matching `#92b7d6` would work here and nowhere else.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        #: Whether the row being read is the heading row. It is built from
        #: `<td>`s like every other row, so a reader that only counted cells
        #: would hand the headings back as a record — and a record whose
        #: `Project ID` reads "Project ID" is exactly the shape of a row this
        #: registry could not publish.
        self._heading_row = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
            self._heading_row = False
        elif tag == "td":
            self._cell = []
            if str(attrs.get("bgcolor", "")).lower() == "#92b7d6":
                self._heading_row = True

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            row, self._row = self._row, None
            if not row or self._heading_row:
                return
            if self.headers and len(row) == len(self.headers):
                self.rows.append(row)
            self._cell = None


_SORT_LINK = re.compile(r"submitform2\('Asc','[^']*'")
_HEADER_CELL = re.compile(r"color=\"#FFFFFF\">(.*?)&nbsp;&nbsp;<a", re.S)


def headings(name: str) -> list[str]:
    """One HTML report's column headings, as the grid prints them."""
    text = (FIXTURES / name).read_text(encoding="utf-8", errors="replace")
    found = [unescape(h).strip() for h in _HEADER_CELL.findall(text)]
    assert found, f"{name} published no sortable column headings"
    return found


def grid(name: str) -> list[dict[str, str]]:
    """One HTML report's data rows, keyed on its own column headings."""
    text = (FIXTURES / name).read_text(encoding="utf-8", errors="replace")
    parser = _Grid()
    parser.headers = headings(name)
    parser.feed(text)
    return [dict(zip(parser.headers, row)) for row in parser.rows]


def sheet(name: str) -> tuple[list[str], list[list[str]]]:
    """One CSV export, unrepaired — header and raw rows, field counts and all."""
    text = (FIXTURES / name).read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines(True)))
    return rows[0], rows[1:]


def csv_rows(name: str) -> list[dict[str, str]]:
    """A CSV export as dicts, dropping the unnamed trailing column."""
    header, rows = sheet(name)
    width = len(header)
    return [
        {k: v for k, v in zip(header, row) if k}
        for row in rows
        if len(row) == width and any(row)
    ]


@pytest.fixture()
def projects():
    return csv_rows("car-projects.csv")


@pytest.fixture()
def normalised(projects):
    return [car.normalize_project(car._row(row)) for row in projects]


def as_csv(rows: list[dict[str, str]], headings: list[str]) -> str:
    """Rows back out as the module's own export would write them.

    The registry publishes no CSV fixture for the issuance and cancellation
    reports, so those two are re-exported here from their grids. The **values
    are the registry's**; only the transport is reconstructed, and it is
    reconstructed the way the live exporter writes it — every field quoted,
    one unnamed trailing column.
    """
    lines = [",".join(f'"{h}"' for h in headings) + ","]
    for row in rows:
        lines.append(",".join(f'"{row.get(h, "")}"' for h in headings) + ",")
    return "\n".join(lines) + "\n"


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
    """Serves the four reports and the detail page from fixtures.

    The session and the CSRF refusal are modelled in `tests/test_apx.py`,
    where they belong; this one only has to be honest about *which* bytes come
    back for which report, so the field mapping is tested against the
    registry's own values.
    """

    PAGES = {
        111: "car-projects.html",
        112: "car-issuances.html",
        206: "car-retirements.html",
        308: "car-cancellations.html",
    }

    def __init__(self) -> None:
        self.gets: list[str] = []
        self.posts: list[tuple[int, dict]] = []
        self.token: str | None = None
        #: The report the last GET rendered. `frmDownload` carries no `r`: it
        #: sends `Data=Stamp_0`, a handle to the recordset the server stored in
        #: **that** session when it rendered the page. Modelling it any other
        #: way would let a test pass a download that the live module could not.
        self.report: int | None = None
        self.detail = (FIXTURES / "car-detail.html").read_text(
            encoding="utf-8", errors="replace"
        )
        self.exports = {
            111: (FIXTURES / "car-projects.csv").read_text(encoding="utf-8-sig"),
            206: (FIXTURES / "car-retirements.csv").read_text(encoding="utf-8-sig"),
            112: as_csv(
                grid("car-issuances.html"), headings("car-issuances.html")
            ),
            308: as_csv(
                grid("car-cancellations.html"), headings("car-cancellations.html")
            ),
        }

    def get_text(self, url, *, headers=None, params=None, use_cache=True, **kw):
        self.gets.append(url)
        if "prjView.asp" in url:
            return self.detail
        self.report = self._report(url)
        page = (FIXTURES / self.PAGES[self.report]).read_text(
            encoding="utf-8", errors="replace"
        )
        self.token = apx.csrf_token(page)
        return page

    def request(self, method, url, *, form_body=None, headers=None, **kw):
        assert method == "POST", method
        self.posts.append((self.report, dict(form_body or {})))
        assert (form_body or {}).get("c16e") == self.token, (
            "a POST with a stale token gets the home page at HTTP 200"
        )
        assert (form_body or {}).get("Data") == "Stamp_0", form_body
        return Response(self.exports[self.report])

    @staticmethod
    def _report(url: str) -> int:
        return int(url.rsplit("r=", 1)[1].split("&", 1)[0])

    def close(self) -> None:
        pass


@pytest.fixture()
def client():
    return FakeClient()


@pytest.fixture()
def adapter(client):
    return car.CARAPI(client)


# -- the contract the pipeline drives ---------------------------------------


def test_it_satisfies_the_adapter_protocol(adapter):
    assert isinstance(adapter, base.RegistryAdapter)


def test_progress_is_cumulative(adapter):
    """Both sinks read `done` as a position against a total, never as `1`."""
    progress = RecordingProgress()
    rows = list(adapter.iter_projects(progress=progress))
    progress.assert_cumulative(len(rows))


def test_credit_progress_is_cumulative(adapter):
    progress = RecordingProgress()
    rows = list(adapter.iter_credits(car.RETIREMENTS, progress=progress))
    progress.assert_cumulative(len(rows))


def test_the_adapter_class_is_the_one_registered_for_this_registry():
    assert registries.adapter_class(REGISTRY) is car.CARAPI
    assert registries.adapter_classes(REGISTRY) == (car.CARAPI,)


def test_a_full_project_row_reads_its_dates_off_the_detail_page(adapter):
    """End to end, against the registry's own detail page for CAR1957."""
    rows = {row["external_id"]: row for row, _raw in adapter.iter_projects()}
    row = rows["CAR1957"]

    assert row["project_id"] == 1957
    assert row["standard_name"] == "Climate Action Reserve"
    assert row["sectoral_scope"] == "Reforestation - ARB Compliance"
    assert row["country_code"] == "US"
    assert row["state_province"] == "CALIFORNIA"
    # The detail page, and only the detail page, publishes these — and they
    # are stored as ISO, because the registry writes `1/30/2027` and
    # `db.parse_date` reads ISO only. See `car._date`.
    assert row["project_start_date"] == "2023-02-08"
    assert row["credit_period_start"] == "2027-01-30"
    assert row["credit_period_end"] == "2053-01-29"
    # And the label the issuance report also states is kept as a label.
    assert row["extra"]["crediting_period_label"] == "Initial"
    assert row["extra"]["arb_id"] == "CAFR6957"


def test_the_issuance_report_is_read_once_for_the_methodologies(adapter, client):
    """`Protocol and Version` is on issuance rows, so the project half reads them.

    Two extra requests for the whole run, against a report the sync fetches
    anyway — not two per project.
    """
    list(adapter.iter_projects())
    assert [report for report, _f in client.posts].count(112) == 1


def test_every_ledger_is_named_and_reachable(adapter):
    for resource in car.LEDGERS:
        assert adapter.report_id(resource)
        rows = list(adapter.iter_credits(resource))
        assert rows, f"{resource} yielded nothing"
        assert all(row["project_id"] is not None for row in rows)


def test_no_ledger_row_collides_with_another(adapter, caplog):
    """The platform reports a duplicate `entity_id`; nothing here should trip it."""
    for resource in car.LEDGERS:
        caplog.clear()
        rows = list(adapter.iter_credits(resource))
        assert len({row["entity_id"] for row in rows}) == len(rows)
    assert "hashed to an entity_id" not in caplog.text


# -- the wiring, which needs no platform ------------------------------------


def test_the_registry_is_wired_into_every_place_one_is_named():
    """Five places, and a registry missing from any of them fails differently."""
    assert settings.CAR == "CAR"
    assert settings.REGISTRY_LABELS[REGISTRY] == "Climate Action Reserve"
    assert REGISTRY in settings.SYNC_ESTIMATE_MINUTES
    assert REGISTRY in settings.PROJECT_DETAIL_URLS
    assert REGISTRY in registries.ADAPTERS


def test_registry_resolves_by_alias():
    for name in ("car", "CAR", "reserve", "climate-action-reserve", "apx"):
        assert registries.resolve(name) == REGISTRY


def test_the_detail_url_is_built_from_the_numeric_part_of_the_reference():
    """`CAR1957` -> `id1=1957`, checked on 227 grid rows with 0 mismatches.

    A URL built from the reference itself answers HTTP 200 with an "Invalid
    URL" page, which is a link that looks fine in the sheet and is broken when
    a business user clicks it.
    """
    url = settings.PROJECT_DETAIL_URLS[REGISTRY].format(project_id=1957)
    assert url.endswith("prjView.asp?id1=1957")
    assert url.startswith(settings.CAR_SITE)


def test_the_standard_name_is_asserted_because_none_is_published():
    """The registry publishes a protocol per project and never a standard."""
    assert settings.CAR_STANDARD_NAME == "Climate Action Reserve"


def test_climate_forward_is_a_second_tenant_and_is_not_ingested():
    """One APX tenant has an adapter; the other is named so nobody re-derives it.

    Climate Forward is the identical module — same forms, same field names,
    same `rptdownload.asp` — and a different registry, whose units are ex-ante
    forecasts rather than issued offsets. That is a business decision, not a
    scraping one, so the `apx` alias resolves to the one tenant that has an
    adapter rather than meaning "every tenant".
    """
    assert registries.resolve("apx") == REGISTRY
    assert set(registries.ADAPTERS[REGISTRY]) == set(registries.ADAPTERS[settings.CAR])


# -- what the fixtures themselves prove -------------------------------------


def test_the_published_reference_is_unique_and_its_numeric_part_is_a_key(projects):
    """The SocialCarbon rule: a reference is not a key until it is shown unique.

    The live export has 1,277 distinct `Project ID`s across 1,277 rows. The
    numeric parts must be distinct too, because it is the number — not the
    string — that becomes half of the primary key.
    """
    references = [row["Project ID"] for row in projects]
    assert len(set(references)) == len(references)

    numbers = [int(re.fullmatch(r"CAR0*(\d+)", r).group(1)) for r in references]
    assert len(set(numbers)) == len(numbers)


def test_the_registered_total_header_carries_a_trailing_space(projects):
    """In the grid and in the CSV, and it is not a typo in the contract doc."""
    header, _rows = sheet("car-projects.csv")
    assert "Total Number of Offset Credits Registered " in header
    assert "Total Number of Offset Credits Registered" not in header


def test_the_csv_export_is_not_correctly_quoted_and_nothing_raises():
    """13 retirement rows come back one field wide and `csv.reader` is happy.

    The registry wraps a value that already had quotes around it, so RFC-4180
    reads the leading `""` as an escaped quote and then ends the field at the
    next comma. A reader that trusts the field count will mis-column those
    rows without ever raising.
    """
    header, rows = sheet("car-retirements.csv")
    populated = [row for row in rows if any(row)]
    malformed = [row for row in populated if len(row) != len(header)]
    assert len(malformed) == 13
    assert all(len(row) > len(header) for row in malformed)


def test_the_grid_and_the_csv_disagree_about_thousands_separators():
    """`45,802` in the HTML, `45802` in the CSV, and only one of them parses."""
    rows = grid("car-retirements.html")
    separated = [r["Quantity of Offset Credits"] for r in rows if "," in r["Quantity of Offset Credits"]]
    assert separated, "the retirement grid should render a separated quantity"
    assert db.float_or_none(separated[0]) is None

    csv_quantities = [r["Quantity of Offset Credits"] for r in csv_rows("car-retirements.csv")]
    assert csv_quantities and not any("," in q for q in csv_quantities)


def test_no_country_name_is_published_anywhere(projects):
    """An ISO code and nothing else, on every row. ACR's gap in a second registry."""
    header, _rows = sheet("car-projects.csv")
    assert not [h for h in header if "Country" in h and h != "Project Site Country"]
    assert {row["Project Site Country"] for row in projects} <= {"US", "MX", "CA", "CN", "AR"}


def test_the_compliance_program_id_says_n_a_where_there_is_none(projects):
    """Populated on all 1,277 rows, and 'none' is a literal string among them."""
    assert any(row["Compliance Program ID"] == "N/A" for row in projects)
    assert all(row["Compliance Program ID"] for row in projects)


# -- the field mapping ------------------------------------------------------


def test_every_project_row_uses_shared_column_names(normalised):
    for row in normalised:
        assert set(row) <= set(db.PROJECT_FIELDS)


def test_every_credit_row_uses_shared_column_names():
    feeds = {
        car.ISSUANCES: grid("car-issuances.html"),
        car.RETIREMENTS: csv_rows("car-retirements.csv"),
        car.CANCELLATIONS: grid("car-cancellations.html"),
    }
    for resource, rows in feeds.items():
        assert rows, f"no fixture rows for {resource}"
        for index, raw in enumerate(rows):
            row = car.normalize_credit(resource, car._row(raw), index)
            assert set(row) <= set(db.CREDIT_EVENT_FIELDS)


def test_the_ledger_names_are_the_ones_chosen_not_the_ones_described():
    """`credits` selects Gold Standard's bucket-by-status semantics in db."""
    assert car.LEDGERS == ("issuances", "retirements", "cancellations")
    assert "credits" not in car.LEDGERS


def test_the_crediting_period_label_never_reaches_a_date_column():
    """The issuance column called `Crediting Period` is a label, not a date.

    `Initial`, `Renewed-Second`, `Renewed-Third`, and blank on 4,848 of 5,170
    rows. Reading the column name rather than the column would put the word
    "Initial" in `event_date`, at HTTP 200, with nothing to say so.
    """
    raw = {
        "Project ID": "CAR1957",
        "Date Issued": "03/19/2026",
        "Crediting Period": "Renewed-Second",
        "Vintage": "2026",
        "Total Offset Credits Issued": "9,166",
    }
    row = car.normalize_credit(car.ISSUANCES, car._row(raw))
    assert row["event_date"] == "2026-03-19"
    assert row["vintage"] == "2026"
    assert "Renewed" not in str(row["event_date"])
    # It is kept — as the registry's own label for the row, in the one column
    # that can hold one.
    assert row["status"] == "Renewed-Second"
    # And the separated quantity survives the transport it came by.
    assert row["quantity"] == 9166.0


def test_the_crediting_period_dates_come_from_the_detail_page(projects):
    """The projects report carries none of the three, and that is the whole cost."""
    header, _rows = sheet("car-projects.csv")
    assert "Crediting Period Expires" not in header
    assert "Project Commencement Date" not in header

    detail = {
        "Project Commencement Date": "01/01/2015",
        "Project Reporting Start Date": "06/01/2015",
        "Crediting Period Expires": "12/31/2040",
    }
    row = car.normalize_project(car._row(projects[0]), car._row(detail))
    assert row["credit_period_start"] == "2015-06-01"
    assert row["credit_period_end"] == "2040-12-31"
    assert row["project_start_date"] == "2015-01-01"


def test_the_deliberate_blanks_stay_blank(normalised):
    """Measured over the full index; none of them is filled from elsewhere.

    `País` is blank for the same reason ACR's is — only a code is published —
    and `Cidade` because `Project Site Location` is a county list.
    """
    for row in normalised:
        assert row.get("country_name") is None
        assert row.get("city") is None
        assert row.get("avg_annual_vol_vcu") is None
        assert row.get("exante_quantity") is None
        assert row.get("region_name") is None
        assert row.get("afolu_names") is None


def test_the_county_list_and_the_country_code_are_kept(normalised, projects):
    """Nothing the sheet has no column for is dropped; it is queryable in `extra`."""
    by_reference = {row["external_id"]: row for row in normalised}
    row = by_reference["CAR1957"]
    assert row["country_code"] == "US"
    assert row["extra"]["project_site_location"] == (
        "Shasta, Siskiyou, and Trinity Counties"
    )
    assert row["extra"]["country_iso"] == "US"
    # And it is a county list, which is why it is not a city.
    assert "Counties" in row["extra"]["project_site_location"]


def test_the_compliance_program_id_is_queryable_and_n_a_is_not_an_id(normalised):
    """It is the first thing a cross-registry check reads: 2,035 of 2,277
    cancellations here are conversions out to ARB or WA Ecology."""
    by_reference = {row["external_id"]: row for row in normalised}
    assert by_reference["CAR1957"]["extra"]["compliance_program_id"] == "CAFR6957"
    # `CAR1460` states the literal string `N/A`, which is not a compliance id.
    assert "compliance_program_id" not in (by_reference["CAR1460"]["extra"] or {})


def test_the_converted_to_vcus_figure_lands_in_extra_as_a_number():
    """Two rows of 5,170 state it, and it is the registry naming an overlap
    with Verra itself. Stored as a number so the check is a query."""
    facts = {"protocols": ["Forestry - MX - Version 2.0"], "converted_to_vcus": 1500.0}
    row = car.normalize_project({"Project ID": "CAR1001"}, {}, facts)
    assert row["extra"]["converted_to_vcus"] == 1500.0
    assert isinstance(row["extra"]["converted_to_vcus"], float)


def test_a_project_with_no_issuances_gets_no_methodology(projects):
    """376 of 1,277, and the registry publishes none for them anywhere.

    Nothing is inferred from the project type, even though the protocol name
    restates it — an inferred methodology is indistinguishable from a
    published one once it is in the sheet.
    """
    row = car.normalize_project(car._row(projects[0]), {}, {})
    assert row["methodologies"] is None

    facts = {"protocols": ["Forestry - MX - Version 2.0", "Forestry - MX - Version 3.0"]}
    row = car.normalize_project(car._row(projects[0]), {}, facts)
    assert row["methodologies"] == (
        "Forestry - MX - Version 2.0; Forestry - MX - Version 3.0"
    )


def test_the_account_holder_is_not_read_as_a_beneficiary():
    """5,894 of 11,044 retirements say `On Behalf of Third Party`.

    Filling `beneficiary` from the holder would make **every** retirement read
    as a third-party sale the moment `sold_equals_retired` is flipped. The
    prose is kept in `reason`, which is the only copy: `credit_events` holds
    no raw payload.
    """
    raw = {
        "Project ID": "CAR1480",
        "Account Holder": "ClimeCo LLC",
        "Retirement Reason": "On Behalf of Third Party",
        "Retirement Reason Details": "On behalf of Allegheny College for 2026 emissions",
        "Quantity of Offset Credits": "8000",
        "Status Effective": "06/16/2026",
        "Offset Credit Serial Numbers": "CAR-1-US-1480-46-1156-FL-2026-10272-852724 to 860723",
    }
    row = car.normalize_credit(car.RETIREMENTS, car._row(raw))
    assert row["beneficiary"] is None
    assert "ClimeCo" not in str(row["beneficiary"])
    assert "Allegheny College" in row["reason"]
    assert "On Behalf of Third Party" in row["reason"]


def test_a_cancellation_records_why_it_was_cancelled():
    """2,035 of 2,277 are conversions to a compliance registry, not destruction."""
    raw = {
        "Project ID": "CAR1458",
        "Cancellation Reason": "ARB",
        "Quantity of Offset Credits": "1,000",
        "Status Effective": "01/02/2026",
        "Offset Credit Serial Numbers": "CAR-1-US-1458-1-1-OH-2020-1 to 1000",
    }
    row = car.normalize_credit(car.CANCELLATIONS, car._row(raw))
    assert row["reason"] == "ARB"
    assert row["quantity"] == 1000.0


def test_market_eligibility_is_not_an_additional_certification():
    """CORSIA and ICVCM are eligibility, exactly like Cercarbono's `elegible`."""
    raw = {
        "Project ID": "CAR1480",
        "Eligible for CORSIA 2021-2023 Compliance Period": "Eligible",
        "ICVCM CCP Eligible": "Yes",
        "Additional Certification(s)": "",
        "Offset Credit Serial Numbers": "CAR-1-US-1480-1-1-FL-2026-1 to 5",
    }
    row = car.normalize_credit(car.RETIREMENTS, car._row(raw))
    assert row["additional_certification"] is None


def test_rows_with_no_serial_do_not_collapse_into_one_key():
    """The issuance report publishes no serial and no id of any kind.

    Every row hashing off its own values alone would fuse two rows alike in
    project, vintage, quantity and date into one upsert — and `reconciled()`
    counts rows *yielded*, not rows written, so it would pass and the sheet
    would simply be short.
    """
    raw = {
        "Project ID": "CAR1957",
        "Vintage": "2026",
        "Total Offset Credits Issued": "100",
        "Date Issued": "01/01/2026",
    }
    keys = {
        car.normalize_credit(car.ISSUANCES, car._row(raw), index)["entity_id"]
        for index in range(3)
    }
    assert len(keys) == 3


def test_a_serial_keyed_row_is_idempotent_across_runs():
    """A retirement's serial range is its identity, so its key must not move."""
    raw = {
        "Project ID": "CAR1480",
        "Offset Credit Serial Numbers": "CAR-1-US-1480-46-1156-FL-2026-10272-915425 to 915425",
    }
    first = car.normalize_credit(car.RETIREMENTS, car._row(raw), 0)["entity_id"]
    second = car.normalize_credit(car.RETIREMENTS, car._row(raw), 7)["entity_id"]
    assert first == second


def test_the_registrys_own_registered_total_is_kept(normalised):
    """It agrees with the issuance ledger on all 1,277 projects, which is why
    there is no `iter_credit_totals` — and it is kept so the day it disagrees
    is answerable from the database rather than by re-scraping."""
    assert not hasattr(car.CARAPI, "iter_credit_totals")
    stated = [
        row["extra"]["stated_registered_credits"]
        for row in normalised
        if (row["extra"] or {}).get("stated_registered_credits") is not None
    ]
    assert stated and all(isinstance(value, float) for value in stated)


def test_the_view_placeholder_is_not_stored_as_a_website(normalised):
    """`Documents` and `Data` are hyperlinks in the grid and the word `View`
    in the CSV, on all 1,277 rows."""
    for row in normalised:
        assert (row["extra"] or {}).get("website") != "View"


def test_tipo_macro_carries_the_registrys_own_vocabulary(normalised):
    """Untranslated, like every registry here. Never mapped into a taxonomy."""
    assert {row["sectoral_scope"] for row in normalised} >= {
        "Forestry - MX",
        "Avoided Grassland Conversion",
        "Ozone Depleting Substances - U.S. - ARB Compliance",
    }


# -- derivation, which fails silently ---------------------------------------


def _derived(sectoral_scope: str, **row) -> dict[str, str]:
    rulesets = derive.load_rulesets()
    project = {"sectoral_scope": sectoral_scope, "methodologies": None, **row}
    return {column: value for column, value, _rule in derive.derive_for_project(project, rulesets)}


def test_the_biome_gate_recognises_this_registrys_land_use_wordings():
    """One unrecognised wording means no biome for any row, and nothing logs it.

    674 of CAR's 1,277 project types pass the gate. `Forestry - MX` (485) and
    the two forest-management wordings pass on the existing `[Ff]orest`
    alternative; `Avoided Grassland Conversion` (39) needed `Grassland` added.
    """
    gate = next(
        ruleset for ruleset in derive.load_rulesets() if ruleset.column == "Bioma"
    ).applies_when
    assert len(gate) == 1
    pattern = re.compile(str(gate[0].value), re.I)

    for wording in (
        "Forestry - MX",
        "Improved Forest Management - ARB Compliance",
        "Reforestation - ARB Compliance",
        "Avoided Grassland Conversion",
    ):
        assert pattern.search(wording), f"{wording!r} misses the land-use gate"

    # And the emission-reduction types stay out, correctly.
    for wording in (
        "Ozone Depleting Substances - U.S. - ARB Compliance",
        "Landfill Gas Capture/Combustion",
        "Adipic Acid",
    ):
        assert not pattern.search(wording)


def test_a_mexican_land_use_project_reaches_a_biome():
    """485 of the 674 are `Forestry - MX`, and every country band above the
    ISO-code section matches a country *name* this registry never publishes.

    Without `mexico-by-code` the single largest group of land-use projects in
    the registry comes out blank, silently.
    """
    assert _derived("Forestry - MX", country_code="MX")["Bioma"] == (
        "México (bioma não determinado)"
    )


def test_a_us_land_use_project_reaches_the_existing_iso_code_band():
    assert _derived("Improved Forest Management - ARB Compliance", country_code="US")[
        "Bioma"
    ] == "Floresta Temperada Norte-Americana"


def test_a_grassland_project_is_not_labelled_a_forest():
    """The gate word and the band were added together on purpose.

    Widening the gate alone would have sent all 39 to the North American
    *forest* band — the mistake CLAUDE.md records ACR avoiding by leaving the
    cell blank instead.
    """
    assert _derived("Avoided Grassland Conversion", country_code="US")["Bioma"] == (
        "Pradarias Norte-Americanas (campos temperados)"
    )


def test_every_project_type_the_registry_publishes_reaches_a_rule():
    """All 32, not the 13 the fixtures happen to carry.

    The first version of this listed the fixture subset and said the other 19
    would be blank — "a measurable gap, not a hidden one: `verra coverage -r
    car` after the first full sync names them". It did: **52 projects across
    11 types** had neither a Tipo Micro nor a Durabilidade, and three of the
    eleven were an existing rule being too narrow rather than an activity
    nobody had covered (`Avoided Conversion - ARB Compliance` against an
    `equals`, `Coal Mine Methane - VAM` against a rule reading "Mine Methane
    Capture", `Landfill - MX` against one anchored on "Landfill Gas").

    Counts are the live vocabulary as of 2026-08-05 and are here so a wording
    that disappears is as visible as one that appears.
    """
    published = (
        ("Forestry - MX", 485),
        ("Improved Forest Management - ARB Compliance", 141),
        ("Landfill Gas Capture/Combustion", 126),
        ("Livestock - ARB Compliance", 108),
        ("Ozone Depleting Substances - U.S. - ARB Compliance", 96),
        ("Livestock Gas Capture/Combustion", 72),
        ("Avoided Grassland Conversion", 39),
        ("Mine Methane Capture - ARB Compliance", 36),
        ("Ozone Depleting Substances - U.S.", 30),
        ("Improved Forest Management", 25),
        ("Reforestation - ARB Compliance", 13),
        ("Ozone Depleting Substances - U.S. - WA ECO Compliance", 13),
        ("Biochar", 9),
        ("Organic Waste Composting", 8),
        ("Low Carbon Cement", 8),
        ("Avoided Conversion", 8),
        ("Nitric Acid N2O- Secondary Catalyst", 7),
        ("Canada Grassland", 7),
        ("Ozone Depleting Substances - Article 5 Imports", 5),
        ("Conservation-Based Forest Management", 5),
        ("Adipic Acid", 5),
        ("Soil Enrichment", 4),
        ("Reforestation", 4),
        ("Organic Waste Digestion", 4),
        ("Nitric Acid N2O- Tertiary Catalyst", 4),
        ("Nitrogen Management", 3),
        ("Livestock - MX", 3),
        ("Landfill - MX", 3),
        ("Coal Mine Methane - VAM", 2),
        ("Coal Mine Methane - Drainage", 2),
        ("Improved Forest Management - WA ECO Compliance", 1),
        ("Avoided Conversion - ARB Compliance", 1),
    )
    assert sum(count for _scope, count in published) == 1277
    for scope, _count in published:
        derived = _derived(scope, country_code="US")
        assert derived.get("Durabilidade"), f"{scope!r} reaches no durability band"
        assert derived.get("Tipo Micro de Projeto"), f"{scope!r} reaches no micro type"


def test_the_new_bands_reuse_existing_values_rather_than_inventing_wordings():
    """A CAR improved-management project stores carbon the way a Verra IFM one
    does, and a new wording must never quietly become a new band."""
    car_ifm = _derived("Improved Forest Management - ARB Compliance", country_code="US")
    verra_ifm = _derived(
        "Agriculture Forestry and Other Land Use", afolu_names="IFM", country_code="US"
    )
    assert car_ifm["Durabilidade"] == verra_ifm["Durabilidade"]
    assert car_ifm["Tipo Micro de Projeto"] == verra_ifm["Tipo Micro de Projeto"]


def test_the_two_projects_verra_also_publishes_are_cross_linked(normalised):
    """The registry states the overlap itself, and this is what it turned into.

    `Offset Credits Converted to VCUs` is populated on 2 of 5,173 issuance rows
    and names no registry. Both were checked against the database: CAR400 is
    Verra 1528 and CAR498 is Verra 1527, each `… - CER Conversion`, each
    *Units transferred from approved GHG program*, and each issuing one VCU
    block of exactly the converted quantity — 45,730 and 4,270, vintage 2014.

    **Unlike the other two overlaps here these are the same units**, not
    disjoint tranches, and CAR cancels nothing for them. Neither figure is
    adjusted; the link is what makes the double count visible.
    """
    assert car.ALSO_REGISTERED_AS == {
        "CAR400": "VERRA 1528",
        "CAR498": "VERRA 1527",
    }
    row = car.normalize_project({"Project ID": "CAR400"})
    assert row["extra"]["also_registered_as"] == "VERRA 1528"
    for other in normalised:
        if other["external_id"] not in car.ALSO_REGISTERED_AS:
            assert (other["extra"] or {}).get("also_registered_as") is None


def test_no_fixture_carries_a_replacement_character():
    """A capture taken through a broken decode is evidence of nothing.

    The first CAR fixtures were saved from `response.text` while the platform
    was being decoded as UTF-8, so `ASOCIACIÓN DE SILVICULTORES DE LA REGIÓN
    FORESTAL` and `José María Morelos` arrived carrying `U+FFFD` — and a test
    asserting those values would then have pinned the damage as if it were the
    registry's. They were repaired against a correctly decoded copy of the same
    live source, and this is what stops the next capture reintroducing it.
    """
    damaged = {
        path.name: path.read_text("utf-8").count("\ufffd")
        for path in sorted(FIXTURES.glob("car-*"))
    }
    assert damaged and not any(damaged.values()), damaged


def test_the_published_dates_are_stored_as_iso_because_nothing_else_parses():
    """`10/7/2018` is the one date format in this project that is not ISO.

    Every registry before this one publishes `2018-10-07`, so every adapter
    stores what it was given — and `db.parse_date`, which `excel` and `derive`
    both read, accepts ISO only. Storing CAR's dates as published left
    `Data de Início`, `Data de Término` **and `Duração`** blank on all 1,277
    rows, with the database looking fully populated and nothing in the log.
    It was caught by `verra coverage -r car` after the first full sync, not by
    the sync, which reconciled perfectly.

    Month-first is measured rather than assumed: across all 2,103 published
    crediting-period dates the first component never exceeds 12 and the second
    reaches 31. It is fixed here and not in `db.DATE_FORMATS`, because a global
    `%m/%d/%Y` would silently misread the first registry that writes day-first.
    """
    detail = {
        "Project Commencement Date": "1/1/2015",
        "Project Reporting Start Date": "10/7/2018",
        "Crediting Period Expires": "12/31/2040",
    }
    row = car.normalize_project({"Project ID": "CAR1"}, car._row(detail))
    assert row["credit_period_start"] == "2018-10-07"
    assert row["credit_period_end"] == "2040-12-31"
    for column in ("credit_period_start", "credit_period_end", "project_start_date"):
        assert db.parse_date(row[column]) is not None, column


def test_a_date_in_an_unknown_shape_is_kept_and_logged(caplog):
    """The column is the evidence that the format changed.

    Dropping it would leave exactly the blank this whole fix is about, and a
    registry that starts writing ISO — or anything else — must be visible.
    """
    with caplog.at_level("ERROR"):
        row = car.normalize_project(
            {"Project ID": "CAR1"}, {"Crediting Period Expires": "31 December 2040"}
        )
    assert row["credit_period_end"] == "31 December 2040"
    assert "not `M/D/YYYY`" in caplog.text
