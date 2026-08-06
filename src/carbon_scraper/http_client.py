"""HTTP layer: rate limiting, retries, and an on-disk cache.

The cache exists so that developing against this scraper does not mean
hammering Verra's registry. A cached response is reused until it expires;
pass `refresh=True` (or run with VERRA_NO_CACHE=1) to bypass it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import settings

log = logging.getLogger(__name__)


def cache_disabled() -> bool:
    """Read the env var per call, not at import.

    `sync --refresh` sets VERRA_NO_CACHE at runtime. Reading it at import time
    only worked because `http_client` happened to be imported lazily by the
    adapter dispatch; anything that imports it eagerly — the GUI, for one —
    would silently get a cached run instead of a fresh one.
    """
    return os.environ.get("VERRA_NO_CACHE") == "1"


def cache_ttl_seconds() -> int:
    return int(os.environ.get("VERRA_CACHE_TTL", str(24 * 3600)))


class Cancelled(Exception):
    """Raised when the caller asked to stop mid-run."""


class RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, per_second: float, cancel: threading.Event | None = None) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0
        self._cancel = cancel

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                _sleep(self._min_interval - delta, self._cancel)
            self._last = time.monotonic()


def _sleep(seconds: float, cancel: threading.Event | None) -> None:
    """Sleep, but wake immediately if cancellation is requested.

    Every wait in this module goes through here. A plain `time.sleep` in the
    retry path can block for 45 seconds, which is long enough that a GUI Cancel
    button looks broken and a closed window leaves a live worker thread.
    """
    if cancel is None:
        time.sleep(seconds)
        return
    if cancel.wait(seconds):
        raise Cancelled("cancelled while waiting")


#: Headers that select *which data* a response contains, rather than how it is
#: transported. They belong in the cache key: two requests identical in method,
#: URL and body but differing in one of these are two different questions.
#:
#: This is not hypothetical. The S&P platform identifies a registry and a
#: standard entirely in headers — `POST …/project/publicReportPageSearch` with
#: the same body returns Verra VCS or Verra JNR depending only on `standardid`.
#: Left out of the key, the second standard scraped in a run silently served
#: the first one's cached responses: 5,247 JNR projects, all of them VCS, with
#: `standard_name` reading "Verified Carbon Standard" and nothing raised.
IDENTITY_HEADERS = ("registry", "standardid", "standardacronym", "language", "platform")


def _cache_key(
    method: str, url: str, body: Any, headers: Any = None, form: Any = None
) -> str:
    identity = {
        name: value
        for name, value in (headers or {}).items()
        if name.lower() in IDENTITY_HEADERS
    }
    payload: dict[str, Any] = {"m": method.upper(), "u": url, "b": body, "h": identity}
    # Added only when there is a form body, so the ~1 GB of entries written
    # before form posts existed still hash to the same key. A new key here for
    # every registry would mean re-scraping all seven to warm the cache again.
    if form is not None:
        payload["f"] = form
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_path(key: str) -> Path:
    return settings.CACHE_DIR / f"{key}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    """A cache entry, or None if it cannot be read.

    A full sync writes ~50,000 of these. A process killed mid-write — Cancel,
    a closed lid — leaves one truncated, and an unguarded `json.loads` then
    ends the next run with a traceback in a dialog rather than a cache miss.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.debug("Discarding unreadable cache entry %s", path.name)
        return None


def _cached_body(cached: dict[str, Any]) -> bytes:
    """The stored body as bytes.

    `body` is the pre-base64 format. Kept so an existing ~1 GB cache is not
    invalidated by the change; new entries are always written as `body_b64`.
    """
    encoded = cached.get("body_b64")
    if encoded is not None:
        return base64.b64decode(encoded)
    return str(cached.get("body", "")).encode()


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Write a cache entry atomically.

    Straight to the final name leaves a truncated file behind when the
    process dies mid-write; `os.replace` is atomic on NTFS.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # a cache is an optimisation, never a hard failure
        log.debug("Could not write cache entry %s: %s", path.name, exc)
        tmp.unlink(missing_ok=True)


class RetryableStatus(Exception):
    """Raised for status codes worth retrying (429 / 5xx).

    Carries the server's own `Retry-After`, where it sent one. A 429 is the
    only response that knows how long to wait, and guessing shorter than it
    asked is how a rate-limited scrape turns into a banned one.

    It carries `status` for the same reason. A 429 is raised here rather than
    reaching `raise_for_status()`, so an adapter that turns a registry's
    refusals into something readable never sees an `HTTPStatusError` for one
    and has to recognise it from this exception instead.
    """

    def __init__(
        self,
        message: str,
        retry_after: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """`Retry-After` in seconds, if the server sent a usable one.

    Only the delta-seconds form is read. The HTTP-date form is legal and no
    registry here has ever sent one; parsing a date wrong would produce a wait
    of hours or of nothing, and both are worse than falling back to the
    ordinary backoff.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


def decoded(response: httpx.Response, fallback: str | None = None) -> str:
    """A response body as text, for a server that may not say what it sent.

    **httpx assumes UTF-8 when nothing declares otherwise, and decodes with
    `errors="replace"`** — so a legacy server sending Windows-1252 bytes
    produces a body full of `U+FFFD` at HTTP 200, with nothing raised. It is
    lossy in the direction that matters: every accented character in a registry
    collapses onto the same replacement character, so `MÉXICO`, `MICHOACÁN` and
    `Oaxaca de Juárez` all arrive damaged and none of them can be repaired
    afterwards.

    Xpansiv/APX is where this was measured (2026-08-05): classic ASP, no
    `charset` in `Content-Type`, no `<meta charset>` in the markup, and
    Windows-1252 on the wire. 491 Climate Action Reserve projects are Mexican
    and their state names carry accents, as do project names, retirement
    reasons and every apostrophe the site's own English text uses (`0x92`, a
    curly quote, not ASCII).

    The order is: what the server declared, then UTF-8 **strictly**, then the
    caller's measured fallback. Strict is the point — UTF-8 is tried and
    allowed to *fail*, rather than silently substituting, so a modern page
    still decodes as UTF-8 and a legacy one falls through to the encoding the
    platform was measured to use instead of to a row of question marks.
    """
    if response.charset_encoding or fallback is None:
        return response.text
    body = response.content
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode(fallback, errors="replace")


def _slowest_rate(requested: float | None, ceiling: float) -> float:
    """The slower of an adapter's request rate and the global one.

    Neither `requested or ceiling` nor `min(...)` says this correctly, because
    **zero is a meaningful value on both sides**: `RateLimiter` reads a
    non-positive rate as "no limit", and both numbers come from the
    environment.

    * `CARBON_ACR_RPS=0` made the truthiness fallback discard ACR's rate and
      hand it the global 1/s — seven times the rate that registry bans at, out
      of a variable whose only documented purpose is going slower.
    * `VERRA_RPS=0` made `min()` return 0 and discard ACR's 0.14 the same way,
      from the other side.

    So the stated rates are ranked and the slowest wins; "no limit" is the
    fastest thing there is and can only apply when nothing was stated at all.
    """
    stated = [rate for rate in (requested, ceiling) if rate is not None and rate > 0]
    return min(stated) if stated else 0.0


class RegistryClient:
    """Thin httpx wrapper that knows how to talk to a registry politely.

    Shared by every registry adapter. The live-config helpers below are only
    meaningful for Verra; Gold Standard uses a fixed host and ignores them.
    """

    def __init__(
        self,
        *,
        timeout: float | None = None,
        cancel: threading.Event | None = None,
        requests_per_second: float | None = None,
    ) -> None:
        settings.ensure_dirs()
        self._cancel = cancel
        # Per registry, because politeness is not one number: ICE GreenTrace
        # sits behind a Cloudflare rate-limiting rule that answers the ~100th
        # request in ten minutes with a 429 and `Retry-After: 3600`, and ACR
        # needs ~1,005 requests. Every other registry here is happy at 1/s and
        # keeps it. This is a limit an adapter may only lower.
        self._limiter = RateLimiter(
            _slowest_rate(requests_per_second, settings.REQUESTS_PER_SECOND),
            cancel,
        )
        self._client = httpx.Client(
            timeout=timeout or settings.REQUEST_TIMEOUT,
            follow_redirects=True,
            headers=settings.BROWSER_HEADERS,
        )
        # Keyed by config URL: the Platts platform serves several registries,
        # each publishing its own routing table on its own site. One cache slot
        # would hand Plan Vivo whichever table Verra happened to fetch first.
        self._uris: dict[str, dict[str, Any]] = {}
        self._api_headers: dict[str, dict[str, str]] = {}

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def raise_if_cancelled(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise Cancelled("cancelled")

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- live config -------------------------------------------------------

    def environment_config(self, config_url: str | None = None) -> dict[str, Any]:
        """Fetch the app's own published routing table.

        `config_url` names which registry's table to read; it defaults to
        Verra's. Reading it at runtime rather than hardcoding base URIs is what
        lets S&P move the backend without breaking us.
        """
        url = config_url or settings.ENVIRONMENT_CONFIG_URL
        cached = self._uris.get(url)
        if cached is not None:
            return cached
        try:
            data = self.get_json(url, use_cache=True)
            self._uris[url] = data.get("uris", {})
        except Cancelled:
            # A cancel is not a network degradation. Swallowing it here turns
            # pressing Cancel into "using fallback URIs" and the run carries on.
            raise
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            log.warning("Could not read live environment config (%s); using fallback.", exc)
            self._uris[url] = dict(settings.FALLBACK_URIS)
        return self._uris[url]

    def api_headers(self, config_url: str | None = None) -> dict[str, str]:
        """Headers the browser sends on every API call."""
        url = config_url or settings.ENVIRONMENT_CONFIG_URL
        cached = self._api_headers.get(url)
        if cached is not None:
            return cached
        uris = self.environment_config(url)
        defaults = (uris.get("common") or {}).get("DEFAULT_HEADERS")
        headers = dict(settings.BROWSER_HEADERS)
        headers.update(defaults or settings.FALLBACK_HEADERS)
        self._api_headers[url] = {k: str(v) for k, v in headers.items()}
        return self._api_headers[url]

    def base_uri(self, name: str, *, config_url: str | None = None) -> str | None:
        value = self.environment_config(config_url).get(name)
        return value if isinstance(value, str) else None

    # -- requests ----------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        form_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        refresh: bool = False,
    ) -> httpx.Response:
        self.raise_if_cancelled()
        key = _cache_key(
            method,
            httpx.URL(url).copy_merge_params(params or {}).__str__(),
            json_body,
            headers,
            form_body,
        )
        path = _cache_path(key)
        caching = use_cache and not cache_disabled()

        if caching and not refresh and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < cache_ttl_seconds():
                cached = _read_cache(path)
                if cached is not None:
                    return httpx.Response(
                        status_code=cached["status"],
                        content=_cached_body(cached),
                        headers=cached.get("headers", {}),
                        request=httpx.Request(method, url),
                    )

        response = self._send(method, url, json_body, params, headers, form_body)

        if caching and response.status_code < 400:
            _write_cache(
                path,
                {
                    "status": response.status_code,
                    # Bytes, not `response.text`. Storing the decoded body and
                    # re-encoding it as UTF-8 on read while keeping the origin's
                    # `charset=` corrupts anything that was not UTF-8 to begin
                    # with — the legacy Markit view is server-rendered JSP and
                    # answers ISO-8859-1, so "Scolel té" came back out of the
                    # cache as "Scolel tÃ©" and a cached re-run disagreed with
                    # the live one.
                    "body_b64": base64.b64encode(response.content).decode("ascii"),
                    # `x-total-*` matters: Gold Standard returns its result
                    # counts only in headers, and a cache hit that dropped
                    # them would silently break reconciliation.
                    "headers": {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() == "content-type"
                        or name.lower().startswith("x-total")
                    },
                },
            )
        return response

    def invalidate(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        form_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Drop one cached response.

        For the answers that are refusals wearing a 200: EcoRegistry reports
        `ERROR_401` in the body, and the Platts platform reports a wrong
        `standardId` as `totalEntities: 0`. Both pass the `status_code < 400`
        test and get written to disk, so every re-run for the next 24 hours
        replays the same poisoned answer — and "fix the header, re-run" is
        exactly what someone debugging a header does, without reaching for
        `--refresh`.
        """
        key = _cache_key(
            method,
            httpx.URL(url).copy_merge_params(params or {}).__str__(),
            json_body,
            headers,
            form_body,
        )
        _cache_path(key).unlink(missing_ok=True)

    def _send(
        self,
        method: str,
        url: str,
        json_body: Any,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        form_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send with backoff, built per call rather than at import time.

        A `@retry` decorator would freeze `settings.MAX_RETRIES` at import and
        sleep uninterruptibly; `Retrying` here reads the setting per call and
        takes our cancellable sleep. `Cancelled` is deliberately not in the
        retryable set, so it propagates out of the loop at once.
        """
        backoff = wait_exponential(multiplier=1.5, min=2, max=45)

        def wait(retry_state: Any) -> float:
            """Backoff, but never shorter than a 429 asked for.

            Cloudflare answers a tripped rate-limit rule with
            `Retry-After: 3600`, and retrying after 2 seconds because the
            exponential curve says so is how a rate-limited scrape becomes a
            blocked one. Capped at `settings.MAX_RETRY_AFTER` because an hour
            asleep inside a GUI run is indistinguishable from a hang — past
            the cap the attempt is spent, the run fails, and re-running it
            resumes from the cache. Every write is an idempotent upsert.
            """
            exception = retry_state.outcome.exception() if retry_state.outcome else None
            asked = getattr(exception, "retry_after", None)
            if asked:
                return min(float(asked), settings.MAX_RETRY_AFTER)
            return backoff(retry_state)

        retrying = Retrying(
            stop=stop_after_attempt(settings.MAX_RETRIES),
            wait=wait,
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
            reraise=True,
            sleep=lambda seconds: _sleep(seconds, self._cancel),
        )
        return retrying(
            self._send_once, method, url, json_body, params, headers, form_body
        )

    def _send_once(
        self,
        method: str,
        url: str,
        json_body: Any,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        form_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self.raise_if_cancelled()
        self._limiter.wait()
        # httpx.Headers, not a dict: header names are case-insensitive, and a
        # plain dict merge keeps BOTH `origin` (lowercased on the client) and
        # an adapter's `Origin`, sending two conflicting values. Cercarbono
        # rejects Verra's Origin with a generic HTTP 500, so an adapter that
        # overrides a shared header has to actually override it.
        supplied = httpx.Headers(headers or {})
        merged = httpx.Headers(self._client.headers)
        merged.update(supplied)
        # `supplied`, not `merged`: the shared client header always carries a
        # content type, so asking the merged set whether one was stated always
        # answers yes. And `httpx.Headers`, not the caller's dict, for the same
        # reason the merge above uses it — a caller spelling the header
        # `content-type` states it just as fully as `Content-Type`, and a plain
        # `.get("Content-Type")` would not see it and would overwrite it here.
        if form_body is not None and "content-type" not in supplied:
            # The shared client header says `application/json`, and it wins over
            # the one httpx would infer from `data=`. ICE GreenTrace answers a
            # form body sent under that content type with the same generic HTTP
            # 500 it gives an unmapped path, so this is set here rather than
            # left to each caller to remember.
            merged["Content-Type"] = "application/x-www-form-urlencoded"
        response = self._client.request(
            method.upper(),
            url,
            json=json_body,
            data=form_body,
            params=params,
            headers=merged,
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableStatus(
                f"{response.status_code} from {url}",
                _retry_after_seconds(response),
                response.status_code,
            )
        return response

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.request("GET", url, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_text(
        self, url: str, *, fallback_encoding: str | None = None, **kwargs: Any
    ) -> str:
        """GET returning the body as text.

        For the registries that never learned to speak JSON: the legacy Markit
        public view is server-rendered JSP, so its adapter parses HTML. The
        cache stores the body as text either way, so this costs nothing extra.

        `fallback_encoding` is for a server that declares no charset anywhere
        and does not mean UTF-8. See `decoded()`.
        """
        response = self.request("GET", url, **kwargs)
        response.raise_for_status()
        return decoded(response, fallback_encoding)

    def get_json_with_headers(self, url: str, **kwargs: Any) -> tuple[Any, Any]:
        """GET returning (body, headers).

        Gold Standard publishes its result counts in `X-Total-Count` and
        `X-Total-Number-Of-Credits` response headers rather than in the body,
        and reconciliation depends on reading them.
        """
        response = self.request("GET", url, **kwargs)
        response.raise_for_status()
        return response.json(), response.headers

    def post_json(self, url: str, json_body: Any, **kwargs: Any) -> Any:
        response = self.request("POST", url, json_body=json_body, **kwargs)
        response.raise_for_status()
        return response.json()

    def post_form(self, url: str, form_body: dict[str, Any], **kwargs: Any) -> Any:
        """POST an `application/x-www-form-urlencoded` body, returning JSON.

        For ICE GreenTrace, whose report API is a JSON service that reads its
        criteria from a form body and nothing else: the same criteria sent as a
        query string or as JSON both return HTTP 500. The form body is part of
        the cache key, because it carries the paging offset and the ledger
        selector — two calls to one URL returning different ledgers is the
        `standardid` trap in another costume.
        """
        response = self.request("POST", url, form_body=form_body, **kwargs)
        response.raise_for_status()
        return response.json()
