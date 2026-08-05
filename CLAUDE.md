# CLAUDE.md — Carbon Registry Scraper

## Response style

**Always use `/caveman:caveman` in this project.** Invoke the skill at the start of a session and stay in it. Code, commit messages, PR bodies, and security/destructive-action warnings are still written normally — caveman applies to prose replies only.

## What this project is

A scraper that builds a database of every carbon project across public
registries and exports one formatted Excel sheet for the business team.
Eight registries are live: **Verra** (VCS **and** JNR), **Gold Standard**,
**Cercarbono**, **Plan Vivo** (**V5 and V4**, on two different platforms),
**SocialCarbon**, **BioCarbon**, **Puro.earth** and **ACR** (the American
Carbon Registry). That is every registry on the original list.
Adding one means writing an adapter, not touching the pipeline.

**A registry is not always one system, and not always one standard.**
`registries.ADAPTERS` maps a registry to a *tuple* of adapters for exactly that
reason: Plan Vivo publishes V5 on S&P Platts and V4 on the legacy Markit
registry, and Verra publishes VCS and JNR as two standards on one tenant. Both
cases store under one `registry` value, one checkbox, one row set.

It is becoming an **installed Windows application**, not just a CLI: the
business team ticks registries, picks a folder and presses a button. `PLAN.md`
tracks that work phase by phase and is the file to read before starting
anything on it.

Two outputs, deliberately separate:

- `data/verra.db` — SQLite, the source of truth. Every registry in one
  database, keyed by a `registry` column. Full history, provenance, idempotent.
- `out/carbon-projects_vN.xlsx` — the deliverable, regenerated from the DB on
  demand. **Versioned, never overwritten** — see "Delivering the spreadsheet".

The Excel column list is dictated by `assets/fields-asked.txt`. **That file is the schema.** Column order in the export is read from it at runtime — edit the file, not the code, to change the sheet.

## Where files live — get this wrong and the packaged build breaks silently

Paths come in two kinds and must never be mixed up:

- **`settings.RESOURCE_ROOT`** — read-only files that ship with the program
  (`assets/`, the default `config/`). Under PyInstaller this is `sys._MEIPASS`,
  a temp directory wiped when the process exits. **Never write under it.**
- **`settings.USER_ROOT`** — everything we write: the database, the Excel
  deliveries, the ~1 GB response cache, logs, and the user's own editable
  config. Frozen builds put it in `%LOCALAPPDATA%\CarbonRegistryScraper`.

In a development checkout both are the repository root, so the on-disk layout
is unchanged and this split is invisible. That is exactly why it is easy to
break: code that works perfectly in the checkout can write into a temp bundle,
or into Program Files, once frozen.

`ensure_dirs()` creates only the writable tree and calls `seed_user_files()`,
which copies `fields-asked.txt` and `config/derivation/*.yaml` into `USER_ROOT`
on first run and **never overwrites** them afterwards — an installed user keeps
their edits across upgrades, and "edit the file, not the code" stays true for
them. Read editable config through `settings.fields_file()`,
`settings.credits_config()` and `settings.derivation_dir()`, which fall back to
the bundled original; do not read the module constants directly.

Overrides, all honoured at import: `CARBON_HOME` (moves the whole writable
tree), `CARBON_DATA_DIR`, `CARBON_OUT_DIR`, `CARBON_CACHE_DIR`,
`CARBON_LOG_DIR`, `CARBON_CONFIG_DIR`, `CARBON_ASSETS_DIR`, `CARBON_DB`,
`CARBON_XLSX`. The older `VERRA_DB` / `VERRA_XLSX` still work.

## Delivering the spreadsheet — versioned, never overwritten

**Every export writes a NEW version. Never overwrite a delivery.**

`verra export` copies the version history forward: it finds the highest
existing `out/carbon-projects_vN.xlsx`, and writes `_v(N+1)`. The previous
file stays byte-for-byte as it was sent. If no versioned file exists yet but
an older unversioned sheet does (`carbon-projects.xlsx`, `verra-projects.xlsx`),
that one is adopted as `_v1` first, so the history starts from the sheet the
business actually received.

The reason is not tidiness. The business acts on numbers in a specific file;
a silent overwrite makes "the figure you quoted me" unrecoverable. Version
numbers are compared numerically, so `_v10` follows `_v9`, not `_v1`.

Implemented in `excel.next_version_path()`. Do not add a "just overwrite it"
flag.

## Registries

| | Verra VCS | Plan Vivo | Gold Standard | Cercarbono | SocialCarbon | BioCarbon | Puro.earth |
|---|---|---|---|---|---|---|---|
| Front end | `registry.verra.org` | `registry.spglobal.com/pvclimate` | `registry.goldstandard.org` | `registry.cercarbono.com` | `registry.socialcarbon.org` | `globalcarbontrace.io` | `registry.puro.earth` |
| Backend | `prod-us.api.platts.com` (S&P) | **the same** | `public-api.goldstandard.org` | `api-front.ecoregistry.io` | **the same host** — a Bubble.io app | `api.globalcarbontrace.io` — Laravel | **none — there is no API** |
| Shape | POST search, Elasticsearch behind it | **the same** | plain REST GET | plain REST GET | Bubble Data API, `cursor`/`limit` | plain REST GET, Laravel paginator | JSON inside the HTML — a Next.js RSC payload |
| Projects | ~5,200 | **2 — V5 only, see below** | 4,141 | 231 (CO2 standard) | 19 | 105 (GHG programme) | 118 |
| Credit records | ~305k retirements alone | 27 issuances + 10 holdings | ~183k blocks | 2,529 serials + 9,350 retirements | 17 + 81 + 2 | 626 + 11,439 + 3 | 583 + 2,099 bundles |
| Requests per sync | ~9,000 | **7** | ~7,300 | ~234 | **4** | ~225 | ~121 |
| Contract doc | `docs/api-contract.md` | `docs/api-contract-planvivo.md` | `docs/api-contract-gs.md` | `docs/api-contract-cercarbono.md` | `docs/api-contract-socialcarbon.md` | `docs/api-contract-biocarbon.md` | `docs/api-contract-puro.md` |
| `discover` needed | yes | no — the standards lookup was enough | no — plain HTTP was enough | no — plain HTTP plus a bundle read | no — the API is open and self-describing | no — the key and the routes are in the bundle | no — the data ships in the page |

| | ACR |
|---|---|
| Front end | `greentrace.ice.com/acr` |
| Backend | `greentrace.ice.com/api/greentraceservice/v1` — **ICE GreenTrace**, a platform serving ACR and ART |
| Shape | ICE CMS "report centre": `POST {reportUrl}/results`, **form-encoded** |
| Projects | 994 |
| Credit records | 3,358 issuance blocks + 10,724 retirements + 1,358 cancellations |
| Requests per sync | ~1,005 — **at one request per seven seconds**, see below |
| Contract doc | `docs/api-contract-acr.md` |
| `discover` needed | no — the site's own CMS config and one JS chunk are the contract |

All read via
`--registry verra|planvivo|gs|cercarbono|socialcarbon|biocarbon|puro|acr|all`.
Verra
and Plan Vivo share `registries/platts/api.py`; the others have their own
sections and contract docs.

**Two of those registries are scraped by two adapters each**, and the counts
above are only the S&P half:

| | second adapter | adds |
|---|---|---|
| Verra | `verra/jnr.py` — JNR, a second standard on the same S&P tenant | 5 projects, **no credits at all** |
| Plan Vivo | `planvivo/v4.py` — V4, on the **legacy Markit registry** | 30 projects, 411 issuances, 442 holdings, **5,034 retirements** |

Nothing is planned: ACR was the last name on the original list.

**ACR is the only registry here that bans rather than throttles.** GreenTrace
sits behind a Cloudflare rate-limiting rule — roughly 100 requests in ten
minutes earns HTTP 429 with `Retry-After: 3600`, and every retry inside that
hour earns the same. A sync needs ~1,005 requests, because only the
per-project detail carries a crediting period, so it runs at
`settings.ACR_REQUESTS_PER_SECOND` (one request per seven seconds, ~2 hours).
An adapter may declare `requests_per_second` and `RegistryClient` takes the
**minimum** of that and the global setting: it is a lever for going slower and
cannot be used to go faster. A 429's own `Retry-After` is honoured up to
`settings.MAX_RETRY_AFTER`, past which the run fails and is resumed from the
cache — which is safe, because every write is an idempotent upsert.

**And there is a second, harder refusal behind the 429.** After a few hundred
requests even at one every seven seconds, every API route starts answering
`401 {"message": "Invalid API Key"}` — while the site's own page stays
byte-identical and still ships no key of any kind. It is not a credential we
are missing and not a contract that changed; it is the platform declining to
answer this client, and it clears on its own. `greentrace.GreenTraceBlocked`
says so in the exception, because "401 Unauthorized" sends the next person
hunting for a key that does not exist. **A full ACR sync may therefore take
more than one sitting** — re-running continues from the response cache. The
answers this does *not* have are a higher rate and a different address.

### Adding a registry hosted on S&P Platts

Verra and Plan Vivo are two tenants of one platform, and there are more. The
app at `registry.spglobal.com` serves them off the identical backend, and its
`app.js` publishes the table: `VERRA`, `UKLR`, `RAAS`, `PVCL` (Plan Vivo),
`OxCP`, `KRR`, `GCC`, `BCCR`. For any of them the recipe is a header change and
a subclass, not a new scraper:

1. **`verra standards -r <name>`** — or the raw
   `GET {cmsResources}/public/standardsByRegistry/<CODE>`. Unauthenticated, one
   GET, and the **only published source of the real `standardId`**. It also
   returns `publicReportExport`, which is how the new adapter's ledger set is
   decided rather than copied from Verra's.
2. `GET https://<site>/config/environment.config.json` — the routing table,
   including the public `appkey`. Read per registry, not shared: they are
   byte-identical today and that is not a guarantee.
3. Subclass `PlattsAPI` with `registry_code`, `standard_id`,
   `standard_acronym`, `site`, `config_url` and `detail_url_template`. Nothing
   else. Everything measured about the platform is inherited.
4. **Check which fields the registry actually populates before reusing Verra's
   column map.** Plan Vivo needed two changed and one dropped — see below.

**Never guess a `standardId`.** A wrong-but-plausible one returns **HTTP 200
with `totalEntities: 0`**, not an error: no exception, no rows, nothing in the
log — indistinguishable from a registry with no projects. Omitting the headers
entirely returns the familiar generic 500, which is at least visible.

**`BCCR` is the "BC Carbon Registry" — British Columbia, not BioCarbon.** The
plan assumed otherwise and the standards lookup settled it. BioCarbon still
needs its own adapter.

**Before writing any new adapter, check whether the registry is on this
platform.** It is the difference between a day's work and an afternoon's.

## The Verra target: read this before touching the network code

Verra's registry runs on **S&P Global's "Carbon Registry" (Platts) platform** — a React micro-frontend app on CloudFront, Okta-authenticated. It is not a server-rendered site and it is not the registry most online tutorials describe.

**Trap: `https://registry.verra.org/uiapi/...` is dead.** Every older scraper, blog post, and StackOverflow answer uses those endpoints. They now return the SPA HTML shell with HTTP 200 for any path — so a naive scraper "succeeds" and parses garbage. If you find yourself writing `uiapi`, stop.

Facts established by inspecting the live app:

- Real backend base: `https://prod-us.api.platts.com/ci-raas-prod/`
- The app publishes its own routing table, unauthenticated:
  - `https://registry.verra.org/config/environment.config.json` — service base URIs and `DEFAULT_HEADERS`
  - `https://registry.verra.org/config/endpoints.json` — logical names like `projectManager.getById`
- URL resolution is `uris[<manager>] + "/" + <method>`.
- **Auth boundary (measured, not guessed):** `raas-project-api/project-manager/*` and `br-reg/rest/lookup-manager/*` return `401`. `raas-report-api/es/public/**` is auth-exempt — it reaches the service and returns an application-level `500` on a bad body instead of `401`. Public data flows through there.
- The public bundle contains `text/csv`, `.xlsx` and `application/vnd.ms-excel` — a server-side bulk export exists and is preferred over pagination when available.
- No `robots.txt` is served. The data is public-by-design and the `appkey` in the config is a public client key shipped to every anonymous visitor. We send exactly what the browser sends and nothing more. We do not attempt to reach authenticated endpoints.

Base URIs are read from the live config at runtime rather than hardcoded, so an S&P backend move is absorbed automatically.

## Architecture

One pipeline, one adapter per registry, two front ends:

```
cli.py (Typer) ─┐
                ├─► pipeline.py ─► registries/<name>/api.py ─► db ─► derive ─► excel
gui/app.py    ──┘
```

An adapter normalises its registry's JSON onto the shared column names in
`db.PROJECT_FIELDS` / `db.CREDIT_EVENT_FIELDS` and yields those. **The database
never learns a registry's own field names.** `pipeline` only ever talks to
`registries.base.RegistryAdapter`, so a new registry is one new adapter and
zero changes to db, derive or excel.

`pipeline.py` holds the orchestration and has no opinion about display. Two
seams make it drivable from either front end:

- **`ProgressSink`** — where "scraping projects, 4,120 of 5,244" goes. The CLI
  passes a rich console sink, the GUI a queue sink, tests the null sink.
  Adapters call their `progress` callback **once per record** (~183k times on a
  full Gold Standard sync); `pipeline._Throttle` rate-limits that centrally so
  no sink can forget to. **`done` is a cumulative position, never `1`** — both
  sinks read it against a total (`done * 100 / total` in the window), so a
  per-record `1` pins the bar at "1 of N" for a whole scrape. Go through
  `base.reconciled()` and it cannot be got wrong; `conftest.RecordingProgress`
  asserts it.
- **`cancel`** — a `threading.Event` threaded down to `http_client`, checked
  between requests and slept on during retry backoff. Cancelling is safe at any
  point: every write is an idempotent upsert, so a stopped sync is repaired by
  re-running it.

**The GUI must never call the Typer command functions.** They carry `OptionInfo`
defaults; calling them from anything but Typer works by accident and breaks the
moment a signature changes. Both front ends call `pipeline`. A test enforces it
rather than trusting anyone to remember.

A registry filter is `None`, a name, **or a sequence** — the GUI's checkboxes
select registries for both of its buttons, so `pipeline.selected/only`,
`db.all_projects` and `excel.build_rows` all take a set. `db.registry_clause`
is where the meanings are pinned: `None` is every registry, and an **empty
sequence is no rows, never everything**. Falling back to "all" on an empty
selection would make an untick silently export the whole database — no error,
plausible output, wrong scope, which is exactly the shape of the ignored-filter
bug this codebase keeps meeting at the registries.

`registries.ADAPTERS` maps registry identifier → a lazy importer. It is a table
rather than a chain of `if`s because the old fall-through returned the Gold
Standard adapter for any registry it did not recognise — so a registry added to
`ALIASES` but forgotten in the dispatch scraped Gold Standard and filed the
rows under the new registry's name, with no error anywhere.

Because registries number their projects independently — Verra 1890 and Gold
Standard 1890 are different projects — `registry` is part of the primary key
of every table. `db.migrate()` rebuilds a pre-Gold-Standard database in place,
backfilling `registry='VERRA'`; it runs on connect and is a no-op afterwards.

The S&P registries additionally have a discovery phase, because their API is
undocumented:

1. **`verra discover -r verra|planvivo`** — Playwright drives the real public page and records every request to `prod-us.api.platts.com`. Writes a capture under `docs/` and saves responses to `tests/fixtures/<slug>-*.json`. Run this once up front, and again whenever the scraper breaks.
2. **`verra standards -r <name>`** — the cheap half of the same job, and usually enough on its own. One unauthenticated GET for the real `standardId` and the public ledger set. Plan Vivo needed nothing more.
3. **Everything else uses plain `httpx`** against the contract discovery found.

Do not turn this into a browser-driven scraper. Playwright is the diagnostic, not the data path. Gold Standard and Cercarbono never needed it — plain HTTP was enough.

Captures are named per registry so two tenants of one platform cannot overwrite
each other: `settings.api_contract_paths(slug)` keeps Verra's original
filenames and gives everyone else a `-capture` suffix, so a generated file
never lands on top of a hand-written contract doc.

### The confirmed Platts contract (Verra and Plan Vivo)

```
POST {raasReportPublicManager}/{resource}/publicReportPageSearch
{"searchFilter": {"pagination": {"start": 0, "limit": 400,
                                 "sortOptions": [{"sort": "entityId", "dir": "ASC"}]},
                  "filterModel": {}}}
```

`resource` is one of `project`, `issuances`, `holdings`, `retirements`,
`cancellations`. Four constraints, all measured — do not "optimise" past them:

- The headers `registry: VERRA`, `standardid`, `standardacronym: VCS` and
  `language: en` are **required**. Without them every call returns a generic
  HTTP 500 that looks like a server fault but is not. This is the single
  easiest way to waste an afternoon here.
- `limit` maxes out at **400**; 500 fails.
- Elasticsearch caps `start` at **10000**. Larger resources must be split into
  partitions of <10k hits (`PARTITION_KEYS` in `api.py`).
- Paging **must** sort by `entityId`. Without a stable sort Elasticsearch
  reorders between pages and silently drops records — an early run lost 1,271
  of 5,244 projects exactly this way. `api.py` reconciles every partition
  against its expected count and logs `INCOMPLETE` if they disagree.

### The worst trap: filters that are silently ignored

**The API returns the ENTIRE index for any filter it does not understand.** It
does not error. `{"filterType": "Text", "type": "blank"}` on `vintage` returned
305,146 rows — everything.

The consequence is nasty: a partition built on an ignored filter overlaps every
other partition, then gets truncated at the 10k window. You get duplicates
*and* missing records at once, with no error anywhere. The first full run
yielded 401,437 retirement records of which only 266,622 were unique, against a
true total of 305,144.

Rules that follow:

- Never partition on a filter shape that has not been proven to narrow. The
  guard in `_partitions` aborts a split when any child's count is not smaller
  than its parent.
- Proven-good filters: `Text/equals`, `Number/equals`. Proven-bad: `Date/inRange`
  (ignored), `Text/blank` (ignored), `Number/inRange` on `projectId` (mapped as
  text, matches nothing sensible).
- **Always reconcile against the registry's own count.** Never trust a row
  count just because the run finished without an exception.

**This generalises, and every registry so far has confirmed it.** Every one of
these was a real afternoon:

| Registry | Looks like | Actually is |
|---|---|---|
| Verra | filters applied, sensible row count | unknown filters ignored, whole index returned |
| Gold Standard | Cloudflare 403 — "this API is private" | public; it just wants a browser `User-Agent` |
| Cercarbono / EcoRegistry | `ERROR_401 "No autorizado"`, at **HTTP 200** | public; it just wants `platform: ecoregistry` |
| Cercarbono / EcoRegistry | generic HTTP 500 | a CORS check: we were sending Verra's `Origin` |
| Cercarbono | 2,529 issuance serials, run finished clean | one project publishes its serials twice, another two are missing entirely |
| Plan Vivo | HTTP 200, `totalEntities: 0` — an empty registry | a wrong `standardId`; the right one returns rows |
| Plan Vivo | `unitType` on every issuance | it repeats across reserve and non-reserve rows; `unitClass` is what distinguishes them |
| Plan Vivo | 2 projects, reconciled 2/2, every run | the whole V4 registry lives on another platform. The scrape was right; its scope was not |
| Markit | "Next →" offered on every page | it is never disabled, even 200 pages past the end |
| Markit | a `<tr>` with plausible values | a malformed `style` attribute swallowed a data payload; it is not a record |
| Markit | 35 project rows | 30 projects — merged cells and repeated ids |
| Verra JNR | 5,247 projects, identical to VCS | the **cache key ignored headers**, so the second standard served the first one's responses |
| SocialCarbon | 19 projects, 19 published references | **18 distinct** — two different projects publish `SOCIALCARBON-19`, and `-15` is missing |
| SocialCarbon | an `asset` list of tokenised credit blocks | 17 of 22 mirror the issuances exactly; the other 5 are **Verra** credits deposited into the platform |
| SocialCarbon | 17 issuances, all of them credits | they carry `Approved` / `Issuance complete`; an unset one is a **request**, not units |
| Bubble.io | `limit=200` accepted, HTTP 200 | clamped to 100, `remaining` says so, nothing else does |
| BioCarbon | `{"status": 403, "data": []}`, at **HTTP 200** | public; it just wants the `x-api-key` its own bundle ships |
| BioCarbon | `verified_reductions` equals the ledger on 103 of 105 | it is *verified*, not issued: one project has 322,687 verified and **no issuance blocks at all** |
| BioCarbon | a `cancellations` endpoint, 3 rows | 14 issuance blocks carry a `dropouts` the endpoint never mentions — 584,940 against 477,859 |
| BioCarbon | `País` reads "Malasia", "Perú", "Brasil" | the registry's own two languages, and `biome.yaml` matches on the **name** |
| Puro | a server-rendered page, so parse the HTML | the HTML *is* the transport: the JSON is in a Next.js RSC payload, split across pushes that must be joined before decoding |
| Puro | `RSC: 1` — the documented way to ask for that payload | ignored. Three variants all return the same prerendered HTML; there is no JSON route and no API host in any bundle |
| Puro | `countryCode: "NA"` — a placeholder | **Namibia.** Three adapters here strip `na` as "not stated"; reusing one deletes a real country |
| Puro | 1,519 retirements, so 1,519 rows | **2,099** — a retirement draws from several facilities at once, and the bundle is what names one |
| Puro | 20 issuances flagged withdrawn | a label with **no quantity**, and the registry's own issued total counts them in full |
| ACR | `acr2.apx.com`, the host every older note names | dead — HTTP 200 and "You have reached an invalid page" for **every** path. ACR moved to ICE GreenTrace |
| ACR | the `reportUrl` the page publishes | not an endpoint. A GET on it is HTTP 500 `No static resource`; `/results` and `/criteria` are the endpoints |
| ACR | criteria sent as a query string, or as JSON | the same generic 500. **Only a form body works** |
| ACR | `max=20000`, HTTP 200, rows returned | clamped to 2000 in silence — fifth registry to ignore a page size |
| ACR | one credit URL, so one ledger | four, selected by `holdingStatus`, and **the dataset key changes with it** |
| ACR | the unfiltered credit view — 16,385 "holdings" | the whole book. Its RETIRED and CANCELED rows **are** the two ledgers; ingesting it double-counts both |
| ACR | `issuanceQuantity` on every issuance row | the **parent event's** total, repeated across its blocks: 604M against a true 379M |
| ACR | 1,358 cancellations | 1,166 of them are **conversions** to the ARB or Ecology compliance registries, not credits destroyed |
| ACR | `projectSiteLocState` | three vocabularies — `OHIO`, `US-CA`, `Lower Saxony` — and multi-state values mix them in one string |
| ACR | a country column | an ISO code and **no name anywhere**: not in the list, not on the detail, not in the report's own filters |
| ACR | 1 req/s, the setting every other registry is happy with | a Cloudflare 429 at ~100 requests per ten minutes, with `Retry-After: 3600` |
| ACR | `401 "Invalid API Key"` on every route, mid-sync | **not a key we are missing.** The site's page is byte-identical and carries none; it is the platform declining to answer us, and it clears with time |

Four rules for every new adapter, before writing a line of paging code:

1. A public registry API that appears blocked usually is not. Check what
   headers the site's own bundle sends before concluding you need credentials.
2. A filter that appears applied usually is not. Prove it narrows, then
   reconcile the final row count against the registry's own published total.
3. **A per-record feed is not a per-project total.** Where a registry states
   its own totals, store those and let them win. `credit_totals` outranks
   summing `credit_events`, and every registry so far has needed it for at
   least one resource.
4. **A field name that exists is not a field that is populated.** Verra's
   column map applied cleanly to Plan Vivo and produced a sheet with the right
   shape and three empty columns, because `sectoralScope`, `vcsProjectId` and
   `regionName` are null there and a different field carries the vocabulary.
   Check fill rates against a real sync before believing a mapping.

**Check the status code is not the whole answer.** EcoRegistry reports refusals
in a 200 body; the Platts registries report a wrong `standardId` as 200 with
`totalEntities: 0`. Both look like an empty registry and neither raises.

Header overrides must actually override. `http_client._send_once` merges through
`httpx.Headers`, not a `dict`: header names are case-insensitive, and a plain
dict merge kept both httpx's lowercased `origin` and an adapter's `Origin`,
sending two conflicting values. That cost an afternoon on Cercarbono.

**A header that selects data belongs in the cache key.** The S&P platform
identifies a registry and a standard entirely in headers: the same POST body to
the same URL returns Verra VCS or Verra JNR depending only on `standardid`.
`_cache_key` originally hashed method, URL and body alone, so the second
standard scraped in a run silently served the first one's cached responses —
5,247 "JNR" projects, every one of them VCS, `standard_name` reading "Verified
Carbon Standard", nothing raised and reconciliation perfectly happy. The real
answer is 5 projects. `http_client.IDENTITY_HEADERS` is the fix and a test
pins it. Transport headers (correlation ids) stay out, or the cache never hits.

Server-side `groupKeys` aggregation returns 500, so per-project totals are
aggregated locally in SQL. The exception is `retirements`, which is too large to
page safely: `verra totals` fetches an exact server-side SUM per project
(`sum_by_project`), and those values override row-derived sums in
`db.credit_totals()`. Run it after any full `sync`.

## Gold Standard

Full contract in `docs/api-contract-gs.md`. The parts that will waste your day
if you skip them:

```
GET https://public-api.goldstandard.org/projects?page=<n>&size=150
GET https://public-api.goldstandard.org/credits?page=<n>&size=25
```

- **A browser `User-Agent` is required.** Without one every call is a
  Cloudflare 403 that reads like "this API is private". It is not — it is
  unauthenticated and public.
- **`size` caps differ per resource and lie in different ways.** `projects`
  clamps to **150** silently: ask for 1000, get 150, no error, no marker — a
  naive loop skips 85% of the registry. `credits` refuses anything above
  **25** with a *403*, which is a response-size limit, not a block.
- **Same silently-ignored-filter trap as Verra.** `?project_id=` and
  `?status=` both return the entire 182,989-record index with an unchanged
  `X-Total-Count`. Only `query` was proven to narrow. Never partition on the
  others.
- Counts come back in **headers**: `X-Total-Count`,
  `X-Total-Number-Of-Credits`. The response cache deliberately preserves
  `x-total-*` — a cache hit that dropped them would silently break
  reconciliation.
- Ordering is by descending `id` and stable across pages. No 10k window: page
  7000 returns fine. Plain sequential paging is safe here.
- A credit block embeds its whole `project` object, so the credit stream
  carries its own linkage — no per-project fan-out.

**`status` on a credit block is its current state, not an event type.** A
block issued and later retired reads `RETIRED`. Issued totals are therefore
the sum of *every* block regardless of status; summing only `ISSUED` reports
zero issued credits for any project that has since retired them. Handled in
`db.credit_totals()` — do not "fix" it back.

`sustaincert_id` is the canonical human reference (`GS7495`), not the project
name: a project named `GS23711-…` can itself be `GS23718`, because the name
carries its parent programme's number.

## Plan Vivo

Full contract in `docs/api-contract-planvivo.md`. Same backend, same request
shape and same envelope as Verra — `registries/platts/api.py` does all the
work and `registries/planvivo/api.py` is 40 lines of identity.

**This adapter reaches Plan Vivo V5 only, and that is a known gap, not a
finished job.** `PVCL` is the **PV Climate** registry, the Plan Vivo Standard
V5 system launched on S&P in 2025; its 2 projects are all of it. Plan Vivo has
been certifying since 2008 and the **V4-and-earlier projects live somewhere
else** — see PLAN.md 5e, which is where the search is tracked. The sync
reconciles 2/2 exactly and always will, because 2 is what this tenant
publishes: *the scrape is not wrong, its scope is*, which is the same shape as
every ignored-filter trap in this file. `verra standards -r planvivo` has
already ruled out the cheap explanation — `PVCL` publishes exactly one
standard, so V4 is not a second `standardId` behind the same registry code.

Only what differs from Verra is worth remembering:

- **`sectoralScope` is null; `projectType` carries the vocabulary.** Plan Vivo
  says "Afforestation / Reforestation". The column map is rebuilt with
  `sectoralScope` **removed**, not shadowed: two platform fields writing one
  column lets dict order decide whether a null overwrites a real value.
- **`vcsProjectId`, `regionName`, `methodologies` and `avgAnnualVolVcu` are all
  null.** `Project ID` falls back to the numeric `projectId` (which is what the
  registry's own public URL uses), and `Continent` is derived from the country
  name, the same path Cercarbono takes.
- **`unitClass`, not `unitType`.** An issuance is `rPVC`, `fPVC`,
  `Achievement Reserve` or `Future Risk Buffer`, and `unitType` reads the same
  across a project's reserve and non-reserve rows — recording it alone would
  make the reserves invisible. Holdings spell the same idea `unitClassName`.
  Verra is untouched and still records the bare `unitType`.
- **`fPVC` are forward credits** — the platform's own unit-type lookup flags
  them `isVerified: false` — and they are 103,246 of the 213,145 issued units.
  `Total Credits Issued` counts them because the registry's own published
  figure does. Raised with the business in `docs/field-mapping.md`, not
  silently reinterpreted; the class is stored, so a split is a
  `config/credits.yaml` change and not a re-scrape.
- **Two ledgers are genuinely empty** (retirements, cancellations) on a
  two-project registry activated in 2025. They are scraped anyway. Empty is a
  fact about the registry, and the day it fills nothing needs changing.
- `verra totals` has nothing to do here: every ledger pages in one request.

## Plan Vivo V4, and the legacy Markit registry — the third platform

Full contract in `docs/api-contract-markit.md`. Adapter in
`registries/markit/` (`api.py` + `tables.py`), identity in
`registries/planvivo/v4.py`.

**Plan Vivo is two registries on two platforms.** `planvivo/api.py` reaches PV
Climate — the **V5** system launched on S&P in 2025, 2 projects, and that is
genuinely all of it. Everything certified under **V4** and earlier is on
`mer.markit.com/br-reg/public`, the registry S&P inherited with IHS Markit.
That is 30 more projects and 5,034 retirements against V5's zero. The V5 sync
was never wrong and always reconciled 2/2 exactly: **the scrape was not wrong,
its scope was.**

The `standardId` is `100000000000004` — the id PLAN.md had recorded as "the
planning guess that returns `totalEntities: 0`". It was never a wrong id. It
was the right id for the wrong platform.

**Both eras store under one `PLAN_VIVO` registry** (user's decision,
2026-07-29). `standard_name` is what keeps that honest and reversible: a V4 row
reads "Plan Vivo Standard V4", a V5 row reads "PV Climate". The project id
spaces do not overlap.

It is a **platform, not a registry**: one HTML view serves **21 programmes**
behind `standardId`, including Social Carbon, ACRE, CCB, Peru REDD+, Pacific
Carbon Standard and W+. The full table is in the contract doc. Check it before
writing any new adapter — it is the same afternoon-versus-day difference as
the S&P tenant list.

It is also the first adapter here that **parses HTML** (stdlib `html.parser`,
no new dependency — the page is the contract; there is no JSON behind it).
Its traps are its own:

- **`limit` is accepted and ignored.** 15 rows a page whatever you ask for,
  HTTP 200, byte-identical response. Third registry to do this.
- **The pager lies, and asking past the end is not free.** No total is
  published anywhere and `<li id="page_no">` renders empty; "Next →" is
  *never* disabled, even 200 pages past the end. Worse, a 35-row feed answers
  a past-the-end request with an empty page but the 5,034-row one answers
  **HTTP 500**. So paging stops on a **short page** — fewer than `PAGE_SIZE`
  records — which means never asking for the page that 500s. The 500 is still
  handled for feeds that end exactly on a boundary, but only after a page has
  already been read: a 500 on the *first* request is a broken registry, not
  an empty one.
- **`standardId` is a request, not a filter.** Asking for Social Carbon
  returns rows whose Standard column reads "No Established Standard" — it is
  an *additional certification* there, not a primary standard. Every row is
  re-checked against the Standard column via `standard_names`.
- **Some `<tr>`s are not records.** The view emits rows whose `style`
  attribute has swallowed an entire data payload
  (`style="34.914551Sofala…Active…"`), which parse into plausible values in
  the wrong columns. A record is recognised structurally: it carries its own
  "View" link, not one inherited through a `rowspan`.
- **`rowspan` carries down**, and read positionally the continuation row puts
  Category ("Carbon") in the project-name column.
- **A project id is not unique.** Sofala renders twice; Scolel té's two rows
  are two sub-projects sharing one `master-project.jsp` id; Olympic Forest is
  "(Mali)" and "(Senegal)" under one id. **35 rows are 30 projects.** Rows
  sharing an id are merged — distinct project types joined, the count in
  `extra.mergedRows` — because `project_id` is half the primary key.
- **The Country column holds the state too**, and several ISO names carry a
  comma of their own: `Bolivia, Plurinational State of, Cochabamba` is one
  country and one state, `Korea, Republic of` is a country with none.
  `markit.COMMA_COUNTRY_NAMES` is the guard.
- **Ledger rows have no id.** `entity_id` is hashed from the row's values plus
  an occurrence index — hashed to stay idempotent, indexed because two
  identical rows are two events.

**Do not partition on `name`.** It looks like a project search and is not:
`name=zzzznotathing` returns nothing, so it plainly narrows — but
`name=N'hambita` returns **Mikoko Pamoja** rows, a different project, and
`name=Sofala` returns none of Sofala's own issuances. Whatever it matches, it
is not the project name. This is the "filter that appears applied" trap in its
purest form, and it was caught only by checking *which* rows came back rather
than how many. `dir=DESC` does work and reaches the far end of a feed, which
is the one partitioning tool here that has been proven. Neither is needed
today.

**Two gaps in the registry's own data**, measured and left alone:

- **`104000000013993` has credits but is not a project.** It appears in the
  holdings and retirement ledgers, its detail page is an empty shell, and it
  is absent from the project list even unfiltered. Its rows are kept; no
  project row is invented, so it simply does not reach the sheet. Same shape
  as Cercarbono's converted-in `CDC-106`/`CDC-107`.
- **Six projects retire more than they issued**, Sofala most starkly at
  **0 issued against 273,836 retired** — confirmed by searching the issuance
  feed directly, not inferred from a gap. Nothing is back-computed to make the
  arithmetic work: the issuance ledger is what the registry publishes, and
  `Total Credits Issued` stays as published. Raised in
  `docs/field-mapping.md` for the business rather than silently reconciled.

## Cercarbono

Full contract in `docs/api-contract-cercarbono.md`. Runs on **EcoRegistry**, a
shared platform — Cercarbono has no backend of its own. Four endpoints, three
of which return everything at once:

```
GET {ecoregistry}/project/public-by-standard/cercarbono-co2   the project list
GET {ecoregistry}/analytics/projects                          locations + serials
GET {ecoregistry}/analytics/get-retirements                   the whole ledger
GET {ecoregistry}/project/public/<id>                         crediting period
```

- **`platform: ecoregistry` and `lng` are required**, and the refusal without
  them arrives as **HTTP 200** carrying `ERROR_401`. Checking the status code
  cannot see it, and a silent `ERROR_401` looks exactly like an empty registry.
  The adapter raises on any `codeMessages` in a response.
- **`Origin` is validated.** The shared `settings.BROWSER_HEADERS` carries
  Verra's site, and sending it here returns a generic HTTP 500. The adapter
  overrides `Origin`/`Referer` with Cercarbono's own.
- **No paging at all**, so there is no page count to reconcile against. The
  guard is that every project in the standard's own list must come back.
- **Only `cercarbono-co2` is ingested.** EcoRegistry also hosts
  `cercarbono-biodiversity` and `cercarbono-circular-economy`, whose credits
  are not tCO2e. Both bulk feeds carry all standards mixed together and are
  filtered against the CO2 project ids. User's decision, 2026-07-28.
- `code` (`CDC-271`) is the human reference; the numeric `id` (274) is an
  internal key and the two do not agree.
- **`Worldwide` is a blank, not a place.** 575 of 811 locations carry it as
  both region and city — the placeholder for "never entered", always beside a
  real country. Writing it into Estado/Cidade would state a location the
  registry never published.
- The per-project detail is one request per project and is the **only** source
  of the crediting period. That is the whole reason a sync is 234 requests
  rather than 3.

**Do not sum Cercarbono's serials to get issued credits.** Three of 231
projects disagree with the row sum: `CDC-196` publishes its 2022 and 2023
issuances under two serial revisions (161,297 rows against a true 120,448), and
`CDC-106`/`CDC-107` are converted-in ex-BioCarbon projects missing from the
bulk feed entirely while still having retirements — so the row sum showed them
retiring credits they never issued. `certificatedVerification` on the detail is
the registry's own figure, a third endpoint agrees with it, and it is stored in
`credit_totals` where it outranks the rows. Buffer credits are included,
because the registry's own figure includes them; the flag lives in
`credit_events.unit_type`.

An adapter that publishes per-project totals implements the optional
`iter_credit_totals(resource)`; `registries.base.credit_totals_of()` is the
seam and `pipeline.sync_one` calls it when present. Registries without one are
unaffected.

**Ledger names are load-bearing.** `db.credit_totals()` branches on the exact
string `resource == 'credits'`: that name selects Gold Standard's
bucket-by-`status` semantics, and any other name selects Verra's
one-ledger-per-resource semantics. A new adapter picks its ledger names
deliberately, not descriptively.

## SocialCarbon

Full contract in `docs/api-contract-socialcarbon.md`. A **Bubble.io
application** with an open, unauthenticated Data API — no key, no browser
`User-Agent` (verified with none at all), no Cloudflare, no `Origin` check.
The friendliest target here and the smallest: **19 projects and three ledgers
in four requests**, about a minute.

```
GET {api}/meta                                 the readable types
GET {api}/obj/<type>?limit=100&cursor=<n>      {"response": {cursor, results, count, remaining}}
```

`count + remaining` is the registry's own total, restated on every page, and
is what reconciliation reads. There is no header count and no count endpoint.

- **`Project ID` is not unique and is not a key.** Two entirely different
  projects publish `SOCIALCARBON-19` — a peatland programme in Poland and a
  forest project in Brazil — and `SOCIALCARBON-15` is missing. 19 records, 18
  references. This is the Markit trap **inverted**: there a repeated id was
  one project and rows had to be merged, here merging would fuse two countries
  into one row. `project_id` is `hashed_id(REGISTRY, _id)` off Bubble's own
  record id, `external_id` is the duplicate reference as published, and
  `extra.bubble_id` keeps the id the hash cannot be read back from.
- **`asset` is not a ledger, and scraping it double-counts twice.** 17 of its
  22 rows mirror the issuances to the identical 189,794 units; the other 5
  read `"Standard": "VCS"`, carry no project link, and are Verra credits
  deposited into the platform's tokenisation layer. For the same reason
  SocialCarbon's **legacy-Markit rows** (`100000000000007`, where it is an
  *additional certification* reading "No Established Standard") are not
  ingested either. The Bubble registry is the current system.
- **An issuance can be a request.** `Approved` and `Issuance complete` are the
  registry's own flags. All 17 rows carry both today, so `iter_credit_totals`
  states exactly what the rows sum to — it exists so the first pending request
  does not quietly inflate an issued total. Every row is still stored; the
  state lands in `credit_events.status`.
- **`limit` clamps to 100 in silence.** Third registry to ignore a page size,
  so `_fetch` advances on `remaining`, never on the row count it got back.
- **Filters actually work.** Bubble validates `constraints` and answers a bad
  field with HTTP 404 `Field not found`, not the whole index — the first
  registry here that refuses a filter loudly. Nothing needs partitioning
  anyway.
- **`Retiree` is not a beneficiary.** It names the account that retired the
  units (81/81); `Beneficiary` names the third party (20/81). Only the
  structured field is read (user's decision, 2026-08-04) — the other 61 state
  it as prose in `Notes`, which is stored whole in `reason` because
  **`credit_events` keeps no raw payload**, only `projects` do. That column is
  the only copy, and it is what makes the decision reversible.
- **Two derivation gaps, both silent.** The bare `AFOLU` wording missed
  `biome.yaml`'s `Land use \(AFOLU\)` gate (now `\bAFOLU\b`, blast radius
  measured at zero existing rows), and
  `Congo, Democratic Republic of the` is a *third* ISO inversion
  `continent.yaml` did not carry.
- Four readable types are account data (`billing`, `accountmanager`,
  `organisationdetails`, `user`). Nothing asks for them and they are not read.

## BioCarbon

Full contract in `docs/api-contract-biocarbon.md`. **BioCarbon Registry
publishes through Global CarbonTrace** — a Laravel API behind a Vue SPA at
`globalcarbontrace.io`. `biocarbonregistry.com` no longer resolves; if you find
it in an older note, it is dead.

```
GET {api}/api/ghg/{projects|carbon-credits|retreats}?per_page=1000&page=<n>
GET {api}/api/ghg/projects/{id}                              the detail
GET {api}/api/ghg/carbon-credits/project/{id}/cancellations
GET {api}/api/impact-stats/get-stats            the registry's headline figures
```

Every list response is a Laravel paginator and its `total` is the registry's
own count. 105 projects, 626 issuance blocks, 11,439 retirements, 3
cancellation records, ~225 requests.

**It is a platform, not a registry** — one programme (`biocarbon`), three
categories (`gei`, `biodiversity`, `water`). Only `gei` is tCO2e and only
`gei` is ingested, the same call Cercarbono's adapter makes about EcoRegistry.

- **A public `x-api-key` is required and the refusal is an HTTP 200.** Without
  it the body reads `{"status": 403, "data": [], "message": …}` under a 200
  status line — `raise_for_status()` sees nothing and `data: []` looks like an
  empty registry. The key ships in the site's own `assets/api-*.js`, exactly
  as Verra's `appkey` does; `settings.BIOCARBON_API_KEY` reads
  `CARBON_BIOCARBON_KEY` first so a rotation is config, not a release.
- **`/api/public/*` and `/api/ghg/*` are the same data.** Only `ghg` has the
  per-project routes, so the adapter speaks `ghg` throughout.
- **`per_page` is honoured** — verified at 100 through 5000. The first
  registry here that does not silently clamp or ignore a page size. 1000 is
  politeness.
- **`transferences` is not a ledger.** 714 holder-to-holder moves of units
  already issued. Same shape as SocialCarbon's `asset` feed.
- **The cancellation feeds disagree, and the fuller one has no dates.** The
  endpoint publishes 3 rows / 477,859 units; 14 issuance blocks carry a
  `dropouts` totalling **584,940**, and `amount = active + outof + dropouts`
  holds on every one. Rows from the endpoint, total from the blocks through
  `iter_credit_totals`. No bulk feed and no published total exist, so this is
  the one ledger with nothing to reconcile against — every project is swept,
  not just those whose blocks show a dropout.
- **`verified_reductions` is not an issued total, and this is Cercarbono's
  trap inverted.** It matches the ledger for 103 of 105 projects;
  `BCR-TR-152-1-001` states 322,687 verified with **no issuance blocks at
  all**. The registry's own `emmitedCredits` agrees with the ledger, not the
  verified sum. There the ledger was the incomplete feed; here it is the
  authoritative one, and only checking both against the registry's own
  headline figure says which.
- **Both credit ledgers reconcile to the unit** against `impact-stats`:
  85,177,570 issued, 50,157,520 retired.
- **`total_reductions_general` is the ex-ante total; `total_reductions` is
  not.** The registry's own certificate template calls the first "the result
  … during the project's quantification period (:duration years)" and the
  second "verified during this monitoring period". No yearly figure is
  published and the total is never divided by the duration to make one.
- **`final_user` is the beneficiary, not `to_name`.** `to_name` is on all
  11,439 rows and is often an intermediary retiring for its customers — the
  same call as SocialCarbon's `Beneficiary` over `Retiree`. The registry marks
  7,033 retirements `private` and returns the name anyway;
  `credit_events.status` carries the flag so honouring it stays a query.
- **`País` arrives in the registry's own two languages** — "Colombia" beside
  "Malasia", "Perú", "Panamá", "Brasil". No language switch exists.
  `country_iso` is 105/105 so Continent is safe, but `biome.yaml` matches on
  the **name** and now carries both spellings.
- **Quantities are thousands-separated strings.** `float("299,564")` raises.
- One project publishes a crediting period ending 26 years before it starts.
  Stored as published.

## Puro.earth

Full contract in `docs/api-contract-puro.md`. **The first target here with no
API at all.** `registry.puro.earth` is a Next.js App Router app: the browser
never fetches registry data: the server renders each route and streams its
React Server Components payload *inside the HTML*, where the lists sit as
ordinary JSON.

```
GET {site}/projects        118 projects
GET {site}/issuances       583 issuance transactions
GET {site}/retirements   1,519 retirement transactions
GET {site}/projects/<code> one project — the detail page
```

Three requests are the whole registry. A sync is ~121 because one detail page
per project is read too, and about 120 MB because the pages are large — the
retirement feed alone is 5.2 MB and a detail page is ~900 KB.

`registries/puro/flight.py` is the Puro equivalent of `markit/tables.py`: the
part that knows the delivery format, kept away from the part that knows what
the fields mean.

- **The pushes must be joined before anything is decoded.** The payload
  arrives as any number of `self.__next_f.push([1,"<json string>"])` calls and
  the split lands mid-object on the big pages. Decoding them one at a time
  finds truncated JSON.
- **There is no JSON route.** `RSC: 1`, `?_rsc=`, and a full
  `Next-Router-State-Tree` were each tried and all three return the same
  prerendered HTML. No API host appears in any of the 15 client chunks either:
  the fetch happens server-side. Do not go looking again.
- **A transaction is not a credit record.** Every one carries a `bundles`
  list, and a retirement routinely draws on several production facilities at
  once — 1,519 retirements are **2,099 bundles**. The bundle names the
  facility, so a row per transaction files a multi-facility retirement against
  whichever facility came first and loses the rest. Issuances are 1:1 *today*;
  they are read the same way regardless.
- **`countryCode` is `"NA"` for Namibia**, and two projects are there. Three
  adapters here carry a `NOT_STATED` table listing `na`; reusing one deletes a
  real country code and takes its Continent with it. Puro states absence as
  JSON `null` and has no placeholder vocabulary, so `puro.NOT_STATED` is
  **empty on purpose** and a test pins it.
- **Withdrawal is a label, not a deduction.** 20 issuances carry
  `FULLY_WITHDRAWN` (6) or `PARTIALLY_WITHDRAWN` (14), `withdrawalDetails` is
  null on all 2,102 transactions, and **no withdrawn quantity is published**.
  The registry's own `Issued credits` counts them in full — measured, not
  assumed. So there is no cancellation ledger and `Total Credits Cancelled` is
  blank; the label lives in `credit_events.status`.
- **The registry publishes no row count anywhere** — not in the body, not in a
  header, not on the site. Reconciling `len(data)` against itself proves
  nothing, so the guards are the ones that can fail: every bundle's facility
  must be a known project (0 orphans across 2,682 bundles), every
  transaction's volume must equal its bundles', and every project's bundles
  must sum to what its own detail page states. **All 118 agree exactly, on
  both ledgers.**
- **The detail page is the only source of the country *name*.** The list
  publishes an ISO code and nothing else. The name is read beside the flag
  that code builds, so a positional read of someone's markup is a checked one.
- **Puro is the only registry that publishes a durability**, in years, on
  every labelled bundle — `CORC20+`, `CORC100+`, `CORC1000+`. Seven of its
  eight methodologies therefore have a `Durabilidade` band that is *checked*
  rather than inferred. Wooden Building Elements is the exception: bare `CORC`
  credits, no durability figure, so its band is an ordinary guess and is the
  one to take to the business first.
- **The beneficiary embargo is honoured by the registry, not by us.** 108
  retirements state a `beneficiaryHiddenUntil`; on the 51 still in force the
  name is simply absent from the API. The opposite of BioCarbon, which marks a
  retirement private and returns the name anyway. A later sync picks it up.

## Commands

```bash
verra discover -r verra           # capture a live S&P API contract (Playwright)
verra standards -r planvivo       # an S&P registry's real standardId + ledger set
verra standards -r all            # every S&P tenant named, in 8 GETs — check
                                  # this BEFORE assuming a registry needs a
                                  # new platform. It also takes a bare tenant
                                  # code: `verra standards -r GCC`
verra sync -r gs --limit 25       # smoke test one registry
verra sync -r gs --projects-only  # 4,141 GS projects, ~1 min
verra sync -r gs                  # + 182,989 credit blocks, ~2 h (7,320 requests)
verra sync -r verra               # ~5k projects + units, ~2.5 h
verra sync -r cercarbono          # 231 projects + both ledgers, ~4 min (234 requests)
verra sync -r planvivo            # BOTH systems: V5 on S&P (7 requests) and
                                  # V4 on Markit (~440), ~10 min in total
verra sync -r socialcarbon        # the whole registry in 4 requests, ~1 min
verra sync -r biocarbon           # 105 projects + three ledgers, ~5 min (225 requests)
verra sync -r puro                # 118 projects + both ledgers, ~4 min (121 requests)
verra sync -r acr                 # 994 projects + three ledgers, ~2 h — 1,005
                                  # requests at ONE PER SEVEN SECONDS, because
                                  # this registry bans rather than throttles
verra sync                        # every registry (-r defaults to `all`)
verra totals                      # EXACT per-project Verra retirement totals
verra derive                      # apply YAML rules -> derived columns
verra export                      # write the NEXT out/carbon-projects_vN.xlsx
verra update                      # sync + totals + derive. Writes NO spreadsheet
verra run                         # sync + totals + derive + export
verra status                      # row counts per registry, last run, failures
verra coverage -r gs              # per-column fill rate — where the gaps are
verra cache --clear               # a full sync caches ~1 GB of responses
verra slim-db --force             # the 20.8 MB database the installer ships
carbon-gui                        # the window the business team uses
pytest                            # offline, no network
.\build.ps1                       # tests + slim DB + EXE + portable ZIP + installer

cd docker; docker compose run --rm sync      # the scrape, headless, off the desktop
cd docker; docker compose run --rm publish   # the container's DB -> data/verra.db
```

`-r` / `--registry` takes `verra`, `gs`, `cercarbono`, `planvivo`,
`socialcarbon`, `biocarbon`, `puro`, `acr` or `all`.
`totals` is Verra-only — Gold Standard's whole credit stream pages cleanly,
Cercarbono, SocialCarbon, BioCarbon and Puro pick up their exact totals during
`sync` through `iter_credit_totals`, ACR's ledgers agree with its own
per-project figures on all 994 projects, and Plan Vivo's ledgers each fit in
one request.
`discover` and `standards` are S&P-only and refuse anything else.

`sync`, `derive` and `export` are separate on purpose: fixing a classification rule must never require re-scraping a registry.

Point the whole writable tree somewhere else for a throwaway run — useful for
smoke tests that must not touch the real database or `out/`:

```bash
CARBON_HOME=/tmp/scratch verra sync -r gs --limit 25
CARBON_OUT_DIR=/tmp/sheets verra export      # versioning still applies there
```

## Field sourcing — decided with the user, do not re-litigate

| Column | Verra | Gold Standard | Cercarbono | Plan Vivo |
|---|---|---|---|---|
| `Project ID` | `vcsProjectId` | `GS{sustaincert_id}` — **not** parsed from the name | `code` (`CDC-271`) — **not** the numeric `id` | numeric `projectId` — **no human reference is published**, and it is what the registry's own URL uses |
| `Tipo Macro de Projeto` | **Sectoral Scope**, copied straight through. Not derived. | **`type`**, copied straight through, **untranslated** | **`sectors[]`**, copied through **untranslated**, de-duplicated | **`projectType`**, copied through **untranslated** — `sectoralScope` is null here |
| `Total Credits Issued` / `Sold` / `Retired` / `Cancelled` | the **Units section**, four separate ledgers, aggregated per project | one `credits` stream, bucketed by block `status` | `certificatedVerification` for issued; `retirements` ledger for the rest; **no cancellation ledger — blank** | the same four ledgers as Verra; retirements and cancellations are **genuinely empty — blank, not zero** |
| `Total Credits Sold` | **Retired VCUs**, treated as sold, per the user's instruction | same rule | same rule | same rule |
| `Tipo Micro`, `Bioma`, `Durabilidade` | derivation layer | derivation layer, keyed on `type` | derivation layer, keyed on the **methodology string** — no AFOLU sub-type code is published | derivation layer, keyed on `projectType` — no methodology is published at all |
| `Continent` | Verra's own `regionName` | derived from `country_code` | derived from `country_name` — **no ISO code is published anywhere** | derived from `country_name` — no region, no ISO code |
| `Estado`, `Cidade` | project record | **not published — blank** | published for ~46%; the rest carry the `Worldwide` placeholder | project record |
| `Metodologia` | project record | published for ~60% of projects; blank otherwise | published for 94% | **not published — blank** |
| `Yearly Ex Ante` | `avgAnnualVolVcu` | `estimated_annual_credits` | **not published — blank**, never back-computed from issuances | **not published — blank**; forward credits are issued *units* here, not an estimate |
| everything else | direct from the project record | direct from the project record | direct from the project record | direct from the project record |

**Tipo Macro is registry-dependent by design.** Each registry's own vocabulary
is carried through as published: Verra rows say "Energy industries
(renewable/non-renewable sources)", Gold Standard rows say "Energy Efficiency -
Domestic", Cercarbono rows say "Land use (AFOLU)", Plan Vivo rows say
"Afforestation / Reforestation". This is the settled shape of the column
(user's decision, 2026-07-28), not a gap awaiting cleanup — a new registry
brings its own vocabulary too and that is expected. Never normalise, translate
or map them into a shared taxonomy without being asked.

The consequence for derivation: a rule matching a sector has to know every
registry's wording for it. `biome.yaml`'s `applies_when` gates the whole
ruleset on
`Agriculture Forestry|^A/R$|\bAFOLU\b|Afforestation / Reforestation|REDD|[Ff]orest|[Dd]eforestation` —
seven vocabularies, and **one unrecognised wording means no biome for any row
of that registry, with nothing in the log to say so.** A new registry's wording
belongs there before anything else. SocialCarbon is why that reads `\bAFOLU\b`
and not `Land use \(AFOLU\)`: it says the bare `AFOLU`, and the parenthesised
alternative missed it silently. BioCarbon says
`Agriculture, forestry and other land uses (AFOLU)`, which the
`Agriculture Forestry` alternative misses **on the comma alone** — and it is
why `durability.yaml`'s `afolu-generic` now carries `\bAFOLU\b` too, a gap
that had been silently costing SocialCarbon a `Durabilidade` since it was
added.

**A registry staying out of that gate can also be right.** Puro adds no wording
to it: it certifies engineered and hybrid removals — biochar, enhanced rock
weathering, geological storage, wood in buildings — and none of its 118
projects is land use, so `Bioma` is blank for all of them by design. The check
is whether the registry has land-use projects, not whether it has rows.

**The country-name bands are language-specific, and not every registry writes
English.** BioCarbon publishes "Malasia", "Perú", "Panamá", "Brasil",
"México" — its own data, carried through untranslated like every registry's
vocabulary — so `biome.yaml` carries both spellings of each. `continent.yaml`
is unaffected because it reads `country_code`, which BioCarbon publishes on
every project. Check the country column of a new registry's first index before
trusting any name-matching rule.

A second thing only Verra publishes: **`region_name`.** The continent-level
biome bands read it, so Gold Standard, Cercarbono, Plan Vivo, SocialCarbon and
BioCarbon rows never reach them and fall through to the country-name rules.
Adding a country band
(Miombo, Mesoamerica) therefore also refines the Verra rows that used to sit on
the coarse continental one — check the blast radius with a before/after count
on `project_derived` rather than assuming a new rule only touches new rows.

**And a registry can publish neither.** ACR states an ISO country *code* and no
name at all, so every band in `biome.yaml` missed all 994 of its rows —
including 352 forest projects, which would have made its Bioma blank for the
wrong reason. `north-america-temperate-by-code` is the answer: an ISO-code band,
**last in the file** so any country-name or region rule still wins, measured at
**4 rows added and 0 changed** across the seven existing registries. Its 351
matches are ACR's US and Canadian forests. That is the pattern for the next
registry that publishes a code and no name — a band at the end, and a
before/after count on the real database, never a guess about who else it
touches.

**Plan Vivo V4 sources differently from V5**, because it is a different
platform. Measured over the full 30-project index on 2026-07-29:

| Column | Plan Vivo V4 |
|---|---|
| `Project ID` | Markit's numeric `project_id`, from the row's own "View" link — no human reference is published, and for a grouped project this is the **master's** id |
| `Tipo Macro de Projeto` | the `Project Type` column, untranslated: "REDD", "Improved forest management", "Forest Conservation & Avoided Deforestation", "Forest Restoration", "Forest". Where one id covers several rows the distinct values are joined with `; ` |
| `Standard` | **"Plan Vivo Standard V4"** — asserted by the adapter, not published. The view says only "Plan Vivo", which is what V5's platform says too, and this is the one thing that tells the two eras apart |
| `Estado` | published, and it shares the `Country` cell — see the ISO-comma trap |
| `Total Credits Issued` / `Retired` | the `issuance` and `retirement` ledgers: 411 and **5,034** rows |
| `Total Credits Cancelled` | the ledger exists and is **empty** — blank, not zero |
| `Additional Certification` | the per-project detail page, one request each |
| `Continent` | derived from the country name. **The ISO inverted forms must be in `continent.yaml`** — "Bolivia, Plurinational State of" is not "Bolivia" to an exact-match list |
| `Data de Início` / `Data de Término` | **not published — blank, 0 of 30.** No crediting period appears anywhere on this platform, listing or detail |
| `Additional Certification` | from the detail page, and rare: 1 of 30 (Sofala, "Climate, Community and Biodiversity") |
| `Metodologia`, `Yearly Ex Ante`, `Total Ex Ante`, `Cidade` | **not published — blank** |

**Deliberate blanks for Verra JNR**, measured over its full 5-project index:
all four credit columns. JNR publishes projects and no credits whatever — its
issuance, holding, retirement and cancellation ledgers are all empty. Blank,
not zero, exactly as Plan Vivo V5's empty ledgers are.

**Deliberate blanks for Plan Vivo (V5)**, measured over the full 2-project index:

- `Metodologia` — `methodologies` is null (0/2).
- `Yearly Ex Ante` / `Total Ex Ante` — not published. Forward credits (fPVC)
  are issued *units* here, not an estimate, and are not moved into these
  columns.
- `Total Credits Retired` / `Sold` / `Cancelled` — both ledgers exist and are
  genuinely empty on a registry activated in 2025. Blank, not zero.
- `Additional Certification` — null on both.
- `Project ID` — no human reference is published; the numeric id stands.

**Deliberate blanks for Cercarbono**, measured over the full 231-project CO2
index:

- `Yearly Ex Ante` / `Total Ex Ante` — not published. Issuances are actuals,
  not an estimate; do not back-compute one from the other.
- `Total Credits Cancelled` — no cancellation ledger exists. Blank, not zero.
- `Additional Certification` — no equivalent field. The serials' `elegible`
  list is market eligibility ("Colombian Carbon Tax"), not a co-certification.
- `Estado` / `Cidade` — 46%. The rest carry the `Worldwide` placeholder.

**Deliberate blanks for Gold Standard**, measured over the full 4,141-project
index — do not try to fill them from elsewhere:

- `Cidade` — no city field exists (0/4,141).
- `Estado` — `state` is in the schema but null throughout (0/4,141).
- `Additional Certification` — no equivalent field. The credit `labels` field
  is a product class (`EMISSION_REDUCTION`), not a co-certification, and is
  deliberately not used for it.
- `Continent` — 21 gaps, all `XZ` / "International" multi-country projects.
  There is no single continent for those.

**SocialCarbon sources differently again**, measured over its full 19-project
index on 2026-08-04:

| Column | SocialCarbon |
|---|---|
| `Project ID` | the published `SOCIALCARBON-N` reference — **and it is not unique.** Two different projects publish `SOCIALCARBON-19`, `-15` is missing. The primary key is hashed from Bubble's `_id`; the sheet shows the duplicate as published |
| `Tipo Macro de Projeto` | `Project Type`, untranslated: "Agriculture Forestry and Other Land Use" (17), "AFOLU" (1), "Harmful Algae Bloom Treatment" (1) |
| `Total Credits Issued` | the `issuance` ledger, **counting only rows the registry marks `Approved` and `Issuance complete`** — an unapproved row is a request, not units. Stated through `iter_credit_totals` |
| `Total Credits Retired` / `Cancelled` | real ledgers, 81 and 2 rows. Only 5 of 19 projects have any credits; the rest are blank, not zero |
| `Metodologia` | `SCM0003`…`SCM0010-M1`, **19 of 19** |
| `Continent` | derived from the country name — no ISO code is published anywhere |
| `Yearly Ex Ante` | `Estimated Annual Emission Reductions`, 18 of 19 |

**Deliberate blanks for SocialCarbon**, same index:

- `Estado` / `Cidade` — no state or city field exists. The only location is a
  free-text `Address` (14/19) plus a lat/lng pair, which go to `extra`.
  Reading a state out of "XG3P+H8, South Africa" would be inventing one.
- `Total Ex Ante` — not published; computed from the yearly figure.
- `Additional Certification` — no equivalent. `CORSIA eligible` on an issuance
  is market eligibility, exactly like Cercarbono's `elegible` list.
- `country_code`, `region_name` — not published.

**BioCarbon sources differently again**, measured over its full 105-project
GHG index on 2026-08-04:

| Column | BioCarbon |
|---|---|
| `Project ID` | the published `BCR-CO-319-14-004` reference. Unique across all 105 — **checked, not assumed** — and still not the primary key: every ledger row and the public URL use the numeric initiative id |
| `Standard` | `applicable_standard`, "BioCarbon Standard" on 105 of 105 |
| `Tipo Macro de Projeto` | `sector_name`, untranslated: "Agriculture, forestry and other land uses (AFOLU)" (74), "Energy industries (renewable sources / energy efficiency)" (17), "Waste handling and disposal" (11), "Transport" (3) |
| `Metodologia` | the methodology **names**, 105 of 105 — its own `BCR0001`…`BCR0012` plus CDM codes, and the name carries the code, which is what lets the existing CDM rules fire unchanged |
| `Continent` | derived from `country_iso`, published 105/105 — the Gold Standard path |
| `Total Ex Ante` | `total_reductions_general`, 41 of 105. The only registry that publishes a total and no yearly figure, and the reason `excel` falls back to `exante_quantity` |
| `Total Credits Issued` | the issuance ledger, 85,177,570 — **not** `verified_reductions` |
| `Total Credits Cancelled` | `dropouts` on the issuance blocks, 584,940 — **not** the cancellation endpoint's 477,859 |

**Deliberate blanks for BioCarbon**, same index:

- `Estado` / `Cidade` — no structured field exists. The only sub-national
  location is `localitation`, a free-text sentence, which goes to `extra`.
- `Yearly Ex Ante` — not published, and never back-computed from the total.
- `Additional Certification` — no equivalent field.
- `region_name` — only Verra publishes one.
- `afolu_names` — no sub-type code. `type_project_name` is a display name, not
  a vocabulary; Tipo Micro keys on the methodology string, as Cercarbono's does.

**Puro sources differently again**, measured over its full 118-project index on
2026-08-05:

| Column | Puro.earth |
|---|---|
| `Project ID` | the published `code` (`227253`). Unique across all 118 — checked, not assumed — and unlike BioCarbon it *is* the primary key: the public URL, the certificate serials and every credit bundle use the same value |
| `Standard` | **"Puro Standard", asserted.** The registry names a *General Rules* version per project (13 versions across 118) and never names the standard; the version goes to `extra` |
| `Tipo Macro de Projeto` | the **methodology name**, untranslated: "Biochar, 2022" (80), "Wooden Building Elements" (14), "Terrestrial Storage of Biomass" (8), "Enhanced Rock Weathering, 2022" (6), "Carbonated Materials" (5), "Geologically stored carbon" (3+1), "Soil Amendment" (1) |
| `Metodologia` | **the same string.** Puro publishes one classification of what a project does and no sector vocabulary at all, so both columns carry it rather than one being filled from somewhere it is not published |
| `Durabilidade` | derivation layer, keyed on the methodology — and the **only** registry whose bands can be checked against a published figure |
| `País` | **published only on the detail page**, read beside the flag the project's own ISO code builds. The list route carries `countryCode` and no name |
| `Continent` | derived from `countryCode`, 118/118 — the Gold Standard path |
| `Total Credits Issued` / `Retired` | the two ledgers, 1,819,251 and 1,041,121 — confirmed twice, by summing the bundles and by reading each project's own page |

**Deliberate blanks for Puro**, same index:

- `Estado` / `Cidade` — no sub-national field exists on the list or the detail
  page. A lat/long pair is published for 31 of 118 and goes to `extra`.
- `Yearly Ex Ante` / `Total Ex Ante` — no estimate is published at all. Puro
  certifies removals that have already happened.
- `Total Credits Cancelled` — withdrawal is a **label with no quantity**, and
  the registry's own issued total counts the units anyway. Blank, not zero.
- `Additional Certification` — no equivalent. The `sdgs` list is an SDG claim,
  exactly like Cercarbono's `elegible` list.
- `Bioma` — biome is a land-use classification and Puro has no land-use
  projects. Blank for all 118, correctly.
- `status`, `region_name`, `afolu_names` — not published.

**ACR sources differently again**, measured over its full 994-project index on
2026-08-04:

| Column | ACR |
|---|---|
| `Project ID` | the published `ACR1275` reference, unique across all 994 — checked — and its **numeric part** is the primary key. The API's own routes take a different id (`P2423FTH4Z22`), which goes to `extra` and is what the project link is built from: a link built from the reference answers HTTP 200 with a shell that renders an error |
| `Standard` | **"American Carbon Registry", asserted.** What is published per project is a `creditingProgram` — ACR (612), California Air Resources Board (356), Washington Department of Ecology (26) — which is the compliance programme the credits serve, not the standard |
| `Tipo Macro de Projeto` | `projectType`, untranslated: "Forest Carbon" (352), "Ozone Depleting Substances" (201), "Refrigerants" (134), "Coal Mine Methane" (91), "Industrial Process Emissions" (88), and twelve more |
| `Metodologia` | the protocol name, 994 of 994 — and the **only** thing that separates 352 "Forest Carbon" projects into improved management, reforestation and avoided conversion |
| `Tipo Micro` | derivation layer, keyed on the protocol name, 993 of 994 |
| `Continent` | derived from the ISO country code, 994 of 994 — the Gold Standard path |
| `Total Credits Issued` | the issuance ledger, 379,674,647 — agreeing with the project list's own `issuedCredits` on **all 994 projects** |
| `Total Credits Cancelled` | the cancellation ledger, 187,414,534 — of which 1,166 rows of 1,358 are **conversions** to the ARB or Ecology compliance registries, not credits destroyed. The column reports the registry's own figure (user's decision, 2026-08-04); the reason is on every row, so a split is a query |

**Deliberate blanks for ACR**, same index:

- `País` — **no country name is published anywhere**: the list states an ISO
  code, the detail states the same code, and the report's own filter list
  offers no country field. The first registry here with that gap. Filling it
  means introducing a code-to-name table, which is a decision about the
  deliverable rather than something the registry published, and the answer was
  **blank (user's decision, 2026-08-04)**. The code is stored, so a later
  change of mind is a `derive` change and not a re-scrape.
- `Cidade` — the field exists and is null throughout. The lat/long pair goes
  to `extra`.
- `Total Ex Ante` — `estimatedTotalCredits` is the number **0** on every
  project sampled, which is not the same as an estimate of nothing. `excel`
  computes the total from the yearly figure instead.
- `Additional Certification` — no equivalent. `hasAnotherCarbonProgram` and
  `hasAnotherEnvironmentalMarket` are booleans with no name attached and go to
  `extra`, where a double-count check can read them.
- `region_name`, `afolu_names` — not published.
- `Bioma` for 9 land-use rows — 5 "Agricultural Land Management" and 4
  "Wetland Restoration". The only North American band available names a
  *forest*; labelling a restored wetland as temperate forest is worse than the
  blank.

**Known wrinkle:** under "retired = sold", `Total Credits Sold` and `Total Credits Retired` are the same number. Retirement beneficiary is stored anyway, and `config/credits.yaml` has a `sold_equals_retired` toggle that flips to a beneficiary-based split (retired for a named third party = sold) if the business confirms that is wanted. Do not change the default without being asked.

## Rules for working in this repo

- **Never invent data.** If Verra does not publish a value, the cell stays blank. No plausible-looking estimates, no filling gaps from other sources without being asked.
- **Derivation rules live in `config/derivation/*.yaml`, not in Python.** The rules are informed guesses awaiting business validation. Changing a classification must be a YAML edit followed by `verra derive && verra export`.
- **Every derived value records which rule produced it** (`project_derived.rule_name`). A wrong classification must be traceable.
- **All DB writes are idempotent upserts.** A re-run repairs a partial run; it never duplicates.
- **Be polite to the registry.** ~1 req/s default, backoff on failure, on-disk response cache so development re-runs do not re-hit the site. Do not raise concurrency to "make it faster".
- **Tests never touch the network.** They run against committed fixtures in `tests/fixtures/`.
- **Every export is a new version.** See "Delivering the spreadsheet". A run that overwrites a delivered file is a bug, not a convenience.
- `docs/field-mapping.md` is the artifact the business reviews. Keep it current when rules change.

## Layout

```
src/carbon_scraper/
  cli.py                       typer front end; thin layer over pipeline
  pipeline.py                  the orchestration both front ends drive;
                               ProgressSink + cancellation
  settings.py                  paths (resource/user split), base URLs, headers, knobs
  http_client.py               httpx + rate limit + retry/backoff + on-disk cache
  db.py                        SQLite schema, migration, idempotent upserts, run log
  derive.py                    rule engine + computed columns
  excel.py                     xlsx writer; column order from assets/fields-asked.txt,
                               versioned output
  registries/
    __init__.py                ADAPTERS dispatch table (registry -> a TUPLE of
                               adapters) + name aliases
    base.py                    the adapter contract pipeline drives, plus the
                               optional iter_credit_totals seam, the
                               ClientOwner lifecycle mixin and reconciled()
    text.py                    stated() / joined() / hashed_id(), shared by the
                               adapters. The placeholder TABLES stay per
                               registry; the code applying them does not
    platts/api.py              the S&P PLATFORM: POST search API, paging,
                               partitioning, reconciliation, normalisation.
                               Shared by every registry hosted on it
    platts/discovery.py        Playwright network capture -> a per-registry
                               contract file; plus standards_by_registry()
    markit/api.py              the LEGACY MARKIT PLATFORM: HTML paging, the
                               standard re-check, merged-row handling.
                               21 programmes behind one standardId
    markit/tables.py           rowspan-aware HTML table reader (stdlib only)
    verra/api.py               Verra VCS's identity only — a PlattsAPI subclass
    verra/jnr.py               Verra JNR — same tenant, second standard
    planvivo/api.py            Plan Vivo V5's identity, plus its two field diffs
    planvivo/v4.py             Plan Vivo V4 — a MarkitPublicAPI subclass
    goldstandard/api.py        REST paging, header-based reconciliation
    cercarbono/api.py          three bulk feeds + per-project detail, CO2 filter
    socialcarbon/api.py        Bubble Data API: cursor paging on `remaining`,
                               keys hashed off Bubble's `_id` because the
                               published reference is not unique
    biocarbon/api.py           Global CarbonTrace: Laravel paginator, a 403
                               that arrives inside a 200, and the one ledger
                               with two feeds and no published total
    puro/api.py                Puro.earth: no API at all. Bundles rather than
                               transactions, and the only registry that
                               publishes a durability of its own
    greentrace/api.py          the ICE GREENTRACE PLATFORM: form-posted
                               report paging, the silent 2000-row clamp, the
                               dataset key that changes with the filter.
                               Serves ACR and ART
    acr/api.py                 ACR's identity, the fields it populates, and
                               the rate this registry has to be read at
    puro/flight.py             the Next.js RSC payload reader — joins the
                               `__next_f.push` stream, then decodes. Stdlib
                               only, and the Puro half of markit/tables.py
  gui/
    state.py                   what the window remembers. No Tk, no pipeline
    worker.py                  thread + queue + logging bridge. NO Tk import
    app.py                     the window; the only module touching a widget

build.ps1                      clean -> pytest -> slim-db -> EXE -> ZIP -> installer
packaging/
  bundle.py                    what goes in the frozen build, DERIVED from
                               settings.SEEDED_FILES and registries.ADAPTERS
  carbon_gui.py                the frozen entry point; calls gui.app.main()
  carbon-registry.spec         PyInstaller one-folder, --noconsole
  carbon-registry.iss          Inno Setup, per-user, keeps %LOCALAPPDATA% data
  LEIA-ME.txt                  pt-BR, ships INSIDE the ZIP; unblock is step 1
  release-notes-pt.md          pt-BR, the GitHub release body
docker/
  Dockerfile                   two targets: runtime (no browser), discovery
  compose.yaml                 jobs, not daemons; volume-backed CARBON_HOME
  publish-db.py                VACUUM INTO the checkout; refuses to overwrite
  README.md                    the operator guide, and the WAL reasoning
```

There is no browser-based scraping fallback: the direct API path works for
every registry and is verified end to end. `platts/discovery.py` exists to
re-derive a contract when the operator changes it, not to scrape.

**Adding a registry** = one new `registries/<name>/api.py` implementing
`base.RegistryAdapter`, one entry in `registries.ADAPTERS` (plus `ALIASES`),
one label in `settings.REGISTRY_LABELS`, one entry in
`settings.PROJECT_DETAIL_URLS`, a contract doc under `docs/`, fixtures and
tests, and derivation rules that key on whatever field it publishes. Nothing in
`db`, `derive`, `excel` or the GUI should need to change — the GUI builds its
checkboxes from `REGISTRY_LABELS`.

**Check the platform tables first.** `verra standards -r all` names every S&P
tenant in eight GETs, `docs/api-contract-markit.md` lists the 21 programmes on
the legacy Markit view, and `docs/api-contract-acr.md` lists the two ICE
GreenTrace tenants — ACR, which has an adapter, and **ART** (Architecture for
REDD+ Transactions, 30 projects), which does not and is one subclass away.
Between them that is 31 registries reachable by subclassing something that
already works. Cracking a new site is the last resort, not the first move.

**And check that the site an older note names is still the site.** ACR was
filed for a year as "APX ASP platform, form posts and HTML tables". `acr2.apx.com`
still answers — with HTTP 200 and "You have reached an invalid page" for every
path, which a scraper reads as an empty registry rather than a moved one. The
registry's own public-reports page named the new host in one fetch.

**Then check whether the site is shipping the data in its own HTML.** Puro was
filed as "server-rendered HTML, look for a JSON endpoint before writing a
parser" and turned out to be neither: it is a Next.js App Router app whose RSC
payload carries the whole registry as JSON inside the page. There is no XHR to
intercept and no endpoint to find — and looking for one is an hour spent
proving a negative. A modern JS framework rendering on the server is a *good*
outcome here, not a browser-automation problem. See `puro/flight.py`.

**If it is on S&P Platts** that shrinks to: subclass `PlattsAPI`, set six
class attributes, and check which fields the registry actually populates. See
"Adding a registry hosted on S&P Platts" above. **If it is on legacy Markit**:
subclass `MarkitPublicAPI`, set `standard_id` and `standard_names`, and check
the fill rates — the column set differs per programme.

**Before adding a second adapter to an existing registry**, be sure it is the
same registry to the business, because the merge is what the sheet will show.
Plan Vivo V4/V5 and Verra VCS/JNR are both merges the user asked for; what
keeps them honest is that `standard_name` distinguishes the rows, so either
can be split again with a query rather than a re-scrape.

**Ask what else already holds these credits, before scraping and not after.**
Every registry added so far has turned out to overlap another one somewhere:
Verra CCBS co-certifies VCS projects, SocialCarbon appears on legacy Markit as
an additional certification, and SocialCarbon's own `asset` feed re-states its
issuances *and* carries deposited Verra credits. Two feeds that each look
authoritative is the normal case, not the strange one. Reconciling a count
proves nothing about this: both feeds reconcile perfectly and the sum is still
twice the truth.

**One overlap is live in the database, deliberately.** Cercarbono's `CDC-106`
and `CDC-107` are BioCarbon's `BCR-CO-319-14-002` and `-005` — the same two
physical projects, both still published by both registries. The credits are
*different tranches* (3.9M and 8.0M here against 79k and 171k there), so
neither row is a copy of the other and **both ship**, cross-linked through
`extra.also_registered_as` (user's decision, 2026-08-04). The linkage is
published only from Cercarbono's side, in its own `converted_from_link`, which
is why the BioCarbon adapter carries a measured table rather than reading one.

**And a second one, found the same way, 2026-08-05.** ACR publishes a
`hasAnotherCarbonProgram` flag — true on 16 of its 994 projects — and never
says which programme. Checking those names against the database (rare name
tokens plus country, because matching on "wind" or "wastewater" invents pairs
by the hundred) found two mine-methane projects that **Verra also publishes**:
`ACR0242` is `VCS 559` and `ACR0388`/`ACR1192` are `VCS 573`. Their crediting
periods are *consecutive* rather than concurrent — Verra 2008-2018, ACR
2018-2027 — so the tranches are disjoint, neither row restates the other, and
both ship cross-linked through `acr.ALSO_REGISTERED_AS`.

Two known duplicates now, and together they are what stops "sum every
registry's `Total Credits Issued`" being a safe query. Both were found by
asking the question before scraping rather than after.

**A published human reference is not a primary key until it is proven unique.**
SocialCarbon publishes `SOCIALCARBON-N` on every project and repeats one across
two unrelated projects; the legacy Markit view repeats an id across sub-projects
of one master. Those two need *opposite* handling — merge there, keep apart
here — and only looking at the rows tells you which. Check for duplicates in the
first index you download.

## The GUI

`PLAN.md` is the phase tracker; read it before touching this area.

Three modules, split by **what is allowed to touch a widget**:

| | |
|---|---|
| `gui/state.py` | what the window remembers: ticked registries, output folder. No Tk, no pipeline |
| `gui/worker.py` | the thread, the queue, the logging bridge. **Imports no Tk, and a test asserts it** |
| `gui/app.py` | the window. The only module that reads or creates a widget |

Tkinter is not thread-safe, and the failure is not immediate or reproducible.
The worker thread can only put messages on a `queue.Queue`; `app._drain` pulls
them on `root.after`, on the main loop, which is the only place a widget may be
touched. `worker.py` holding no Tk import is what makes that structural rather
than a rule someone has to remember.

**Two buttons, and the separation is the design.** `Export Excel` runs
derive + export — seconds, no network, works on a machine that has never
scraped anything because the installer ships a database. `Update registry data`
runs sync + Verra's exact-totals pass + derive, and **deliberately does not
export**: if a refresh wrote a delivery, every update would burn a version
number and the business would receive `_v9` without anyone deciding to send
anything. `build_export_task` / `build_update_task` are module-level so that
difference is testable without opening a window.

Other things that are load-bearing:

- **The checkboxes mean the same thing to both buttons.** That is why
  `db.all_projects`, `excel.build_rows` and `pipeline.only/selected` take a
  *sequence* of registries, not just one-or-all. `db.registry_clause` treats an
  **empty** selection as no rows, never as everything — falling back to "all"
  would make an untick silently export the whole database, which is the same
  shape of bug as a silently ignored API filter.
- **The time estimate is a static table** (`settings.SYNC_ESTIMATE_MINUTES`),
  not the durations in the `runs` table. A repeat run reads most responses from
  the ~1 GB cache and finishes in minutes, so measured history would promise
  four minutes and then take two hours. Past durations are reported as history,
  never as a forecast. A registry missing from the table makes the estimate
  *unknown* — it is never quietly omitted from the total.
- The folder picker feeds `pipeline.export(out_dir=...)`, which still writes
  `_vN+1`. It is not, and must not become, an overwrite flag.
- `install_logging` puts a handler on the root logger before anything calls
  `logging.basicConfig` — which is a no-op once a handler exists — so
  `INCOMPLETE` reconciliation errors reach the log pane. A business user has no
  console to find them in. Everything also goes to `LOG_DIR/gui.log`, and an
  uncaught worker exception writes `LOG_DIR/error-<task>-<stamp>.log` and puts
  the path in the dialog.
- **A cancel is not a crash.** `http_client.Cancelled` is caught separately, so
  pressing Cancel shows "stopped", writes no traceback file, and says the run
  is safe to resume.
- Adding a registry adds a checkbox with no GUI edit: the window builds them
  from `settings.REGISTRY_LABELS`, and a test fails if any registry identifier
  is hard-coded in `app.py`.

## Packaging

`.\build.ps1` — clean → pytest → `slim-db` → PyInstaller → ZIP → Inno Setup.
Two artifacts: `dist/portable/CarbonRegistryScraper-<version>-portable.zip` and
`dist/installer/CarbonRegistryScraper-<version>-setup.exe`, 16.7 MB, per-user,
no admin rights. `-SkipTests` and `-SkipInstaller` exist for iterating on the
packaging itself; the ZIP is produced either way, because it is the one that
gets handed out.

**The ZIP is the delivery, and the reason is SmartScreen.** Nothing here is
signed, so anything downloaded carries a Mark of the Web and greets the user
with "Windows protected your PC". Unblocking the `.zip` before extracting
(Properties → Unblock) strips that mark once, before any executable runs, and
nothing extracted afterwards is checked. That instruction is step 1 of
`packaging/LEIA-ME.txt` (Portuguese, shipped inside the ZIP), the first section
of `packaging/release-notes-pt.md`, and printed by `build.ps1` at the end of
every build — three places because the person who forgets it is the person the
tool was built for. Do not replace it with advice to disable SmartScreen.
Version lives in `pyproject.toml`; the `.iss` copy is pinned to it by a test.

- Playwright is a developer dependency and is **excluded from the packaged
  build**. `discover` is a diagnostic; the EXE does not ship it.
- Dependencies are weight in the bundle. Do not add one without a use — see the
  note in `pyproject.toml` about `pandas` and `pydantic`. Tkinter is standard
  library and costs nothing extra.
- `carbon-gui` is registered under `[project.gui-scripts]`, not
  `[project.scripts]`: on Windows that builds a `pythonw` launcher, so no
  console flashes up behind the window.
- In a checkout the GUI delivers to `out/`, the same folder the CLI uses, so
  one version history is not split in two. A frozen build defaults to
  `Documents\Carbon Registry` instead — `%LOCALAPPDATA%` is the right place for
  a database and the wrong place for a file the user has to find and email.
- **`packaging/bundle.py` derives what goes in; it does not list it.** Data
  files come from `settings.SEEDED_FILES` and hidden imports from
  `registries.ADAPTERS`, so adding a registry or a config file cannot forget
  the build. A missed adapter is the nasty one: the checkbox still appears —
  the window builds those from `REGISTRY_LABELS` — and Update raises
  `ModuleNotFoundError` on a machine with no console to read it in.
- **The uninstaller has no `[UninstallDelete]` section, deliberately.**
  Everything the program writes is under `%LOCALAPPDATA%\CarbonRegistryScraper`
  and survives an uninstall, so a reinstall picks the existing database back up
  rather than costing someone a two-hour scrape.
- **Nothing is signed**, so a downloaded artifact shows SmartScreen's "Windows
  protected your PC" with Run anyway behind *More info*. Not fixable in code;
  it needs an Authenticode certificate. Unblocking the ZIP first is the free
  answer — see above. `build.ps1` prints the warning and the instruction.
- **Antivirus is the part none of this fixes.** Unsigned PyInstaller
  executables get heuristically quarantined by some corporate AV, and no
  amount of unblocking changes that. It cannot be checked from the development
  box; the source route (`pip install -e .` in a clone, seed DB into `seed/`)
  is the documented fallback. Do not report the distribution as verified until
  it has run on someone else's machine.
- `build.ps1` routes every external command through `Invoke-Native`. Windows
  PowerShell 5.1 turns a native program's stderr into a terminating error under
  `$ErrorActionPreference = "Stop"`, and PyInstaller logs its whole INFO stream
  to stderr — so a successful freeze aborted the build, but only when the
  output was piped. The exit code is the only honest signal.

### The shipped database

`verra slim-db` writes `dist/seed/carbon-seed.db`: `projects`,
`credit_totals`, `project_derived` and `runs` only. **214.8 MB → 20.8 MB**, and
the sheet is identical — 9,619 rows × 25 columns, zero cell mismatches. The
installer carries it, and `settings.seed_database()` adopts it on first run, so
`Export Excel` works on a machine that has never scraped anything.

The bulk tables carry three things the export needs, so `db.export_slim`
materialises them before dropping the rows: the per-project credit buckets, the
beneficiary-based "sold" figure (under `db.BENEFICIARY_RESOURCE`, so flipping
`sold_equals_retired` still works on a fresh install), and `Additional
Certification`, which Verra only ever states on unit rows.

Three rules, each of which is a bug that would not have raised:

- **A seeded total must lose to a scraped one.** `credit_totals.source`
  separates `seed` from `registry`; `db.credit_totals()` ranks events → seed →
  registry-stated. Equal authority would freeze an install's credit columns at
  whenever the installer was cut, no matter how often the user re-scraped.
  `pipeline.sync_one` drops the seed once a registry's ledgers are scraped **in
  full** — not after `--limit`, not after `--projects-only`.
- **`seed_database()` only ever writes when there is no database at all.** Not
  "if it is empty", not "if it is older". Restoring installer data over a
  scrape the business waited two hours for would be undetectable: the sheet
  still builds.
- **Copy tables by name, never `SELECT *`.** `ALTER TABLE ADD COLUMN` appends,
  so a migrated database has the right columns in the wrong order —
  `runs.registry` is last there and third in `SCHEMA`. The positional copy
  shifted every value one place along and only failed because `started_at` is
  NOT NULL.

The result is a **delivery artifact, not a working copy**: `verra sync` against
an installed one fills the ledgers back in registry by registry.

## Docker — the scrape, not the delivery

Full operator guide in `docker/README.md`. Docker covers the **headless half**:
a full sync is ~16,500 requests and four-plus hours with no window involved, so
it runs in a container instead of tying up the desktop. It **does not replace
`build.ps1`** — PyInstaller needs a Windows host, and SmartScreen, signing and
antivirus are all about an EXE arriving on someone else's machine.

Two targets in one `Dockerfile`: `runtime` (the scraper, no browser, same as
the frozen build) and `discovery` (adds Playwright and one Chromium).

Three things are load-bearing, each pinned by `tests/test_docker.py`:

- **`CARBON_HOME=/data` is a named volume, never a Windows bind mount.**
  `db.py` sets `PRAGMA journal_mode=WAL`, which needs POSIX advisory locks and
  a shared-memory `-shm` mapping; Docker Desktop's Windows bind mounts
  (virtiofs / gRPC-FUSE) provide neither reliably, and the failure is a
  database that still opens. `export`, `publish` and `discover` may bind-mount
  because none of them touches SQLite over the mount. Getting the database
  back to Windows is `docker/publish-db.py` — **`VACUUM INTO`, not a file
  copy**, because a WAL database's committed state is spread across three
  files and copying the first alone yields a stale database that reports
  itself as fine. It refuses to replace `data/verra.db` without `--force`.
- **The install must stay `pip install -e`.** `settings.RESOURCE_ROOT` is
  `Path(__file__).parents[2]`, and `assets/`, `config/` and `docs/` are not
  package data — setuptools collects `src/` only. A plain `pip install .`
  lands in site-packages where none of them exist; nothing raises, the
  requested-field list simply comes back empty. Same class of failure as a
  frozen build writing under `_MEIPASS`.
- **The container refreshes; it never delivers.** `docker compose run --rm
  sync` runs `verra update`, which is `pipeline.update_all()` — the same seam
  the window's `Update registry data` button drives, Verra-only totals guard
  included. There are now three callers of that chain (GUI, CLI, `run_all`),
  which is why the guard lives in `pipeline` and not in a front end: `totals`
  is a per-project fan-out against Verra, and running it after a
  Gold-Standard-only sync is thousands of requests to a registry nobody asked
  for. An export from a scheduled refresh would burn a version number nightly.

Everything is a job that exits — no daemon, no in-container scheduler. On a
laptop a container scheduler stops when Docker Desktop stops, which is whenever
the lid closes, and misses runs with no signal. Politeness settings are
unchanged, and the container NATs out through the host, so Cloudflare in front
of Gold Standard and Cercarbono's `Origin` check see the same address they see
today. Moving this to a cloud VM would change that.
