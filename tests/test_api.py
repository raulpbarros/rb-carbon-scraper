"""API request-shape and response-parsing tests. No network.

These pin the S&P Platts contract. They were written against Verra and still
run against Verra: the Phase-2 move of the paging code into
`registries/platts/` must not change a single assertion here.
"""

from __future__ import annotations

from carbon_scraper import http_client, settings
from carbon_scraper.registries.platts import discovery
from carbon_scraper.registries.verra import api
from carbon_scraper.registries.verra.jnr import VerraJNRAPI


def test_registry_headers_are_present():
    """Without these four headers every call is a misleading HTTP 500."""
    for header in ("registry", "standardid", "standardacronym", "language"):
        assert header in api.REGISTRY_HEADERS


# -- two standards on one tenant -------------------------------------------


def test_jnr_differs_from_vcs_only_in_the_standard():
    """One registry, one platform, two standards. Three attributes apart."""
    assert VerraJNRAPI.registry == api.VerraAPI.registry == settings.VERRA
    assert VerraJNRAPI.registry_code == api.VerraAPI.registry_code
    assert VerraJNRAPI.standard_id == settings.VERRA_JNR_STANDARD_ID
    assert VerraJNRAPI.standard_id != api.VerraAPI.standard_id


def test_the_cache_key_separates_two_standards_on_one_url():
    """The header is the only thing that says which standard was asked for.

    VCS and JNR POST an identical body to an identical URL and differ only in
    `standardid`. With headers left out of the cache key the second standard
    scraped in a run serves the first one's responses — 5,247 JNR projects,
    every one of them VCS, `standard_name` reading "Verified Carbon Standard",
    and nothing raised anywhere.
    """
    url = "https://prod-us.api.platts.com/x/project/publicReportPageSearch"
    body = {"searchFilter": {"pagination": {"start": 0}}}

    vcs = http_client._cache_key("POST", url, body, api.VerraAPI.registry_headers())
    jnr = http_client._cache_key("POST", url, body, VerraJNRAPI.registry_headers())
    assert vcs != jnr


def test_the_cache_key_ignores_headers_that_are_not_identity():
    """A correlation id changes per call; it must not defeat the cache."""
    url = "https://example.invalid/x"
    base = {"registry": "VERRA", "standardid": "1"}
    assert http_client._cache_key("GET", url, None, base) == http_client._cache_key(
        "GET", url, None, {**base, "x-correlation-id": "whatever"}
    )


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


def test_filter_narrows_accepts_a_count_already_paid_for():
    """`_partitions` has the child's count in hand; it must not re-ask."""
    api_ = _FakeAPI(999_999)  # would say "ignored" if it were consulted
    assert api_.filter_narrows("retirements", {}, 305_144, 26_963) is True


# -- partitioning ----------------------------------------------------------


class _PartitioningAPI(api.VerraAPI):
    """Answers counts from a table and records what was drained."""

    def __init__(self, counts, values):
        self.client = None
        self._owns_client = False
        self._counts = counts
        self._values = values
        self.drained: list[str] = []

    @staticmethod
    def _key(filter_model):
        for field, spec in sorted(filter_model.items()):
            return f"{field}={spec['columnFilters'][0]['filter']}"
        return "*"

    def count(self, resource, filter_model=None):
        return self._counts[self._key(filter_model or {})]

    def _distinct_values(self, resource, field, filter_model):
        return self._values.get(field, [])

    def _drain(self, resource, filter_model, expected):
        self.drained.append(self._key(filter_model))
        return iter(())


def test_a_split_is_validated_whole_before_anything_is_drained():
    """One ignored value must abandon the split, not half-emit it.

    Validating as it went meant the buckets checked before the bad one had
    already been drained by the caller, so retrying with the next key
    re-emitted them under a different filter: duplicate reads and inflated
    counts, from the guard that exists to prevent exactly that.
    """
    counts = {
        "*": 20_000,
        "vintage=2016": 5_000,
        "vintage=2017": 4_000,
        "vintage=2018": 20_000,  # ignored: no smaller than its parent
        "regionName=Asia": 12_000,
        "regionName=Africa": 8_000,
    }
    client = _PartitioningAPI(
        counts,
        {"vintage": ["2016", "2017", "2018"], "regionName": ["Asia", "Africa"]},
    )
    list(client.iter_records("retirements"))

    # Nothing from the abandoned vintage split reached the drain.
    assert not any(name.startswith("vintage=") for name in client.drained)
    assert client.drained == ["regionName=Asia", "regionName=Africa"]


def test_a_split_that_narrows_is_used():
    counts = {
        "*": 20_000,
        "vintage=2016": 9_000,
        "vintage=2017": 9_000,
        "vintage=2018": 2_000,
    }
    client = _PartitioningAPI(counts, {"vintage": ["2016", "2017", "2018"]})
    list(client.iter_records("retirements"))
    assert client.drained == ["vintage=2016", "vintage=2017", "vintage=2018"]


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
