"""API request-shape and response-parsing tests. No network.

These pin the S&P Platts contract. They were written against Verra and still
run against Verra: the Phase-2 move of the paging code into
`registries/platts/` must not change a single assertion here.
"""

from __future__ import annotations

from carbon_scraper.registries.platts import discovery
from carbon_scraper.registries.verra import api


def test_registry_headers_are_present():
    """Without these four headers every call is a misleading HTTP 500."""
    for header in ("registry", "standardid", "standardacronym", "language"):
        assert header in api.REGISTRY_HEADERS


def test_text_equals_builds_the_filter_shape_the_api_expects():
    assert api.text_equals("vintage", "2020") == {
        "vintage": {"columnFilters": [{"filterType": "Text", "type": "equals", "filter": "2020"}]}
    }


def test_page_limit_never_exceeds_the_server_maximum():
    # 500 fails against the live API; 400 is the ceiling.
    assert api.MAX_LIMIT == 400
    assert api.MAX_WINDOW == 10_000


def test_every_ledger_has_partition_keys():
    for resource in api.ALL_RESOURCES:
        assert api.PARTITION_KEYS.get(resource), resource


def test_find_records_handles_the_registry_envelope():
    payload = {"entities": [{"projectId": 1}, {"projectId": 2}], "totalEntities": 2}
    assert discovery.find_records(payload) == [{"projectId": 1}, {"projectId": 2}]
    assert discovery.find_total(payload) == 2


def test_find_records_handles_raw_elasticsearch_shape():
    payload = {"hits": {"hits": [{"_source": {"projectId": 7}}]}}
    assert discovery.find_records(payload) == [{"projectId": 7}]


def test_number_equals_filter_shape():
    assert api.number_equals("projectId", 1728) == {
        "projectId": {
            "columnFilters": [{"filterType": "Number", "type": "equals", "filter": "1728"}]
        }
    }


class _FakeAPI:
    """Stands in for the live API so the guard can be tested offline."""

    def __init__(self, counts):
        self._counts = counts

    def count(self, resource, filter_model=None):
        return self._counts

    filter_narrows = api.VerraAPI.filter_narrows


def test_filter_guard_detects_an_ignored_filter():
    """The API returns the whole index for filters it does not understand.

    A 'partition' that is no smaller than its parent is that failure, and
    must never be treated as a valid split — it caused 38,522 retirement
    records to be lost on the first full run.
    """
    ignored = _FakeAPI(305_144)  # same size as the parent -> filter did nothing
    assert ignored.filter_narrows("retirements", {"vintage": "..."}, 305_144) is False

    real = _FakeAPI(26_963)
    assert real.filter_narrows("retirements", {"vintage": "2016"}, 305_144) is True


def test_discovery_classifies_captured_calls():
    assert discovery.classify(".../project/publicReportPageSearch", None) == "project_search"
    assert discovery.classify(".../retirements/publicReportPageSearch", None) == "units_search"
    assert discovery.classify(".../cms-resources/download/image/x.jpg", None) == "export"


def test_every_ledger_search_is_classified_as_units():
    """Regression: 'publicReportPageSearch' contains 'report', which an eager
    export pattern used to match, filing cancellations under the wrong role."""
    for ledger in api.LEDGERS:
        url = f"https://host/es/public/{ledger}/publicReportPageSearch"
        assert discovery.classify(url, None) == "units_search", ledger
