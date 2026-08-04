# BioCarbon Registry — API contract

Measured against the live service on **2026-08-04**. Everything here was
observed, not inferred from documentation; where a figure is quoted it is what
the registry returned on that date.

## What it is

**BioCarbon Registry publishes through Global CarbonTrace**, a Laravel API
behind a Vue 3 SPA. `biocarbonregistry.com` no longer resolves; the live
system is:

| | |
|---|---|
| Front end | `https://globalcarbontrace.io` |
| Backend | `https://api.globalcarbontrace.io` |
| Shape | plain REST GET, Laravel paginator |
| Auth | a public `x-api-key` shipped in the site's own bundle |
| Projects | 105 (GHG programme) |
| Credit records | 626 issuance blocks + 11,439 retirements + 3 cancellations |
| Requests per sync | ~225 |
| `robots.txt` | not served |

**It is a platform, not a registry.** The bundle declares one programme —
`biocarbon` — with three categories:

| category | slug | projects | credits |
|---|---|---|---|
| GHG | `gei` | 105 | all of them |
| Biodiversity | `biodiversity` | 3 | none |
| Water | `water` | 0 | none |

Only `gei` is tCO2e and only `gei` is ingested, the same call Cercarbono's
adapter makes about EcoRegistry's biodiversity and circular-economy standards.
The routing table is in `assets/CategoryView-*.js`, which is where these
category slugs and their endpoints were read from.

## Authentication — a 403 wearing an HTTP 200

```
GET https://api.globalcarbontrace.io/api/ghg/projects
→ HTTP 200
{"status": 403, "data": [], "message": "You do not have permissions to perform this action."}
```

The refusal arrives **inside a 200 body**. `response.raise_for_status()` sees
nothing, and `data: []` is indistinguishable from an empty registry — the same
trap as EcoRegistry's `ERROR_401` and the Platts platform's
`totalEntities: 0`. The adapter raises on any body whose `status` is ≥ 400 and
drops it from the response cache first, because a 4xx is not cached but a 200
is.

The key that makes it work is published:

```js
// assets/api-B8aTk_WB.js
const kt = "https://api.globalcarbontrace.io",
      Qt = "SboCiHaHxtC2xRM92hpBjy1S2Y5La7IwjeB76z",
      Ct = () => ({ Accept: "application/json", "x-api-key": Qt });
```

It is a public client key sent by every anonymous visitor to the registry's own
public pages — the same posture as Verra's `appkey`. We send exactly what the
browser sends and nothing more, and we do not attempt any authenticated route.
`settings.BIOCARBON_API_KEY` reads `CARBON_BIOCARBON_KEY` first, so a rotation
is a config change rather than a release.

No `User-Agent` requirement, no Cloudflare, no `Origin` validation observed.
The adapter sends its own site's `Origin`/`Referer` anyway, because
`settings.BROWSER_HEADERS` carries Verra's and that has already cost an
afternoon on Cercarbono.

## Endpoints

Two prefixes serve the same GHG data:

```
GET {api}/api/public/{initiatives|carbon-credits|retreats|transferences}
GET {api}/api/ghg/{projects|carbon-credits|retreats|transferences}
```

Totals are identical on all four resources (105 / 626 / 11,439 / 714). Only
`ghg` carries the per-project routes, so the adapter speaks `ghg` throughout
rather than mixing two names for one thing.

```
GET {api}/api/ghg/projects?per_page=1000&page=1        the project list
GET {api}/api/ghg/projects/{id}                        one project's detail
GET {api}/api/ghg/carbon-credits?per_page=1000         the issuance ledger
GET {api}/api/ghg/retreats?per_page=1000               the retirement ledger
GET {api}/api/ghg/carbon-credits/project/{id}/cancellations
GET {api}/api/ghg/{projects|carbon-credits|retreats}/relations   filter values
GET {api}/api/impact-stats/get-stats                   the registry's headline figures
```

`relations` returns the dropdown vocabularies (developers, countries,
methodologies) rather than records. It is not read.

### The paginator

Every list response is Laravel's:

```json
{"current_page": 1, "data": [...], "last_page": 12, "per_page": 1000,
 "total": 11439, "from": 1, "to": 1000, "next_page_url": "...?page=2"}
```

`total` is the registry's own count and is what reconciliation reads.

**`per_page` is honoured.** Verified at 100, 200, 500, 1000, 2000 and 5000 —
each returns exactly that many rows with `last_page` adjusted to match. This is
the first registry in this project that does *not* silently clamp or ignore a
page size (Gold Standard clamps `projects` to 150, Bubble clamps to 100, the
legacy Markit view ignores it outright). 1000 is politeness, not a ceiling.

Ordering is stable across pages: all 11,439 retirement serials came back
distinct, and all 626 issuance serials.

## Reconciliation — the registry publishes its own totals

```
GET {api}/api/impact-stats/get-stats
{"success": true, "totalProjects": 54, "pendingProjects": 33,
 "emmitedCredits": 85177570, "retreatCredits": 50157520}
```

Both credit figures match the ledgers **to the unit**:

| | ledger sum | `impact-stats` |
|---|---|---|
| issuances (626 blocks, `amount`) | 85,177,570 | 85,177,570 |
| retirements (11,439 rows, `sold`) | 50,157,520 | 50,157,520 |

`totalProjects: 54` is not the project count — it counts only the registered
ones. The paginator's `total: 105` is the index.

## The traps

### 1. `transferences` is not a ledger

714 rows, 20,406,121 units, each a holder-to-holder move of units **already
issued**:

```json
{"id": 1005, "serial": "BCR-CO-261-14-001-2-2101-2112-0267454-0276278",
 "initiative_id": 22, "transmitter_holder": "South Pole …",
 "receiver_holder": "CLIMA NEUTTRO SAS", "amount": 8825, "created_at": "27/07/2026"}
```

Scraping it as a credit event adds a fifth bucket that double-counts the
issuances — the same shape as SocialCarbon's `asset` feed. It is not in
`LEDGERS`.

### 2. Two feeds disagree about cancellations, and the fuller one has no dates

The per-project `cancellations` endpoint publishes **3 rows across 2 projects**
(477,859 units). Meanwhile **14 issuance blocks across 9 projects** carry a
nonzero `dropouts`, totalling **584,940**.

The block field is the registry's own arithmetic — `amount = active + outof +
dropouts` holds on every block that has one:

| serial | amount | active | outof | dropouts |
|---|---|---|---|---|
| `PCR-CO-ATI-14-001-2-1012-1712-…` | 554,328 | 0 | 80,282 | 474,046 |
| `PCR-CO-ECO-14-001-1-0805-1012-…` | 19,067 | 0 | 8,474 | 10,593 |
| `BCR-CO-259-14-005-2-2310-2312-…` | 285,913 | 285,072 | 0 | 841 |

So the endpoint's rows are stored (they carry the cancellation dates, which
the blocks do not) and `iter_credit_totals(cancellations)` states the
`dropouts` figure, which outranks them in `db.credit_totals()`. Same seam and
same reason as Cercarbono's `certificatedVerification`.

There is no bulk cancellation feed and no published cancellation total, so
this is the one ledger with nothing to reconcile against. Every project is
swept, not only those whose blocks show a dropout: narrowing on our own
reading of another feed is how a registry's own records go missing.

### 3. `verified_reductions` is not an issued total

The detail states a per-project `verified_reductions`, and for 103 of 105
projects it equals the sum of that project's issuance blocks exactly. The two
that disagree are what settles it:

| project | stated verified | blocks sum |
|---|---|---|
| `BCR-TR-152-1-001` | 322,687 | **0 — no blocks at all** |
| `BCR-CO-635-14-003` | 477,625 | 477,623 |

Verification precedes issuance, so a project can have verified units and none
issued. The registry's own `emmitedCredits` agrees with the ledger, not with
the verified sum — so the ledger is authoritative for `Total Credits Issued`,
and `verified_reductions` goes to `extra`. This is the **opposite** error to
Cercarbono's, where the ledger was the incomplete one; only comparing both
against the registry's own headline figure tells you which way round it is.

### 4. Country names are in the registry's own two languages

`country` reads `Colombia`, `Nigeria` and `Ecuador` beside `Malasia`, `Perú`,
`México`, `Panamá`, `Turquía`, `Brasil` and `Estados Unidos`. No language
switch exists — `Accept-Language`, `lng` and `?lang=` all return the same
strings, which are simply what was typed into the registry.

`country_iso` is published on **105 of 105**, so Continent derives from the
ISO code (the Gold Standard path) and is unaffected. `config/derivation/
biome.yaml` reads the *name*, and now carries both spellings; the blast radius
of adding them, measured against the real database before the change, was
**zero existing rows**.

### 5. One project's crediting period ends before it starts

`BCR-NG-657-14-001`: `quantification_period_start: 2045-01-09`,
`quantification_period_end: 2019-01-07`. The registry's own data error, stored
as published. Nothing is swapped or repaired.

## Record shapes

### Project (list)

Every field below is filled on **105 of 105** unless noted.

```json
{"id": 20, "consecutive": 20, "project_id": "BCR-CO-319-14-004",
 "project_name": "El Tigre REDD+", "country": "Colombia", "country_iso": "CO",
 "status": "Registered", "status_code": "Registered",
 "sector_name": "Agriculture, forestry and other land uses (AFOLU)",
 "type_project_name": "Reduced emissions from deforestation & degradation",
 "holder_name": "Resguardo Indígena Guahibo…, Carbosostenible S.A.S., …",
 "holder": {"id": 6, "holder": "Carbo Sostenible S.A.S."},
 "methodologies": [{"code": "BCR0002", "name": "BCR0002_Cuantificación … REDD+"}],
 "quantification_period_start": "2018-06-30",   // 99/105
 "quantification_period_end": "2048-06-29",     // 99/105
 "duration": 30,                                 // 88/105
 "verified_reductions": "751,927",               // 47/105
 "ovv": "AENOR Confia S.A.U."}                   // 65/105
```

`status` comes from a closed lifecycle vocabulary: `Registered` (47),
`Listed` (26), `Declined` (16), `De-registered` (7),
`Registration Request Under Review` (7), `Withdrawn` (2). **All of them are
ingested** — Verra's rows carry `Withdrawn` and `Rejected by Administrator`
too, and the `Status` column is what says which.

### Project (detail) — what the list does not carry

```json
{"code": "200", "status": "success", "initiative": {
  "applicable_standard": "BioCarbon Standard",     // 105/105
  "participants": "…\r\n…",                        // 105/105
  "localitation": "The project is located in the villages of El Tigre…",  // 105/105
  "description": "The Tigre REDD+ project…",       // 105/105
  "total_reductions_general": 1719967,             // 41/105
  "total_reductions": 252445,                      // 69/105
  "confirm_migration": "NO",                       // 63/105
  "migration_standard_name": null,                 // 2/105
  "unification": "NO",                             // 105/105
  "latitude": null, "longitude": null}}            // 16/105
```

**`total_reductions_general` is the ex-ante total and `total_reductions` is
not.** The registry's own certificate template settles it:

> "The result is **:total_reductions_general** tCO2e during the project's
> quantification period (:duration years). The total GHG reductions and
> removals verified during this monitoring period are **:total_reductions**
> tCO2e."

So the first is an estimate over the whole period and the second is a verified
figure for one monitoring period. `total_reductions_general` becomes
`exante_quantity`; `total_reductions` goes to `extra`. **No yearly figure is
published**, and the total is never divided by the duration to manufacture one.

### Issuance block

```json
{"serial": "PCR-CO-BFX-14-002-2-2101-2112-0000001-0299564",
 "amount": "299,564", "active": "0", "outof": 299564, "dropouts": "0",
 "sold": "299,564", "initial_vintage": "2021-01-01", "final_vintage": "2021-12-31",
 "destination": "Impuesto", "year": "2021", "date_issue": "2026-05-13",
 "initiative_id": 7, "project_id": "PCR-CO-BFX-14-002", "country": "CO"}
```

Quantities are **thousands-separated strings**: `float("299,564")` raises, so
the separator is stripped before parsing. `destination` is the unit class, in
the registry's two languages: `Impuesto` (266), `Reserva` (254), `reserved`
(101), `Voluntario` (5). Reserve units are counted in the issued total because
the registry's own published figure counts them; the class is stored in
`credit_events.unit_type`, so splitting them out is a `config/credits.yaml`
change and not a re-scrape.

### Retirement

```json
{"serial": "BCR-CO-259-14-002-2-2401-2409-0043804-0043903", "sold": "100",
 "reason": "Compensación voluntaria para Carbono Neutralidad…",
 "to_name": "Enterprise Management Services S.A.S.",   // 11,439/11,439
 "final_user": "EMPRESA COLOMBIANA DE CEMENTOS S.A.S.", // 9,174/11,439
 "market": "voluntary", "destination": "tax",
 "created_at": "03/08/2026", "initiative_id": 20,
 "data_visibility": "public"}
```

**`final_user` is the beneficiary; `to_name` is not.** `to_name` is filled on
every row and names the account the units were retired *to* — very often a
fuel distributor retiring on behalf of its customers (`ORGANIZACIÓN TERPEL
S.A.` appears thousands of times with a different `final_user` on each row).
Reading it as the beneficiary would make every retirement look like a
third-party sale the moment `sold_equals_retired` is flipped. Same call as
SocialCarbon's `Beneficiary` over `Retiree`.

The bulk feed carries **no `id`** — the per-project route does, the bulk one
does not — so `entity_id` is hashed from the serial, which is unique across all
11,439 rows.

`data_visibility` marks 7,033 rows `private` and 4,406 `public`, and the
public API returns `final_user` on both. The flag is stored in
`credit_events.status` beside the name rather than used to drop it, which
keeps honouring it a query rather than a re-scrape.

### Cancellation

```json
{"serial": "BCR-CO-259-14-005-2-2310-2312-0000001-0000841", "amount": "841",
 "year": "2023", "initial_vintage": "2023-10-01", "final_vintage": "2023-12-31",
 "cancellation_date": "2026-06-26"}
```

Reachable only per project, and the response is `{"status": 200, "data": [...]}`
— not a paginator.

## What is deliberately not read

* `transferences` — see trap 1.
* `biodiversity` and `water` — not tCO2e.
* `/api/{program}/{resource}/export` — exists, POST-only (a GET answers 405).
  The paged JSON is 14 requests for everything and richer than a CSV would be,
  so there is nothing to gain.
* `holder_email` and `holder_nit` on the ledgers — Laravel-encrypted blobs, not
  readable and not wanted.
* The whole authenticated panel API (`/api/accounts`, `/api/holders`,
  `/api/invoices`, …), which the bundle also declares. Not touched.
