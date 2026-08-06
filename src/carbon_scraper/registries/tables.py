"""Read a server-rendered registry's HTML tables — shared by the adapters.

Heading-keyed and `rowspan`-aware, and nothing in it is specific to any one
platform, which is why it lives here beside `text.py` rather than inside the
adapter that happened to need it first. That was the legacy Markit public view
(`mer.markit.com/br-reg/public`), a server-rendered JSP application with no API
behind it — the first target here that had to be parsed rather than decoded.
The APX registry software is the second, and a cross-platform import out of
`markit/` would have been the wrong shape for a reader neither of them owns.

Built on `html.parser` from the standard library: a parser is not worth a
dependency in a bundle the business downloads, and this markup is simple
enough that one is not needed.

Two things here are load-bearing, and both are the same class of bug this
codebase keeps meeting — output that looks right and is not.

**Rows are read by column heading, never by position.** A registry that adds
or reorders a column would otherwise shift every value one place along and
produce a table full of plausible nonsense, which is exactly how `SELECT *`
across two databases broke the shipped seed. Heading-keyed rows simply lose
the column that moved, and the field ends up empty rather than wrong.

**`rowspan` carries down.** A project registered under two standards renders as
two `<tr>`s where the *second* omits the merged cells — no name, no developer,
no country — and every remaining cell in it slides left. Read positionally and
that row's Category ("Carbon") lands in the project-name column: a real
project id with a real-looking name, and nothing anywhere to say it is wrong.
This is where the first recon pass reported 35 rows for 33 projects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any  # noqa: F401 - used in the parser's stack annotation

#: Cells whose merged value spans more rows than this are treated as a markup
#: error rather than obeyed. The public view merges 2-3 rows for a
#: multi-standard project; a four-figure rowspan would poison a whole page.
MAX_ROWSPAN = 50


@dataclass
class Cell:
    text: str = ""
    links: list[str] = field(default_factory=list)
    rowspan: int = 1


@dataclass
class Row:
    """One data row, keyed by column heading."""

    values: dict[str, str]
    links: list[str]
    #: Links from this row's **own** cells, excluding any inherited through a
    #: `rowspan`. The difference is what tells a record apart from the tail of
    #: a merged one: a real row carries its own "View" link, a continuation row
    #: only inherits the link of the row above it.
    own_links: list[str] = field(default_factory=list)

    def get(self, *headings: str) -> str | None:
        """The first heading that carries a value.

        Several headings are accepted because the same idea is spelled
        differently per ledger — a retirement's quantity column is "Retirement
        Quantity" and an issuance's is "Units".
        """
        for heading in headings:
            value = self.values.get(heading)
            if value:
                return value
        return None


@dataclass
class Table:
    headings: list[str]
    rows: list[Row]


class _TableReader(HTMLParser):
    """Collect every table on the page, resolving merged cells."""

    def __init__(self) -> None:
        # convert_charrefs is on by default and wanted: the registry writes
        # `&amp;` and `S&atilde;o`, and we want the text a reader would see.
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        # A stack, not a depth counter. A nested table used to append its
        # `<th>`s to the enclosing table's headings and its `<tr>`s to its
        # rows, which shifts the heading -> index mapping for every later row
        # — plausible values under the wrong column names, silently. Every
        # fixture here happens to hold exactly one table, which is precisely
        # why the flattening was invisible.
        self._stack: list[tuple[list[str], list[Row], dict[int, Any], list[Cell]]] = []
        self._headings: list[str] = []
        self._rows: list[Row] = []
        # column index -> (text, links, rows still to carry)
        self._carry: dict[int, tuple[str, list[str], int]] = {}
        self._cells: list[Cell] = []
        self._cell: Cell | None = None
        self._is_heading = False

    @property
    def _depth(self) -> int:
        return len(self._stack)

    # -- structure ---------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        if tag == "table":
            self._start_table()
        elif tag == "tr" and self._depth:
            self._cells = []
        elif tag in ("td", "th") and self._depth:
            self._is_heading = tag == "th"
            self._cell = Cell(rowspan=_span(attributes.get("rowspan")))
        elif tag == "a" and self._cell is not None:
            href = attributes.get("href")
            if href:
                self._cell.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            if self._is_heading:
                self._headings.append(_squash(self._cell.text))
            else:
                self._cells.append(self._cell)
            self._cell = None
            self._is_heading = False
        elif tag == "tr" and self._depth:
            self._end_row()
        elif tag == "table" and self._depth:
            self._end_table()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text += data

    # -- rows --------------------------------------------------------------

    def _start_table(self) -> None:
        """Begin a table, suspending whatever one encloses it."""
        self._stack.append((self._headings, self._rows, self._carry, self._cells))
        self._headings = []
        self._rows = []
        self._carry = {}
        self._cells = []

    def _end_table(self) -> None:
        """Record this table and resume the one that encloses it.

        The enclosing table's own `<tr>` is still open, and its cells are
        restored untouched: a table nested inside a cell contributes its
        markup to that cell's text, never a row of its own to the parent.
        """
        self.tables.append(Table(list(self._headings), list(self._rows)))
        self._headings, self._rows, self._carry, self._cells = self._stack.pop()

    def _end_row(self) -> None:
        cells, self._cells = self._cells, []
        if not cells and not self._carry:
            return

        # Cells merged from an earlier row occupy their original column index
        # before any of this row's own cells are placed.
        placed: dict[int, Cell] = {}
        for index, (text, links, remaining) in list(self._carry.items()):
            placed[index] = Cell(text=text, links=list(links))
            if remaining - 1 > 0:
                self._carry[index] = (text, links, remaining - 1)
            else:
                del self._carry[index]

        column = 0
        for cell in cells:
            while column in placed:
                column += 1
            placed[column] = cell
            if cell.rowspan > 1:
                self._carry[column] = (cell.text, cell.links, cell.rowspan - 1)
            column += 1

        if not cells:
            # Nothing but carried cells: the source row was entirely merged
            # away, so there is no record here to yield.
            return

        own = {id(cell) for cell in cells}
        ordered = [placed[index] for index in sorted(placed)]
        values: dict[str, str] = {}
        links: list[str] = []
        own_links: list[str] = []
        for index, cell in enumerate(ordered):
            heading = (
                self._headings[index] if index < len(self._headings) else f"column{index}"
            )
            values[heading] = _squash(cell.text)
            links += cell.links
            if id(cell) in own:
                own_links += cell.links
        self._rows.append(Row(values=values, links=links, own_links=own_links))


def _span(value: str | None) -> int:
    try:
        span = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return span if 1 <= span <= MAX_ROWSPAN else 1


def _squash(text: str) -> str:
    return " ".join(text.split()).strip()


def parse_tables(html: str) -> list[Table]:
    reader = _TableReader()
    reader.feed(html)
    reader.close()
    return reader.tables


def results_table(html: str) -> Table:
    """The table carrying the records, out of however many the page renders.

    The public view wraps its results in a layout table or two, so the results
    table is chosen by shape — headings plus data rows — rather than by a CSS
    class that is not ours to rely on. A page with no results still renders its
    headings, and that is a real answer (no records) rather than a failure.
    """
    tables = parse_tables(html)
    with_rows = [t for t in tables if t.headings and t.rows]
    if with_rows:
        return max(with_rows, key=lambda t: len(t.rows))
    with_headings = [t for t in tables if t.headings]
    if with_headings:
        return max(with_headings, key=lambda t: len(t.headings))
    return Table(headings=[], rows=[])


def link_id(row: Row, *, parameter: str, own_only: bool = False) -> int | None:
    """The numeric id in a row's link, e.g. `project.jsp?project_id=123`.

    The listing states no id in any column; the only place a record's key
    appears is the href behind its "View" link, which is why links are kept
    alongside the values.

    `own_only` ignores a link inherited through a `rowspan`, which is how a
    real record is told from the tail of a merged one.
    """
    needle = f"{parameter}="
    for href in row.own_links if own_only else row.links:
        if needle not in href:
            continue
        raw = href.split(needle, 1)[1].split("&", 1)[0].split("#", 1)[0]
        digits = "".join(c for c in raw if c.isdigit())
        if digits:
            return int(digits)
    return None


def link_targets(row: Row, *, own_only: bool = False) -> list[str]:
    """The pages a row links to, filename only (`project.jsp`).

    Worth knowing because this view has two: a grouped project's rows link to
    `master-project.jsp`, and the id in that link is the *master's*, shared by
    every sub-project under it.
    """
    targets = []
    for href in row.own_links if own_only else row.links:
        name = href.split("?", 1)[0].rsplit("/", 1)[-1]
        if name:
            targets.append(name)
    return targets


def cell_dict(table: Table) -> dict[str, str]:
    """A label/value page (the project detail) read as one mapping.

    The detail page is not a record list: it renders its headings in one row
    and their values in the next. Pairing them by index is the whole of it,
    and a heading with no value simply does not appear.
    """
    if not table.rows:
        return {}
    values = table.rows[0].values
    # `Row.values` is already heading-keyed when the table has a heading row,
    # which is exactly the pairing wanted.
    return {k: v for k, v in values.items() if v}


__all__ = [
    "Cell",
    "Row",
    "Table",
    "cell_dict",
    "link_id",
    "link_targets",
    "parse_tables",
    "results_table",
]
