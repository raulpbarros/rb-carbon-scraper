"""Reading data out of a Next.js App Router page.

Puro's registry is a React Server Components app. The server renders a route
and streams the RSC ("flight") payload to the browser inside the HTML, split
across any number of

    <script>self.__next_f.push([1,"<a JSON string>"])</script>

calls. Concatenating those strings rebuilds one text stream, and the project
and transaction lists sit inside it as ordinary JSON objects. The 5.2 MB
retirement page splits its stream across dozens of pushes and the split lands
mid-object, so **every push has to be joined before anything is decoded** —
scanning the pushes one at a time finds a truncated object and gives up.

This module is the Puro equivalent of `markit/tables.py`: the part that knows
about the delivery format, kept away from the part that knows what the fields
mean. Standard library only — the payload is JSON, not markup, so no parser is
needed beyond `json`.

Nothing here is Puro-specific in principle. It is not shared yet because one
registry uses it, and a second one would be the moment to find out which parts
are really general.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The opening of one streamed chunk. The string that follows is decoded with
#: `raw_decode` rather than matched with a regex: a chunk contains escaped
#: quotes by the thousand, and a non-greedy `".*?"` gets the boundary wrong the
#: first time a payload happens to contain `"])`.
_PUSH = re.compile(r"self\.__next_f\.push\(\[1,")

#: The object a list route hands its table. Matched with whitespace allowed
#: rather than as the literal `{"data":[` the build emits today: the payload is
#: minified JSON, and a build that stops minifying it would otherwise read as a
#: registry with no projects.
_DATA = re.compile(r'\{\s*"data"\s*:\s*\[')

#: Where a country name is rendered: `["<flag>", " ", "<name>"]`. The flag is
#: built from the ISO code, so the pair can be *checked* rather than trusted —
#: see `flagged_country`.
_REGIONAL_INDICATOR_A = 0x1F1E6

_DECODER = json.JSONDecoder()


def payload(html: str) -> str:
    """The RSC stream rebuilt from every `__next_f.push` in `html`."""
    chunks: list[str] = []
    for match in _PUSH.finditer(html):
        try:
            text, _ = _DECODER.raw_decode(html, match.end())
        except ValueError:
            # A push whose argument is not a string — `[0]` opens the stream
            # and carries no data. Skipping is right; raising would make the
            # page's own preamble an error.
            continue
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def data_list(flight: str) -> list[dict[str, Any]]:
    """The `{"data": [...]}` array a list route passes to its table.

    One array per page and the whole resource in it — there is no paging on
    any of the three list routes, so a short read here is a broken response
    rather than a page boundary. Hence the raise: `data` missing means the
    page changed shape, and returning `[]` would report that as an empty
    registry.
    """
    match = _DATA.search(flight)
    if match is None:
        raise ValueError("no data array in the page payload")
    obj, _ = _DECODER.raw_decode(flight, match.start())
    rows = obj.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"data is not a list: {type(rows).__name__}")
    return [row for row in rows if isinstance(row, dict)]


def labelled_number(flight: str, label: str) -> float | None:
    """A `{"label": <label>, "value": <n>}` stat block, if the page has one.

    Decoded from the enclosing object rather than pattern-matched off the
    number, so a reordered or extra key does not silently return the wrong
    field. `None` when the page does not carry it — which callers must treat
    as "not published here", never as zero.
    """
    needle = '"label":' + json.dumps(label)
    for match in re.finditer(re.escape(needle), flight):
        start = flight.rfind("{", 0, match.start())
        if start < 0:
            continue
        try:
            obj, _ = _DECODER.raw_decode(flight, start)
        except ValueError:
            continue
        value = obj.get("value") if isinstance(obj, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def flag_for(country_code: str | None) -> str | None:
    """The regional-indicator pair a two-letter ISO code renders as."""
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    return "".join(chr(_REGIONAL_INDICATOR_A + ord(letter) - ord("A")) for letter in code)


def flagged_country(flight: str, country_code: str | None) -> str | None:
    """The country name rendered beside `country_code`'s own flag.

    The list route publishes an ISO code and no name at all, and the detail
    page renders the name twice — in the header and under "Location" — each
    time as `[flag, " ", name]`. Requiring the flag to be the one the project's
    own ISO code builds turns a positional read of someone's markup into a
    checked one: a name lifted from the wrong element cannot be sitting next to
    the right flag.

    `None` unless exactly one name is found. Two different names beside one
    flag means the page is not shaped the way this assumes any more, and a
    coin-flip between them would be worse than a blank cell.
    """
    flag = flag_for(country_code)
    if flag is None:
        return None

    names: set[str] = set()
    for match in re.finditer(re.escape(f'"{flag}"'), flight):
        start = flight.rfind("[", 0, match.start())
        if start < 0:
            continue
        try:
            rendered, _ = _DECODER.raw_decode(flight, start)
        except ValueError:
            continue
        if (
            isinstance(rendered, list)
            and len(rendered) == 3
            and rendered[0] == flag
            and isinstance(rendered[2], str)
            and rendered[2].strip()
        ):
            names.add(rendered[2].strip())
    return names.pop() if len(names) == 1 else None
