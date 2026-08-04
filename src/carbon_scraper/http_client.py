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
    method: str, url: str, body: Any, headers: Any = None
) -> str:
    identity = {
        name: value
        for name, value in (headers or {}).items()
        if name.lower() in IDENTITY_HEADERS
    }
    raw = json.dumps(
        {"m": method.upper(), "u": url, "b": body, "h": identity},
        sort_keys=True,
        default=str,
    )
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
    """Raised for status codes worth retrying (429 / 5xx)."""


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
    ) -> None:
        settings.ensure_dirs()
        self._cancel = cancel
        self._limiter = RateLimiter(settings.REQUESTS_PER_SECOND, cancel)
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

        response = self._send(method, url, json_body, params, headers)

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
        )
        _cache_path(key).unlink(missing_ok=True)

    def _send(
        self,
        method: str,
        url: str,
        json_body: Any,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        """Send with backoff, built per call rather than at import time.

        A `@retry` decorator would freeze `settings.MAX_RETRIES` at import and
        sleep uninterruptibly; `Retrying` here reads the setting per call and
        takes our cancellable sleep. `Cancelled` is deliberately not in the
        retryable set, so it propagates out of the loop at once.
        """
        retrying = Retrying(
            stop=stop_after_attempt(settings.MAX_RETRIES),
            wait=wait_exponential(multiplier=1.5, min=2, max=45),
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
            reraise=True,
            sleep=lambda seconds: _sleep(seconds, self._cancel),
        )
        return retrying(self._send_once, method, url, json_body, params, headers)

    def _send_once(
        self,
        method: str,
        url: str,
        json_body: Any,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        self.raise_if_cancelled()
        self._limiter.wait()
        # httpx.Headers, not a dict: header names are case-insensitive, and a
        # plain dict merge keeps BOTH `origin` (lowercased on the client) and
        # an adapter's `Origin`, sending two conflicting values. Cercarbono
        # rejects Verra's Origin with a generic HTTP 500, so an adapter that
        # overrides a shared header has to actually override it.
        merged = httpx.Headers(self._client.headers)
        merged.update(headers or {})
        response = self._client.request(
            method.upper(), url, json=json_body, params=params, headers=merged
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableStatus(f"{response.status_code} from {url}")
        return response

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.request("GET", url, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        """GET returning the body as text.

        For the registries that never learned to speak JSON: the legacy Markit
        public view is server-rendered JSP, so its adapter parses HTML. The
        cache stores the body as text either way, so this costs nothing extra.
        """
        response = self.request("GET", url, **kwargs)
        response.raise_for_status()
        return response.text

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
