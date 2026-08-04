"""The on-disk response cache. No network.

The cache is an optimisation that a full sync leans on ~50,000 times, so its
two failure modes are both "the run breaks in a way that is not obviously the
cache's fault": a body that comes back out of it different from the way it
went in, and a half-written file left by a killed process.
"""

from __future__ import annotations

import json

import httpx
import pytest

from carbon_scraper import http_client, settings


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    directory = tmp_path / "cache"
    directory.mkdir()
    monkeypatch.setattr(settings, "CACHE_DIR", directory)
    return directory


def client_serving(*responses: httpx.Response) -> http_client.RegistryClient:
    """A RegistryClient whose transport hands back canned responses."""
    client = http_client.RegistryClient()
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not remaining:
            raise AssertionError("the cache should have answered this one")
        return remaining.pop(0)

    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


# -- encoding --------------------------------------------------------------


def test_a_non_utf8_body_survives_the_round_trip(cache_dir):
    """The legacy Markit view is JSP and answers ISO-8859-1.

    Storing `response.text` and re-encoding it as UTF-8 on read, while
    keeping the origin's `charset=`, turned "Scolel té" into "Scolel tÃ©" —
    so a live run and a cached re-run disagreed on the project names of the
    one registry that has accented ones.
    """
    body = "Scolel té & N'hambita".encode("iso-8859-1")
    live = httpx.Response(
        200, content=body, headers={"content-type": "text/html; charset=ISO-8859-1"}
    )
    client = client_serving(live)
    try:
        first = client.get_text("https://example.invalid/page.jsp")
        second = client.get_text("https://example.invalid/page.jsp")  # from cache
    finally:
        client.close()

    assert first == "Scolel té & N'hambita"
    assert second == first


def test_a_utf8_body_still_round_trips(cache_dir):
    live = httpx.Response(
        200, json={"name": "Café"}, headers={"content-type": "application/json"}
    )
    client = client_serving(live)
    try:
        assert client.get_json("https://example.invalid/x")["name"] == "Café"
        assert client.get_json("https://example.invalid/x")["name"] == "Café"
    finally:
        client.close()


def test_the_pre_base64_cache_format_is_still_readable(cache_dir):
    """The change must not invalidate an existing ~1 GB cache."""
    key = http_client._cache_key("GET", "https://example.invalid/legacy", None, None)
    http_client._cache_path(key).write_text(
        json.dumps({"status": 200, "body": '{"ok": true}', "headers": {}}),
        encoding="utf-8",
    )
    client = client_serving()  # any request at all would fail
    try:
        assert client.get_json("https://example.invalid/legacy") == {"ok": True}
    finally:
        client.close()


# -- counts live in headers ------------------------------------------------


def test_a_cache_hit_still_carries_the_totals_header(cache_dir):
    """Gold Standard publishes its result count only in `X-Total-Count`.

    A cache hit that dropped it would leave reconciliation comparing against
    zero — a short read that reports itself as complete.
    """
    live = httpx.Response(
        200,
        json=[{"id": 1}],
        headers={"content-type": "application/json", "X-Total-Count": "4141"},
    )
    client = client_serving(live)
    try:
        _, headers = client.get_json_with_headers("https://example.invalid/projects")
        assert headers["x-total-count"] == "4141"
        _, cached = client.get_json_with_headers("https://example.invalid/projects")
        assert cached["x-total-count"] == "4141"
    finally:
        client.close()


# -- half-written entries --------------------------------------------------


def test_a_truncated_cache_entry_is_a_miss_not_a_crash(cache_dir):
    """Cancel, or a closed lid, mid-write. The next run must not traceback."""
    key = http_client._cache_key("GET", "https://example.invalid/x", None, None)
    http_client._cache_path(key).write_text('{"status": 200, "bo', encoding="utf-8")

    client = client_serving(httpx.Response(200, json={"ok": True}))
    try:
        assert client.get_json("https://example.invalid/x") == {"ok": True}
    finally:
        client.close()


def test_a_cache_entry_is_written_atomically(cache_dir):
    """No `.tmp` left behind, and the final file only appears complete."""
    client = client_serving(httpx.Response(200, json={"ok": True}))
    try:
        client.get_json("https://example.invalid/x")
    finally:
        client.close()

    written = list(cache_dir.iterdir())
    assert len(written) == 1
    assert written[0].suffix == ".json"
    assert json.loads(written[0].read_text(encoding="utf-8"))["status"] == 200


# -- invalidation ----------------------------------------------------------


def test_invalidate_drops_a_refusal_that_arrived_as_a_200(cache_dir):
    """EcoRegistry reports `ERROR_401` at HTTP 200; Platts reports a wrong
    `standardId` as `totalEntities: 0`. Both get cached for 24 hours, so
    "fix the header and re-run" cannot work until the entry is dropped."""
    refusal = httpx.Response(200, json={"codeMessages": [{"code": "ERROR_401"}]})
    good = httpx.Response(200, json={"projects": [1, 2, 3]})
    client = client_serving(refusal, good)
    try:
        url = "https://example.invalid/project"
        assert "codeMessages" in client.get_json(url)
        client.invalidate("GET", url)
        assert client.get_json(url) == {"projects": [1, 2, 3]}
    finally:
        client.close()


def test_invalidate_is_a_no_op_when_nothing_is_cached(cache_dir):
    client = http_client.RegistryClient()
    try:
        client.invalidate("GET", "https://example.invalid/never-asked")
    finally:
        client.close()
