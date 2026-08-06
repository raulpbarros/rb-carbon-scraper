"""The ICE GreenTrace platform: report-by-URL paging, shared by its tenants.

Measured against the live service on 2026-08-04; see docs/api-contract-acr.md.

GreenTrace is **a platform, not a registry** — the fourth one here, after S&P
Platts, legacy Markit and EcoRegistry. Its home page offers two tenants, ACR
and ART (Architecture for REDD+ Transactions), and they are the same API with
one path segment changed. Everything measured about the platform lives here;
`registries/acr/api.py` is identity plus the fields ACR actually populates.

The site is an ICE CMS app whose tables are `cms-component-ice-report-center`
instances. Each is configured with a `reportUrl`, and the component's own
bundle publishes the whole contract:

    GET  {reportUrl}/criteria                  the filters, with their values
    POST {reportUrl}/results                   application/x-www-form-urlencoded
                                               offset, pageNumber, max, + criteria

Four things that will cost an afternoon if skipped:

* **The `reportUrl` is not an endpoint.** A GET on it answers HTTP 500
  `No static resource v1/project/registry/ACR_REGISTRY/project-summaries.`,
  which reads like a broken service and is a path that was never mapped. Only
  `/results` and `/criteria` exist.
* **The criteria must be a form body.** The identical values sent as a query
  string, or as JSON, both return that same generic 500. `http_client` sets
  the content type for a form post, because the shared client header says
  `application/json` and wins over the one httpx would infer.
* **`max` clamps at 2000 in silence.** Asking 20000 of a 3,358-row ledger
  returns 2000 rows under a `totalCount` of 3358, at HTTP 200 — the fifth
  registry here to ignore a page size. So paging advances on `offset` against
  the stated `totalCount`, never on the row count that came back.
* **The dataset key changes with the filter.** One URL serves four ledgers
  selected by `holdingStatus`, and each answers under a different key —
  `holdings`, `issuedCredits`, `retiredCredits`, `cancelledCredits`. Reading a
  fixed key would silently yield nothing the day a filter value changes, so
  the caller states which key it expects and this module raises if the
  response does not carry it.

And one thing that is *not* a trap: **an unknown filter value narrows to
nothing rather than being ignored.** `holdingStatus=BOGUS` returns 0 rows, not
the whole index. That is the opposite of Verra and Gold Standard, and it fails
in the safe direction — a mistake here loses rows loudly instead of returning
every ledger merged together.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from ... import settings
from ...http_client import RegistryClient, RetryableStatus
from ..base import ClientOwner

log = logging.getLogger(__name__)


class GreenTraceBlocked(RuntimeError):
    """The platform has stopped answering this client, and said why badly.

    Measured 2026-08-04: after a sustained read — one request every seven
    seconds, several hundred of them — **every** API route began answering
    `401 {"message": "Invalid API Key"}`, including the report endpoints that
    had just served hundreds of pages. The site's own page is byte-identical
    and still carries no key of any kind, so this is not a contract that
    changed; it is the platform declining to answer us. Its earlier, softer
    form is a 429 with `Retry-After: 3600`.

    It clears on its own. A stopped sync is repaired by re-running it: every
    write is an idempotent upsert and the response cache keeps what was
    already fetched, so the next run resumes rather than restarting.

    Raised as itself so a caller does not read "401 Unauthorized" as "we need
    credentials" and go looking for a key that does not exist.
    """


def _blocked(exc: httpx.HTTPStatusError | RetryableStatus) -> Exception:
    """Turn GreenTrace's refusals into something that says what happened.

    Both exception types have to be read, and the 429 is why. `http_client`
    raises `RetryableStatus` for a 429 *before* the response ever reaches
    `raise_for_status()`, so the rate-limit refusal — this registry's most
    common one, and the one it documents with `Retry-After: 3600` — never
    arrives as an `HTTPStatusError`. Catching only that left the whole 429 arm
    below unreachable, and a Cloudflare block surfaced as a bare
    `RetryableStatus: 429 from …`: a traceback in the GUI's error log rather
    than the message saying the run is safe to resume from the cache.

    A `RetryableStatus` that is not a 429 is a 5xx — the platform's generic
    fault, which an unmapped path and a bad body both produce — and is
    returned unchanged.
    """
    if isinstance(exc, RetryableStatus):
        status, body = exc.status, str(exc)
    else:
        status, body = exc.response.status_code, exc.response.text[:120].strip()
    if status not in (401, 403, 429):
        return exc
    return GreenTraceBlocked(
        f"ICE GreenTrace refused this client with HTTP {status} "
        f"({body!r}). Its public API needs no key — "
        f"the site's own page sends none — so this is a block, not a missing "
        f"credential, and it clears with time. Re-run the sync later: the "
        f"response cache keeps what was already fetched and every write is an "
        f"idempotent upsert. Do not raise the request rate to compensate."
    )


class GreenTraceAPI(ClientOwner):
    """Report paging for one GreenTrace tenant.

    A subclass sets `registry`, `registry_key` and its ledger set. Nothing
    else about the platform is worth restating per tenant.
    """

    #: db `registry` column value; set by the subclass.
    registry: str = ""

    #: The tenant's key in every path — `ACR_REGISTRY`, `ART_REGISTRY`.
    registry_key: str = ""

    def __init__(
        self,
        client: RegistryClient | None = None,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self._bind_client(client, cancel)
        #: (report path, criteria) -> every row of it, fetched once per run.
        self._records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        #: The same key -> the `totalCount` the platform stated while paging.
        self._totals: dict[tuple[str, str], int] = {}

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """What the site's own page sends, and nothing more.

        `settings.BROWSER_HEADERS` carries Verra's `Origin`, and sending one
        registry's origin to another has already cost an afternoon on
        Cercarbono. GreenTrace does not check it today — its CORS preflight
        allows the site's own origin and it answers an anonymous request
        regardless — but sending another registry's is wrong whether or not it
        is noticed.
        """
        headers = dict(settings.BROWSER_HEADERS)
        headers["Origin"] = settings.GREENTRACE_SITE
        headers["Referer"] = f"{settings.GREENTRACE_SITE}/"
        return headers

    def report_url(self, path: str) -> str:
        """The configured `reportUrl` for one of this tenant's tables."""
        return f"{settings.GREENTRACE_API}/{path.format(registry=self.registry_key)}"

    def _post(self, path: str, form: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.report_url(path)}/{settings.GREENTRACE_REPORT_RESULTS}"
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            body = self.client.post_form(url, form, headers=headers)
        except (httpx.HTTPStatusError, RetryableStatus) as exc:
            raise _blocked(exc) from exc
        if not isinstance(body, dict) or not isinstance(body.get("datasets"), dict):
            raise ValueError(f"{path} returned no datasets: {str(body)[:200]!r}")
        return body

    def _page(
        self,
        path: str,
        dataset: str,
        criteria: dict[str, Any],
        offset: int,
        page_size: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page, and the total the platform states for the whole report.

        `pageNumber` is sent beside `offset` because the site sends both, and
        a report API that reads one and ignores the other is not a thing worth
        discovering halfway through a 10,724-row ledger. Which means the two
        must describe **the same window**, and deriving the page number from
        the size we *asked* for does not: `max` is clamped in silence, so a
        platform handing over 2000 rows for a requested 20000 leaves `offset`
        walking 0, 2000, 4000 while `pageNumber` stays at 1 for the whole
        ledger. `page_size` is therefore the size the platform is actually
        delivering, learned from the first page by `fetch`.

        There is no sort key here, and none is published: `/criteria` offers
        filters only. Verra lost 1,271 of 5,244 projects to exactly that, so
        it is worth knowing that this platform's own order held across every
        page of all four reports on 2026-08-04 — and that the guard is
        `iter_projects` comparing key *sets* rather than a row count, which is
        the only thing that can see a reorder. See docs/api-contract-acr.md.
        """
        stride = page_size or settings.GREENTRACE_PAGE_SIZE
        form = {
            "offset": offset,
            "pageNumber": offset // stride + 1,
            "max": settings.GREENTRACE_PAGE_SIZE,
            **{k: v for k, v in criteria.items() if v is not None},
        }
        datasets = self._post(path, form)["datasets"]
        if dataset not in datasets:
            # The key is chosen by the filter, so a missing one means the
            # criteria selected a different report — not an empty page.
            raise ValueError(
                f"{path} answered with {sorted(datasets)} rather than "
                f"{dataset!r}; the criteria {criteria!r} selected another "
                f"dataset."
            )
        block = datasets[dataset] or {}
        rows = [row for row in (block.get("rows") or []) if isinstance(row, dict)]
        total = block.get("totalCount")
        return rows, int(total) if total is not None else len(rows)

    def fetch(
        self,
        path: str,
        dataset: str,
        criteria: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Every row of one report, paged on `offset` against `totalCount`.

        Never on "a short page ends it": `max` is clamped to 2000 without a
        marker, so a full page and a clamped one look identical. The stated
        total is the only thing that knows where the end is, and a page that
        returns nothing while the total promises more stops the loop and logs
        rather than spinning.
        """
        criteria = criteria or {}
        key = (path, repr(sorted(criteria.items())))
        if key in self._records:
            return self._records[key]

        rows: list[dict[str, Any]] = []
        total = 0
        offset = 0
        # How many rows the platform actually hands over, learned from the
        # first page rather than assumed from `max`. It is what keeps
        # `pageNumber` describing the same window as `offset` — see `_page`.
        page_size = 0
        while True:
            page, total = self._page(path, dataset, criteria, offset, page_size)
            rows.extend(page)
            self._totals[key] = total
            page_size = page_size or len(page)
            if len(rows) >= total:
                break
            if not page:
                log.error(
                    "INCOMPLETE: %s returned an empty page at offset %s of %s.",
                    dataset,
                    offset,
                    total,
                )
                break
            if len(page) != page_size:
                # Not fatal — `offset` is what this pager advances on and it
                # stays right. But the two fields can no longer agree, and if
                # the platform reads `pageNumber` this is where rows start
                # being skipped or read twice, silently.
                log.error(
                    "%s returned %s rows at offset %s where every earlier page "
                    "returned %s; `offset` and `pageNumber` can no longer "
                    "describe the same window.",
                    dataset,
                    len(page),
                    offset,
                    page_size,
                )
            offset += len(page)

        self._records[key] = rows
        return rows

    def total(self, path: str, dataset: str, criteria: dict[str, Any] | None = None) -> int:
        """What the platform says the report holds.

        Fetches the report rather than peeking at one page, because a run asks
        for the count and then iterates: peeking would re-request the first
        page a moment later, and here that is a 16 MB download and seven
        seconds of a rate limit that bans for an hour when it is crossed.
        """
        criteria = criteria or {}
        key = (path, repr(sorted(criteria.items())))
        if key not in self._totals:
            self.fetch(path, dataset, criteria)
        return self._totals.get(key, 0)

    def criteria(self, path: str) -> list[dict[str, Any]]:
        """The filters a report accepts, as the platform publishes them.

        Not used by a sync. It is how a filter value is *checked* rather than
        guessed — `holdingStatus` takes ISSUED / RETIRED / CANCELED and those
        four strings are published here, beside their labels.
        """
        url = f"{self.report_url(path)}/{settings.GREENTRACE_REPORT_CRITERIA}"
        body = self.client.get_json(url, headers=self._headers())
        return [item for item in (body or []) if isinstance(item, dict)]

    def detail(self, project_key: str) -> dict[str, Any]:
        """One project's detail record, which the list does not carry.

        **Keyed on the project key (`P2423FTH4Z22`), not the reference id.**
        `…/project/ACR1275` is a 404 here — while the *page* at the same shape
        of URL answers HTTP 200 with a shell that renders an error, which is
        why a row's link is built from the key too.
        """
        url = (
            f"{settings.GREENTRACE_API}/project/registry/{self.registry_key}"
            f"/project/{project_key}"
        )
        try:
            body = self.client.get_json(url, headers=self._headers())
        except (httpx.HTTPStatusError, RetryableStatus) as exc:
            raise _blocked(exc) from exc
        detail = (body or {}).get("projectDetail") if isinstance(body, dict) else None
        return detail if isinstance(detail, dict) else {}
