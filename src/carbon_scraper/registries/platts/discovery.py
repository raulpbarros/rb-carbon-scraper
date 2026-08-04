"""Discover a Platts registry's real API contract by watching a real browser.

The S&P registries are undocumented React apps. Rather than guessing request
shapes, we drive one public page with Playwright, record every call it makes to
the API host, and write down what we saw.

Output, per registry (see `settings.api_contract_paths`):
  config/api-contract*.json      machine-readable
  docs/api-contract*.md          human-readable, for whoever debugs this next
  tests/fixtures/<slug>-*.json   recorded responses, used by the offline tests

Re-run this whenever a scraper starts failing: it is the diagnostic, not the
data path. Everything that actually scrapes uses plain `httpx`.

`standardsByRegistry` below is the cheaper half of the same job. It is
unauthenticated and answers in one GET, so a new registry on this platform can
usually be onboarded without opening a browser at all — the browser run is for
confirming which ledgers the public page really queries.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ... import settings

log = logging.getLogger(__name__)

# Heuristics for labelling a recorded call. Ordered: first match wins.
_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("project_search", re.compile(r"/project/publicReportPageSearch|/project/\w*[Ss]earch", re.I)),
    ("project_detail", re.compile(r"project.*(getbyid|detail)|/project/\d+$", re.I)),
    (
        "units_search",
        re.compile(
            r"/(issuances|retirements|cancellations|holdings|units?|credits?)/", re.I
        ),
    ),
    # Checked after the searches: `publicReportPageSearch` contains "report",
    # so an eager export pattern would swallow every search call.
    ("export", re.compile(r"(export|download|\.csv|\.xlsx|excel)", re.I)),
    ("lookup", re.compile(r"(lookup|static|reference|dropdown|translation|schema)", re.I)),
]


def classify(url: str, body: str | None) -> str:
    haystack = f"{url} {body or ''}"
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(haystack):
            return role
    return "other"


@dataclass
class RecordedCall:
    method: str
    url: str
    role: str
    request_headers: dict[str, str]
    request_body: Any = None
    status: int | None = None
    response_sample: Any = None
    response_is_json: bool = False
    total_hint: int | None = None
    item_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "role": self.role,
            "request_headers": self.request_headers,
            "request_body": self.request_body,
            "status": self.status,
            "response_is_json": self.response_is_json,
            "total_hint": self.total_hint,
            "item_count": self.item_count,
            "response_sample": self.response_sample,
        }


@dataclass
class DiscoveryResult:
    captured_at: str
    registry: str = settings.VERRA
    slug: str = "verra"
    label: str = "Verra"
    calls: list[RecordedCall] = field(default_factory=list)

    def by_role(self, role: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.role == role and (c.status or 0) < 400]

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "registry": self.registry,
            "api_host": settings.API_HOST,
            "calls": [c.to_dict() for c in self.calls],
        }


# -- response shape helpers -------------------------------------------------

_TOTAL_KEYS = (
    "totalEntities",  # what this registry actually uses
    "totalRecords",
    "totalCount",
    "totalElements",
    "total",
    "recordCount",
    "count",
)
_LIST_KEYS = (
    "entities",  # what this registry actually uses
    "value",
    "content",
    "data",
    "records",
    "items",
    "results",
    "rows",
    "hits",
)


def find_total(payload: Any) -> int | None:
    """Locate a total-record count anywhere in a nested response."""
    if isinstance(payload, dict):
        for key in _TOTAL_KEYS:
            value = payload.get(key)
            if isinstance(value, int):
                return value
        for value in payload.values():
            found = find_total(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload[:3]:
            found = find_total(item)
            if found is not None:
                return found
    return None


def find_records(payload: Any) -> list[dict[str, Any]] | None:
    """Locate the list of records inside a response of unknown shape."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)] or None
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        # Elasticsearch: {"hits": {"hits": [{"_source": {...}}]}}
        hits = payload.get("hits")
        if isinstance(hits, dict):
            inner = hits.get("hits")
            if isinstance(inner, list) and inner:
                sources = [h.get("_source", h) for h in inner if isinstance(h, dict)]
                if sources:
                    return sources
        for value in payload.values():
            found = find_records(value)
            if found:
                return found
    return None


def _truncate(payload: Any, max_items: int = 2) -> Any:
    """Keep a representative slice so the contract file stays readable."""
    if isinstance(payload, list):
        return [_truncate(x, max_items) for x in payload[:max_items]]
    if isinstance(payload, dict):
        return {k: _truncate(v, max_items) for k, v in payload.items()}
    if isinstance(payload, str) and len(payload) > 400:
        return payload[:400] + "…"
    return payload


# -- picking a target -------------------------------------------------------


def target(registry: str = settings.VERRA) -> type:
    """The adapter class for `registry`, which carries the URLs to drive.

    Everything `discover` needs about a registry — public page, detail page,
    slug, sample project — lives on its adapter, so there is exactly one place
    to change when S&P moves a page.
    """
    from .. import adapter_class
    from .api import PlattsAPI

    cls = adapter_class(registry)
    if not issubclass(cls, PlattsAPI):
        raise ValueError(
            f"{registry} is not on the S&P Platts platform, and `discover` only "
            f"applies there. A registry with a plain REST API is contracted by "
            f"hand instead — see docs/api-contract-gs.md."
        )
    return cls


#: A bare platform tenant code, as the app's own bundle spells them: `UKLR`,
#: `OxCP`. Deliberately narrow — it is checked only after the adapter lookup
#: has failed, so it must not swallow a mistyped registry name and turn it
#: into a request.
_TENANT_CODE = re.compile(r"[A-Za-z]{2,12}")


def standards_target(registry: str) -> tuple[str, str]:
    """`(registry_code, config_url)` for the standards lookup.

    Two kinds of argument, and the difference matters:

    * **A registry we have an adapter for** (`verra`, `planvivo`) — its own
      code and its own routing table, so nothing about it is assumed.
    * **A bare tenant code** we have no adapter for (`UKLR`, `GCC`, ...).
      Naming one of those is the entire point of this command: the lookup is
      unauthenticated, costs one GET, and is the only published source of a
      registry's real `standardId`. `settings.PLATTS_TENANT_CODES` lists the
      ones the app declares, but any code is allowed — the table is a
      convenience, not a gate.

    A bare code reads the multi-registry app's routing table, because
    `registry.spglobal.com` is the site that serves every tenant without a
    domain of its own. Verra has one, which is why its adapter carries a
    different `config_url` and why that is read from the class rather than
    assumed here.
    """
    from .. import adapter_class

    try:
        cls = adapter_class(registry)
    except ValueError:
        code = registry.strip()
        if not _TENANT_CODE.fullmatch(code):
            raise ValueError(
                f"{registry!r} is neither a registry we have an adapter for nor "
                f"a plausible S&P tenant code. Known codes: "
                f"{', '.join(settings.PLATTS_TENANT_CODES)}"
            ) from None
        # Sent exactly as typed. The app spells its codes in mixed case
        # (`OxCP`) and nothing says the lookup is case-insensitive.
        return code, settings.PV_ENVIRONMENT_CONFIG_URL

    from .api import PlattsAPI

    if not issubclass(cls, PlattsAPI):
        raise ValueError(
            f"{registry} is not on the S&P Platts platform, so it publishes no "
            f"`standardId`. That lookup is the platform's, not a general one."
        )
    return cls.registry_code, cls.config_url


# -- the capture itself -----------------------------------------------------

def discover(
    registry: str = settings.VERRA,
    *,
    headless: bool = True,
    timeout_ms: int = 90_000,
) -> DiscoveryResult:
    """Drive one registry's public page and record its API traffic."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - dev dependency
        raise RuntimeError(
            "Playwright is required for `verra discover`. "
            'Install with: pip install -e ".[dev]" && playwright install chromium'
        ) from exc

    cls = target(registry)
    settings.ensure_dirs()
    result = DiscoveryResult(
        captured_at=datetime.now(timezone.utc).isoformat(),
        registry=cls.registry,
        slug=cls.slug,
        label=settings.REGISTRY_LABELS.get(cls.registry, cls.registry),
    )
    pending: dict[str, RecordedCall] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        def on_request(request: Any) -> None:
            if settings.API_HOST not in request.url:
                return
            body = request.post_data
            parsed: Any = body
            if body:
                try:
                    parsed = json.loads(body)
                except (ValueError, TypeError):
                    parsed = body
            pending[request.url + request.method] = RecordedCall(
                method=request.method,
                url=request.url,
                role=classify(request.url, body),
                request_headers=dict(request.headers),
                request_body=parsed,
            )

        def on_response(response: Any) -> None:
            if settings.API_HOST not in response.url:
                return
            call = pending.get(response.url + response.request.method)
            if call is None:
                return
            call.status = response.status
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 - non-JSON (csv/xlsx) is fine
                call.response_sample = f"<non-JSON {response.headers.get('content-type', '')}>"
                return
            call.response_is_json = True
            call.total_hint = find_total(payload)
            records = find_records(payload)
            call.item_count = len(records) if records else None
            call.response_sample = _truncate(payload)
            # Outside the handler above: an OSError writing the fixture (a
            # read-only checkout, a long path) would otherwise relabel a
            # perfectly good JSON call as non-JSON and throw away its sample
            # and its total_hint — the two things the capture exists for.
            try:
                _save_fixture(cls.slug, call, payload)
            except OSError as exc:
                log.warning("Could not save a fixture for %s: %s", call.url, exc)

        page.on("request", on_request)
        page.on("response", on_response)

        log.info("Opening %s", cls.public_search_url)
        page.goto(cls.public_search_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(20_000)  # the SPA boots slowly, then fires its searches

        _try_open_reports(page)
        _try_paginate(page)
        _try_open_first_project(page, cls)

        page.wait_for_timeout(4_000)
        context.close()
        browser.close()

    result.calls = list(pending.values())
    return result


def _try_paginate(page: Any) -> None:
    """Click to page 2 so we capture the pagination parameters."""
    for selector in (
        "button[aria-label='Next page']",
        "button[aria-label='Next Page']",
        "[class*='pagination'] button:has-text('2')",
        "li[title='Next Page']",
        "text=Next",
    ):
        try:
            element = page.locator(selector).first
            if element.count() and element.is_enabled(timeout=2_000):
                element.click(timeout=5_000)
                page.wait_for_timeout(6_000)
                log.info("Paginated via %s", selector)
                return
        except Exception:  # noqa: BLE001 - selectors are best-effort
            continue
    log.info("Could not paginate; pagination params may be missing from the contract.")


def _try_open_first_project(page: Any, cls: type) -> None:
    """Open a project so we capture the detail and Units calls."""
    sample = getattr(cls, "sample_project_id", None)
    if not sample:
        log.info("No sample project id for %s; skipping the detail page.", cls.registry)
        return
    try:
        page.goto(
            cls.detail_url_template.format(project_id=sample),
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(10_000)
    except Exception as exc:  # noqa: BLE001
        log.info("Project detail navigation failed: %s", exc)
        return

    # The Units/VCU ledger is usually behind a tab.
    for label in ("Units", "VCUs", "Credits", "Issuances", "Retirements"):
        try:
            tab = page.get_by_text(label, exact=False).first
            if tab.count():
                tab.click(timeout=5_000)
                page.wait_for_timeout(6_000)
                log.info("Opened '%s' tab", label)
        except Exception:  # noqa: BLE001
            continue


def _try_open_reports(page: Any) -> None:
    """Open the public reports tables, which is what fires the search calls."""
    for label in ("Projects", "Issuances", "Retirements", "Cancellations"):
        try:
            link = page.get_by_text(label, exact=False).first
            if link.count():
                link.click(timeout=5_000)
                page.wait_for_timeout(8_000)
                log.info("Opened '%s' report", label)
        except Exception:  # noqa: BLE001 - selectors are best-effort
            continue


def _save_fixture(slug: str, call: RecordedCall, payload: Any) -> None:
    """Persist a recorded response so tests can run offline.

    Only responses that actually carry records are useful as fixtures, and the
    richest one wins — otherwise a summary endpoint (`publicProjectsSum`) can
    claim the filename and leave the tests with nothing to parse. The slug
    prefix keeps two registries on the same platform from overwriting each
    other's fixtures, which would make one of the two test suites a lie.
    """
    if call.role in ("other", "lookup") or not call.item_count:
        return
    path = settings.FIXTURES_DIR / f"{slug}-{call.role}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if len(find_records(existing) or []) >= call.item_count:
                return
        except (ValueError, OSError):
            pass
    path.write_text(json.dumps(payload, indent=2)[:2_000_000], encoding="utf-8")
    log.info("Saved fixture %s (%s records)", path.name, call.item_count)


# -- writing the contract ---------------------------------------------------

def save_contract(result: DiscoveryResult) -> tuple[Any, Any]:
    """Write the capture. Returns (markdown_path, json_path)."""
    settings.ensure_dirs()
    markdown_path, json_path = settings.api_contract_paths(result.slug)
    json_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    return markdown_path, json_path


def render_markdown(result: DiscoveryResult) -> str:
    lines = [
        f"# {result.label} registry API contract",
        "",
        "**Generated by `verra discover` — do not edit by hand.**",
        f"Captured: {result.captured_at}",
        "",
        "Recorded by driving the real public registry with a browser and watching",
        f"every request to `{settings.API_HOST}`. Re-run `verra discover` when the",
        "scraper breaks; S&P changes this API without notice.",
        "",
        "## Summary",
        "",
        "| Role | Method | Status | Items | Total | URL |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for call in sorted(result.calls, key=lambda c: (c.role, c.url)):
        url = call.url.replace(f"https://{settings.API_HOST}", "")
        lines.append(
            f"| {call.role} | {call.method} | {call.status} | "
            f"{call.item_count if call.item_count is not None else ''} | "
            f"{call.total_hint if call.total_hint is not None else ''} | `{url}` |"
        )

    lines += ["", "## Calls", ""]
    for call in sorted(result.calls, key=lambda c: (c.role, c.url)):
        lines += [
            f"### `{call.role}` — {call.method} {call.url}",
            "",
            f"- Status: `{call.status}`",
            f"- JSON response: `{call.response_is_json}`",
        ]
        if call.total_hint is not None:
            lines.append(f"- Total records reported: `{call.total_hint}`")
        if call.item_count is not None:
            lines.append(f"- Records in this page: `{call.item_count}`")
        if call.request_body:
            lines += [
                "",
                "Request body:",
                "",
                "```json",
                json.dumps(call.request_body, indent=2)[:4000],
                "```",
            ]
        interesting = {
            k: v
            for k, v in call.request_headers.items()
            if k.lower()
            in {
                "appkey",
                "application",
                "x-xsrf-token",
                "content-type",
                "authorization",
                "registry",
                "standardid",
                "standardacronym",
            }
        }
        if interesting:
            lines += [
                "",
                "Notable request headers:",
                "",
                "```json",
                json.dumps(interesting, indent=2),
                "```",
            ]
        if call.response_sample is not None:
            lines += [
                "",
                "Response sample (truncated):",
                "",
                "```json",
                json.dumps(call.response_sample, indent=2)[:4000],
                "```",
            ]
        lines.append("")
    return "\n".join(lines)


# There is deliberately no `load_contract()` reader. A capture is a document
# for a human to read when the operator changes something, not an input the
# scraper consults: the adapters hold the contract in code, where a test can
# pin it. One existed, unused, and reading like the data path.
