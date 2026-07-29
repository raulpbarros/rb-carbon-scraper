# CLAUDE.md — Carbon Registry Scraper

## Response style

**Always use `/caveman:caveman` in this project.** Invoke the skill at the start of a session and stay in it. Code, commit messages, PR bodies, and security/destructive-action warnings are still written normally — caveman applies to prose replies only.

## What this project is

A scraper that builds a database of every carbon project across public
registries and exports one formatted Excel sheet for the business team.
Four registries are live: **Verra VCS**, **Gold Standard**, **Cercarbono** and
**Plan Vivo**; four more are planned. Adding one means writing an adapter, not
touching the pipeline.

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

| | Verra VCS | Plan Vivo | Gold Standard | Cercarbono |
|---|---|---|---|---|
| Front end | `registry.verra.org` | `registry.spglobal.com/pvclimate` | `registry.goldstandard.org` | `registry.cercarbono.com` |
| Backend | `prod-us.api.platts.com` (S&P) | **the same** | `public-api.goldstandard.org` | `api-front.ecoregistry.io` |
| Shape | POST search, Elasticsearch behind it | **the same** | plain REST GET | plain REST GET |
| Projects | ~5,200 | **2** | 4,141 | 231 (CO2 standard) |
| Credit records | ~305k retirements alone | 27 issuances + 10 holdings | ~183k blocks | 2,529 serials + 9,350 retirements |
| Requests per sync | ~9,000 | **7** | ~7,300 | ~234 |
| Contract doc | `docs/api-contract.md` | `docs/api-contract-planvivo.md` | `docs/api-contract-gs.md` | `docs/api-contract-cercarbono.md` |
| `discover` needed | yes | no — the standards lookup was enough | no — plain HTTP was enough | no — plain HTTP plus a bundle read |

All read via `--registry verra|planvivo|gs|cercarbono|all`. Verra and Plan Vivo
share `registries/platts/api.py`; the others have their own sections and
contract docs.

Planned, in `PLAN.md` order: BioCarbon, Puro.earth, ACR and SocialCarbon.

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
  no sink can forget to.
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
work and `registries/planvivo/api.py` is 40 lines of identity. Only what
differs is worth remembering:

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

## Commands

```bash
verra discover -r verra           # capture a live S&P API contract (Playwright)
verra standards -r planvivo       # an S&P registry's real standardId + ledger set
verra sync -r gs --limit 25       # smoke test one registry
verra sync -r gs --projects-only  # 4,141 GS projects, ~1 min
verra sync -r gs                  # + 182,989 credit blocks, ~2 h (7,320 requests)
verra sync -r verra               # ~5k projects + units, ~2.5 h
verra sync -r cercarbono          # 231 projects + both ledgers, ~4 min (234 requests)
verra sync -r planvivo            # 2 projects + 4 ledgers, seconds (7 requests)
verra sync                        # every registry (-r defaults to `all`)
verra totals                      # EXACT per-project Verra retirement totals
verra derive                      # apply YAML rules -> derived columns
verra export                      # write the NEXT out/carbon-projects_vN.xlsx
verra run                         # sync + totals + derive + export
verra status                      # row counts per registry, last run, failures
verra coverage -r gs              # per-column fill rate — where the gaps are
verra cache --clear               # a full sync caches ~1 GB of responses
carbon-gui                        # the window the business team uses
pytest                            # offline, no network
```

`-r` / `--registry` takes `verra`, `gs`, `cercarbono`, `planvivo` or `all`.
`totals` is Verra-only — Gold Standard's whole credit stream pages cleanly,
Cercarbono picks up its exact totals during `sync` through
`iter_credit_totals`, and Plan Vivo's ledgers each fit in one request.
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
`Agriculture Forestry|^A/R$|Land use \(AFOLU\)|Afforestation / Reforestation` —
four vocabularies, and **one unrecognised wording means no biome for any row of
that registry, with nothing in the log to say so.** A new registry's wording
belongs there before anything else.

A second thing only Verra publishes: **`region_name`.** The continent-level
biome bands read it, so Gold Standard, Cercarbono and Plan Vivo rows never
reach them and fall through to the country-name rules. Adding a country band
(Miombo, Mesoamerica) therefore also refines the Verra rows that used to sit on
the coarse continental one — check the blast radius with a before/after count
on `project_derived` rather than assuming a new rule only touches new rows.

**Deliberate blanks for Plan Vivo**, measured over the full 2-project index:

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
    __init__.py                ADAPTERS dispatch table + name aliases
    base.py                    the adapter contract pipeline drives, plus the
                               optional iter_credit_totals seam
    platts/api.py              the S&P PLATFORM: POST search API, paging,
                               partitioning, reconciliation, normalisation.
                               Shared by every registry hosted on it
    platts/discovery.py        Playwright network capture -> a per-registry
                               contract file; plus standards_by_registry()
    verra/api.py               Verra's identity only — a PlattsAPI subclass
    planvivo/api.py            Plan Vivo's identity, plus its two field diffs
    goldstandard/api.py        REST paging, header-based reconciliation
    cercarbono/api.py          three bulk feeds + per-project detail, CO2 filter
  gui/
    state.py                   what the window remembers. No Tk, no pipeline
    worker.py                  thread + queue + logging bridge. NO Tk import
    app.py                     the window; the only module touching a widget
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

**If it is on S&P Platts** that shrinks further: subclass `PlattsAPI`, set six
class attributes, and check which fields the registry actually populates. See
"Adding a registry hosted on S&P Platts" above.

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
