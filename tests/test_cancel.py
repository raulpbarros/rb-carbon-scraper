"""Cancellation must propagate. No network.

`http_client.Cancelled` is an ordinary `Exception`, so every `except
Exception` on the request path is a place where pressing Cancel can be
mistaken for a bad row or a flaky network and swallowed. The failure is not
a crash — it is worse than a crash: the run carries on, reaches
`db.finish_run(ok=True)`, and the window reports the cancelled run as a
success.

These pin the three sites where that was true.
"""

from __future__ import annotations

import threading

import pytest

from carbon_scraper import http_client, settings
from carbon_scraper.http_client import Cancelled, RegistryClient
from carbon_scraper.registries.verra.api import VerraAPI


@pytest.fixture()
def cancelled() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


# -- the routing table -----------------------------------------------------


def test_environment_config_does_not_swallow_a_cancel(cancelled):
    """A cancel is not "could not read the config; using the fallback".

    Swallowed here, the sync continues against `FALLBACK_URIS` and the user
    who pressed Cancel watches it keep going.
    """
    client = RegistryClient(cancel=cancelled)
    with pytest.raises(Cancelled):
        client.environment_config(settings.ENVIRONMENT_CONFIG_URL)
    client.close()


def test_environment_config_still_degrades_on_a_real_failure(monkeypatch):
    """The fallback path is the point of that handler; keep it working."""
    client = RegistryClient()

    def boom(*args, **kwargs):
        raise RuntimeError("network is down")

    monkeypatch.setattr(client, "get_json", boom)
    assert client.environment_config("https://example.invalid/config.json") == dict(
        settings.FALLBACK_URIS
    )
    client.close()


# -- the exact-totals pass -------------------------------------------------


class _CancellingAPI(VerraAPI):
    """Answers the first project, then behaves as if Cancel were pressed."""

    def __init__(self):  # noqa: D107 - no client wanted; nothing is sent
        self.calls = 0
        self.client = None
        self._owns_client = False

    def sum_field(self, resource, field, filter_model=None):
        self.calls += 1
        if self.calls > 1:
            raise Cancelled("cancelled")
        return 100.0


def test_sum_by_project_does_not_swallow_a_cancel():
    """`verra totals` is 175 minutes of a Verra update.

    Its per-project handler exists so one bad project cannot stop the run.
    A cancel is not a bad project: swallowed, the loop spins through every
    remaining project logging an error apiece and `pipeline.totals` then
    marks the run OK.
    """
    api = _CancellingAPI()
    results = api.sum_by_project("retirements", [1, 2, 3])
    assert next(results) == (1, 100.0, None)
    with pytest.raises(Cancelled):
        next(results)
    assert api.calls == 2  # stopped at the cancel, did not walk projects 3..n


class _FailingAPI(VerraAPI):
    """One project raises a real error; the rest are fine."""

    def __init__(self):  # noqa: D107
        self.client = None
        self._owns_client = False

    def sum_field(self, resource, field, filter_model=None):
        if filter_model["projectId"]["columnFilters"][0]["filter"] == "2":
            raise RuntimeError("that one project is broken")
        return 7.0


def test_sum_by_project_still_skips_one_bad_project(caplog):
    api = _FailingAPI()
    assert [pid for pid, _, _ in api.sum_by_project("retirements", [1, 2, 3])] == [1, 3]
    assert "Totals failed for project 2" in caplog.text


# -- partition-value sampling ----------------------------------------------


class _SamplingAPI(VerraAPI):
    def __init__(self, error):
        self.client = None
        self._owns_client = False
        self._error = error

    def page_search(self, resource, **kwargs):
        raise self._error


def test_distinct_values_does_not_swallow_a_cancel():
    api = _SamplingAPI(Cancelled("cancelled"))
    with pytest.raises(Cancelled):
        api._distinct_values("retirements", "vintage", {})


def test_distinct_values_says_so_when_sampling_fails(caplog):
    """A 500 is not "a short index".

    Treating one as the other builds the partition set from fewer values
    than exist. Returning an empty list silently skips the key entirely.
    """
    api = _SamplingAPI(http_client.RetryableStatus("500 from the registry"))
    assert api._distinct_values("retirements", "vintage", {}) == []
    assert "Sampling retirements for distinct vintage values failed" in caplog.text
