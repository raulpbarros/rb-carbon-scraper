"""The seams every adapter shares. No network.

Four adapters had grown their own copy of the client lifecycle and their own
version of the "count, iterate, report progress, reconcile" loop, and gave
four slightly different answers. These pin the shared ones.
"""

from __future__ import annotations

import pytest

from carbon_scraper import registries, settings
from carbon_scraper.registries import base
from carbon_scraper.registries.text import hashed_id, joined, stated


# -- who owns the HTTP client ----------------------------------------------


class _Client:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Adapter(base.ClientOwner):
    def __init__(self, client=None):
        self._bind_client(client)


def test_an_injected_client_is_not_closed_by_the_adapter():
    """It belongs to the caller — closing it shuts a pool still in use."""
    client = _Client()
    with _Adapter(client):
        pass
    assert client.closed is False


def test_a_client_the_adapter_made_is_closed_with_it(monkeypatch):
    made = _Client()
    monkeypatch.setattr(base, "RegistryClient", lambda **kwargs: made)
    with _Adapter():
        pass
    assert made.closed is True


def test_every_adapter_uses_the_shared_lifecycle():
    """A fifth copy of close/__enter__/__exit__ is a fifth place to get it
    wrong; the mixin is what makes that structural."""
    for name in registries.ALL:
        for cls in registries.adapter_classes(name):
            assert issubclass(cls, base.ClientOwner), f"{cls.__name__}"


def test_adapters_are_constructed_one_at_a_time(monkeypatch):
    """A tuple built every client up front.

    Plan Vivo has two adapters, so the second one's connection pool and SSL
    context sat open for the ~2.5 hours the first one ran, and leaked
    entirely if the first raised. The GUI does not exit between runs.
    """
    made: list[str] = []

    def _make(label):
        class _Counting(base.ClientOwner):
            def __init__(self, **kwargs):
                made.append(label)
                self._bind_client(_Client())

        return _Counting

    monkeypatch.setattr(
        registries, "adapter_classes", lambda name: (_make("first"), _make("second"))
    )

    stream = registries.adapters(settings.PLAN_VIVO)
    assert made == [], "nothing is constructed until the first iteration"

    next(stream)
    assert made == ["first"], "the second client must not be open yet"

    next(stream)
    assert made == ["first", "second"]


# -- the shared iteration loop ---------------------------------------------


def test_reconciled_reports_a_cumulative_position(progress):
    list(base.reconciled(iter("abcde"), expected=5, progress=progress))
    progress.assert_cumulative(5)


def test_reconciled_shouts_on_a_short_read(caplog):
    with caplog.at_level("ERROR"):
        records = list(base.reconciled(iter("abc"), expected=10, label="widgets"))
    assert records == ["a", "b", "c"]
    assert "INCOMPLETE: widgets yielded 3 of 10" in caplog.text


def test_reconciled_treats_a_limit_as_a_deliberate_stop(caplog):
    """`--limit` is a smoke test, not a failed sync."""
    with caplog.at_level("ERROR"):
        records = list(base.reconciled(iter("abcde"), expected=5, max_records=2))
    assert records == ["a", "b"]
    assert "INCOMPLETE" not in caplog.text


def test_reconciled_without_an_expected_count_never_complains(caplog):
    """The S&P adapter reconciles per partition, where the counts are."""
    with caplog.at_level("ERROR"):
        list(base.reconciled(iter("abc")))
    assert caplog.text == ""


# -- the shared text helpers -----------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  Sofala  ", "Sofala"),
        ("Sofala   Project", "Sofala Project"),
    ],
)
def test_stated_trims_and_collapses(value, expected):
    assert stated(value) == expected


def test_stated_honours_each_registrys_own_placeholder_table():
    """The tables must stay per registry; the code applying them must not."""
    assert stated("No definido", {"no definido"}) is None
    assert stated("No definido") == "No definido"
    assert stated("--", {"--"}) is None


def test_stated_can_leave_internal_whitespace_alone():
    """JSON values are the registry's own; two spaces are not markup."""
    assert stated("a  b", collapse=False) == "a  b"
    assert stated("a  b") == "a b"


def test_joined_keeps_first_seen_order_and_drops_repeats():
    assert joined(["Land use (AFOLU)", "Land use (AFOLU)", "Energy"]) == (
        "Land use (AFOLU); Energy"
    )
    assert joined([]) is None
    assert joined(None) is None


def test_hashed_id_is_stable_and_fits_a_sqlite_integer():
    first = hashed_id("PLAN_VIVO", "retirements", 100, None)
    assert first == hashed_id("PLAN_VIVO", "retirements", 100, None)
    assert 0 < first < 2**63 - 1


def test_hashed_id_separates_registries():
    """One registry's ledgers can hold rows from two systems."""
    assert hashed_id("VERRA", "x") != hashed_id(settings.PLAN_VIVO, "x")
