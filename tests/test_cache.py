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


def test_a_server_that_declares_no_charset_is_decoded_as_the_caller_measured(cache_dir):
    """Xpansiv/APX sends Windows-1252 and says so nowhere.

    No `charset` on `Content-Type`, no `<meta charset>`, no BOM — so httpx
    falls back to UTF-8 and decodes with `errors="replace"`. That is HTTP 200
    with every accent in the registry replaced: 491 of Climate Action
    Reserve's projects are Mexican, and `STATE OF MÉXICO` arrived as
    `STATE OF M�XICO` with nothing in the log. It cannot be repaired
    afterwards — every accented character collapses onto one replacement
    character — which is why the caller states the encoding it measured.
    """
    body = "STATE OF MÉXICO — Oaxaca de Juárez".encode("cp1252")
    live = httpx.Response(200, content=body, headers={"content-type": "text/html"})
    client = client_serving(live)
    try:
        assert client.get_text("https://example.invalid/r.asp") == (
            "STATE OF M�XICO � Oaxaca de Ju�rez"
        ), "without a fallback this is what httpx does, and it is why the flag exists"
        assert (
            client.get_text("https://example.invalid/r.asp", fallback_encoding="cp1252")
            == "STATE OF MÉXICO — Oaxaca de Juárez"
        )
    finally:
        client.close()


def test_a_declared_charset_still_wins_over_the_fallback(cache_dir):
    """The fallback is for a silent server, never an override of a stated one."""
    body = "Scolel té".encode("iso-8859-1")
    live = httpx.Response(
        200, content=body, headers={"content-type": "text/html; charset=ISO-8859-1"}
    )
    client = client_serving(live)
    try:
        assert (
            client.get_text("https://example.invalid/p.jsp", fallback_encoding="cp1252")
            == "Scolel té"
        )
    finally:
        client.close()


def test_utf8_is_tried_first_so_a_newer_tenant_is_unaffected(cache_dir):
    """APX is one module across many tenants and only one build was measured.

    `decoded` tries UTF-8 **strictly** before the fallback, so a tenant whose
    build sends UTF-8 without declaring it decodes correctly rather than being
    mojibaked into `Ã©` by a platform constant measured somewhere else.
    """
    body = "Oaxaca de Juárez".encode("utf-8")
    live = httpx.Response(200, content=body, headers={"content-type": "text/html"})
    client = client_serving(live)
    try:
        assert (
            client.get_text("https://example.invalid/q.asp", fallback_encoding="cp1252")
            == "Oaxaca de Juárez"
        )
    finally:
        client.close()


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


# -- form bodies -----------------------------------------------------------


def test_a_form_body_is_part_of_the_cache_key():
    """ICE GreenTrace posts its paging offset and its ledger selector in a form.

    Two calls to one URL that differ only in that body are two different
    questions — the same shape as the `standardid` header that once served
    Verra JNR its VCS responses.
    """
    url = "https://example.invalid/report/results"
    first = http_client._cache_key("POST", url, None, None, {"offset": 0})
    second = http_client._cache_key("POST", url, None, None, {"offset": 2000})
    ledger = http_client._cache_key(
        "POST", url, None, None, {"offset": 0, "holdingStatus": "RETIRED"}
    )
    assert len({first, second, ledger}) == 3


def test_adding_form_bodies_did_not_move_every_existing_key():
    """A ~1 GB cache of seven registries must survive the feature that needed none of it."""
    without = http_client._cache_key("GET", "https://example.invalid/x", None, None)
    explicitly_none = http_client._cache_key(
        "GET", "https://example.invalid/x", None, None, None
    )
    assert without == explicitly_none


def test_a_form_post_sends_the_form_content_type(cache_dir):
    """The shared client header says JSON and wins over what httpx would infer.

    GreenTrace answers a form body sent as `application/json` with the same
    generic HTTP 500 it gives an unmapped path.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(200, json={"datasets": {}})

    client = http_client.RegistryClient()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        client.post_form("https://example.invalid/results", {"offset": 0, "max": 2000})
    finally:
        client.close()

    assert seen["content_type"] == "application/x-www-form-urlencoded"
    assert b"offset=0" in seen["body"]


def test_a_callers_content_type_wins_however_it_is_spelled(cache_dir):
    """Header names are case-insensitive and a plain dict read is not.

    A caller stating `content-type` has stated it just as fully as one stating
    `Content-Type`, and the merge below it already knows that — it goes through
    `httpx.Headers` precisely because a dict merge sent Cercarbono two
    conflicting `Origin` values. The default here has to read the caller's
    headers the same way, or it overwrites a content type the caller chose.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={})

    for spelling in ("Content-Type", "content-type", "CONTENT-TYPE"):
        client = http_client.RegistryClient()
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            client.post_form(
                f"https://example.invalid/{spelling}",
                {"offset": 0},
                headers={spelling: "multipart/form-data"},
            )
        finally:
            client.close()
        assert seen["content_type"] == "multipart/form-data", spelling


# -- politeness ------------------------------------------------------------


def test_a_stated_rate_of_zero_never_speeds_a_client_up(monkeypatch):
    """Both rates come from the environment and both can be `0`.

    `RateLimiter` reads a non-positive rate as "no limit", so neither
    `requested or ceiling` nor `min()` says "the slower of the two": a
    `CARBON_ACR_RPS=0` handed ACR the global 1/s — seven times the rate that
    registry bans at — and a `VERRA_RPS=0` discarded ACR's 0.14 from the other
    side. Only "no rate stated anywhere" may mean unlimited.
    """
    assert http_client._slowest_rate(0.0, 1.0) == 1.0
    assert http_client._slowest_rate(0.14, 0.0) == 0.14
    assert http_client._slowest_rate(25.0, 1.0) == 1.0
    assert http_client._slowest_rate(None, 1.0) == 1.0
    assert http_client._slowest_rate(None, 0.0) == 0.0

    monkeypatch.setattr(http_client.settings, "REQUESTS_PER_SECOND", 1.0)
    client = http_client.RegistryClient(requests_per_second=0.0)
    try:
        assert client._limiter._min_interval >= 1.0
    finally:
        client.close()


def test_a_429_retry_after_is_honoured_up_to_the_cap(monkeypatch):
    """Cloudflare answers a tripped rate limit with `Retry-After: 3600`.

    Retrying after two seconds because the exponential curve says so is how a
    rate-limited scrape becomes a blocked one. The cap is what stops the other
    failure — an hour asleep inside a GUI run, indistinguishable from a hang.
    """
    slept: list[float] = []
    monkeypatch.setattr(settings, "MAX_RETRY_AFTER", 120.0)
    monkeypatch.setattr(settings, "MAX_RETRIES", 2)
    monkeypatch.setattr(http_client, "_sleep", lambda s, c: slept.append(s))

    client = http_client.RegistryClient()
    client._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "3600"})
        )
    )
    try:
        with pytest.raises(http_client.RetryableStatus):
            client.request("GET", "https://example.invalid/limited", use_cache=False)
    finally:
        client.close()

    # The rate limiter sleeps here too, so the assertion is about the retry
    # wait specifically: the cap, and nothing shorter that could only have come
    # from the exponential curve.
    assert 120.0 in slept
    assert [s for s in slept if s > 1.0] == [120.0]


def test_a_retryable_status_without_a_retry_after_uses_the_backoff(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(settings, "MAX_RETRIES", 2)
    monkeypatch.setattr(http_client, "_sleep", lambda s, c: slept.append(s))

    client = http_client.RegistryClient()
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    try:
        with pytest.raises(http_client.RetryableStatus):
            client.request("GET", "https://example.invalid/broken", use_cache=False)
    finally:
        client.close()

    assert slept and slept[0] < settings.MAX_RETRY_AFTER
