"""Small normalisation helpers shared by the adapters.

Every registry has its own vocabulary for "nothing here" — EcoRegistry says
`No definido`, the Markit view renders `--` — and those tables must stay per
registry. What must *not* stay per registry is the code that applies them: two
adapters had grown their own copies of the same three functions, so a fix to
one silently did not reach the other.
"""

from __future__ import annotations

import hashlib
from typing import Any, Container, Iterable


def stated(
    value: Any, not_stated: Container[str] = (), *, collapse: bool = True
) -> str | None:
    """Registry text, or None where the registry stated nothing.

    `collapse` folds internal whitespace runs as well as trimming, which is
    what HTML needs and what a JSON field does not want — a value with two
    deliberate spaces in it is still that value.
    """
    if value is None:
        return None
    text = " ".join(str(value).split()) if collapse else str(value).strip()
    if not text or text.casefold() in not_stated:
        return None
    return text


def joined(
    values: Iterable[Any] | None,
    not_stated: Container[str] = (),
    *,
    collapse: bool = True,
    separator: str = "; ",
) -> str | None:
    """Distinct values in first-seen order, joined.

    First-seen rather than sorted: a registry lists its primary entry first,
    and Cercarbono repeats a sector once per verification, so a single-sector
    project can list `Land use (AFOLU)` three times.
    """
    seen: list[str] = []
    for value in values or ():
        text = stated(value, not_stated, collapse=collapse)
        if text and text not in seen:
            seen.append(text)
    return separator.join(seen) or None


def hashed_id(*parts: Any) -> int:
    """A stable numeric key for a record the registry gives no id.

    `credit_events` is keyed on an integer, and what these registries publish
    is a serial string or nothing at all. Hashing rather than counting is what
    keeps the upsert idempotent: a positional key renumbers every row the
    moment the registry inserts one.

    Include whatever distinguishes the record — the registry name among them,
    so a hashed key cannot collide with a platform-issued `entityId` when one
    registry's ledgers hold rows from two systems.

    60 bits, which fits SQLite's signed INTEGER with room to spare.
    """
    seed = "|".join("" if part is None else str(part) for part in parts)
    return int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:15], 16)
