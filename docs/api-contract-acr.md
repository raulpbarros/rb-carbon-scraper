# American Carbon Registry — API contract

Measured against the live service on **2026-08-04**. Everything here was
observed, not inferred from documentation; where a figure is quoted it is what
the registry returned on that date.

## What it is

**ACR is not on APX any more.** Every older note — including this project's own
plan, which filed it as "APX ASP platform, form posts and HTML tables" — points
at `acr2.apx.com`. That host still answers, and that is the trap: it returns
HTTP 200 with

```html
<html><head><title>Invalid page</title></head>
<body>You have reached an invalid page.</body></html>
```

for `/`, and a plain 404 for every `/mymodule/...` report path. A scraper
written against it does not fail loudly; it finds nothing.

ACR's own site links elsewhere: `acrcarbon.org/acr-registry/public-reports/`
points every public report at **ICE GreenTrace**.

| | |
|---|---|
| Front end | `https://greentrace.ice.com/acr` |
| Backend | `https://greentrace.ice.com/api/greentraceservice/v1` — Spring Boot |
| Shape | ICE CMS "report centre": `POST {reportUrl}/results`, form-encoded |
| Auth | none. No key, no `Origin` check, no Cloudflare challenge |
| Projects | 994 |
| Credit records | 3,358 issuance blocks + 10,724 retirements + 1,358 cancellations |
| Requests per sync | ~1,005 (four are the ledgers; 994 are per-project details) |
| `robots.txt` | served, and empty — a `Sitemap:` line and no `Disallow` |

**GreenTrace is a platform, not a registry** — the fourth one this project has
met, after S&P Platts, legacy Markit and EcoRegistry. Its home page offers two
tenants:

| tenant | registry key | projects | ingested |
|---|---|---|---|
| ACR — American Carbon Registry | `ACR_REGISTRY` | 994 | yes |
| ART — Architecture for REDD+ Transactions | `ART_REGISTRY` | 30 | **no adapter yet** |

ART is the same API with one path segment changed — verified, not assumed:
`POST …/project/registry/ART_REGISTRY/project-summaries/results` returns its 30
projects in the same envelope. Adding it is a subclass of `GreenTraceAPI` with
two class attributes, the same shape as adding an S&P tenant. Check this table
before writing anything new.

## How the contract was found

The page is not the data, and neither is a network capture: the ICE CMS ships
its own component configuration inside the HTML. `GET /acr/projects` contains

```json
{"name":"cms-component-ice-report-center","props":{
  "tables":[{"name":"projects","columns":[…]}],
  "reportUrl":"/api/greentraceservice/v1/project/registry/ACR_REGISTRY/project-summaries",
  "reportType":"report-by-url","isPaginated":true,"rowsPerPage":20}}
```

and the component's own chunk
(`static.ice.com/cms/<version>/chunks/cms-components/ice-report-center/Main-*.js`)
publishes exactly how it calls that URL:

```js
fetch(`${reportUrl}/results`, {method:"POST",
      headers:{"Content-Type":"application/x-www-form-urlencoded"},
      body: qs({...values, offset, pageNumber, max})})
```

No Playwright, no guessing. One page fetch and one JS chunk.

## Endpoints

```
POST {api}/project/registry/ACR_REGISTRY/project-summaries/results   994 projects
GET  {api}/project/registry/ACR_REGISTRY/project-summaries/criteria  its filters
POST {api}/credit/registry/ACR_REGISTRY/holding-summaries/results    the ledgers
GET  {api}/project/registry/ACR_REGISTRY/project/{projectKey}        one project
POST {api}/credit/registry/ACR_REGISTRY/buffered-pool/results        745 reserve rows
POST {api}/public-account/registry/ACR_REGISTRY/results              public holdings
POST {api}/public-profile/registry/ACR_REGISTRY/results              public profiles
```

Request body, `application/x-www-form-urlencoded`:

```
offset=0&pageNumber=1&max=2000&holdingStatus=RETIRED
```

Response envelope:

```json
{"datasets": {"retiredCredits": {"rows": [ … ], "totalCount": 10724}}}
```

`totalCount` is the registry's own count for the whole report, restated on
every page, and is what reconciliation reads.

## The traps

### 1. The `reportUrl` is not an endpoint

`GET`ting it — the obvious first move — returns **HTTP 500**:

```json
{"status":500,"message":"No static resource v1/project/registry/ACR_REGISTRY/project-summaries.",
 "path":"/api/greentraceservice/v1/project/registry/ACR_REGISTRY/project-summaries"}
```

That reads like a broken service. It is Spring saying no handler is mapped for
that path. Only `/results` and `/criteria` exist under it.

### 2. The criteria must be a form body — nothing else works

The same values sent three ways:

| how | result |
|---|---|
| `POST …/results` with `application/x-www-form-urlencoded` | **200, the rows** |
| `POST …/results?offset=0&max=3&holdingStatus=RETIRED` | 500 |
| `POST …/results` with a JSON body | 500 |

All three 500s are the generic one above, so a wrong content type looks exactly
like a wrong path. `http_client.post_form` sets the header explicitly, because
the shared client sends `Content-Type: application/json` and it wins over the
one httpx would infer from a form body.

### 3. `max` clamps at 2000, silently

| asked | rows returned | `totalCount` |
|---|---|---|
| 100 | 100 | 994 |
| 1000 | 994 | 994 |
| 2000 | 2000 | 3358 |
| 2001 / 2500 / 5000 / 20000 | **2000** | 3358 |

HTTP 200 every time, no marker anywhere. The fifth registry here to ignore or
clamp a page size (Gold Standard clamps projects to 150, Bubble to 100, the
Markit view ignores it outright, and only BioCarbon honours one). So the pager
advances on `offset` against `totalCount` and never on the row count it got
back. Paging is stable: 3,358 issuance rows over two pages and 10,724
retirements over six came back **distinct, and their union equalled the stated
total exactly**.

### 3b. Sustained reading earns a 429 first, then a blanket 401

Two escalating refusals, both measured on 2026-08-04:

| what we did | what came back |
|---|---|
| ~93 requests at 1/s, then ~20 more minutes later | `429`, Cloudflare error 1015, `Retry-After: 3600`. Cleared after ~36 minutes |
| ~270 requests at one every 7 seconds | **`401 {"message": "Invalid API Key"}` on every API route**, including the report endpoints that had just served hundreds of pages |

**The 401 is not a contract change and not a credential we are missing.** The
site's own `/acr/projects` page was fetched immediately afterwards and is
byte-identical to the copy taken hours earlier — same CMS build (41.10.8), same
`reportUrl`, and no `apiKey`, `x-api-key`, `Authorization` or `token` string
anywhere in it. A browser still calls that endpoint with no key at all.
Retrying with the full browser header set, `sec-fetch-*`, an `Origin`, a
`Referer` and the site's own `__cf_bm` cookies made no difference.

So it is the platform declining to answer *this client*. It clears with time,
like the 429 did. The adapter raises `greentrace.GreenTraceBlocked` with that
explanation rather than an `HTTPStatusError` saying "401 Unauthorized", which
is what sends the next person looking for a key that does not exist.

**A stopped sync is resumed, not restarted**: the response cache keeps
everything already fetched and every database write is an idempotent upsert, so
re-running later continues from where it stopped. Do not answer this by raising
the request rate, and do not answer it by changing address.

### 4. One URL is four ledgers, and the unfiltered view is all of them at once

`holdingStatus` selects the ledger, and **the dataset key changes with it**:

| `holdingStatus` | dataset key | rows | units |
|---|---|---|---|
| *(absent)* | `holdings` | 16,385 | 379,674,647 |
| `ISSUED` | `issuedCredits` | 3,358 | 379,674,647 |
| `RETIRED` | `retiredCredits` | 10,724 | 57,360,584 |
| `CANCELED` | `cancelledCredits` | 1,358 | 187,414,534 |
| `BOGUS` | `holdings` | 0 | — |

The unfiltered view is the **whole holdings book**, not an "active units"
feed. Its 16,385 records partition by status —

| status | rows | units |
|---|---|---|
| ACTIVE | 3,659 | 89,712,900 |
| INACTIVE | 644 | 45,186,629 |
| RETIRED | 10,724 | 57,360,584 |
| CANCELED | 1,358 | 187,414,534 |

— and its RETIRED and CANCELED subsets are **key-identical** to the two
ledgers, so ingesting it as a fifth feed would restate every retirement and
cancellation. That is SocialCarbon's `asset` trap exactly. It is not ingested.

The balance it would have provided is published anyway, and it holds to the
unit: **ACTIVE + INACTIVE = 134,899,529 = issued − retired − cancelled.**

One thing here is *not* a trap: an unknown filter value returns **zero rows**,
not the whole index. Verra and Gold Standard both answer an unrecognised filter
with all 300k records; this one fails in the safe direction.

### 5. `issuanceQuantity` is the parent event's total, repeated per block

3,358 issuance rows carry only 2,337 distinct `issuanceKey`s — one issuance
event is split across up to four blocks, and every block restates the event's
full quantity.

| field summed over 3,358 rows | total |
|---|---|
| `issuanceQuantity` | 604,312,772 |
| `holdingQuantity` | **379,674,647** |

The second is correct: it matches the project list's own `issuedCredits`
summed over all 994 projects, exactly. Summing the blocks of one event always
equals that event's stated quantity (0 exceptions), which is what says the
field is the parent's and not a second opinion.

### 6. Cancellation here mostly means conversion, not destruction

| `cancellationReasonDisplayName` | rows |
|---|---|
| Convert to ARB Offset Credits | 1,152 |
| Removal from Registry | 162 |
| Compensation for Intentional Reversal | 21 |
| Convert to Ecology Offset Credits | 14 |
| Compensation for Unintentional Reversal | 9 |

1,166 of 1,358 are units leaving for California's or Washington's compliance
registry, where they continue to exist as ARB or Ecology offsets. They are
stored as cancellations, because that is what this registry calls them and what
its own figures count — and the reason is stored on every row, so reporting the
split is a query. Raised in `docs/field-mapping.md` rather than reinterpreted
here.

### 7. The state field mixes two vocabularies, sometimes in one value

`projectSiteLocState` over the 798 projects that appear in the issuance ledger:
792 uppercase names, 4 ISO 3166-2 codes, 2 absent. Multi-state projects join
them with commas, mixing both forms:

```
OHIO
US-CA
MISSOURI, US-GA, US-IN, US-TX, US-WI
```

Carried through as published, like every registry's vocabulary here.

### 8. No country *name* is published anywhere

The list states `country: "US"`, the detail states `projectSiteLocCountry:
"US"`, and the project report's `/criteria` offers no country filter at all —
`searchText`, `developer` and `projectType` are the only three. The site
renders the code as-is.

`country_code` is 994/994, so Continent derives cleanly (the Gold Standard
path). `País` stays blank rather than being invented — the first registry here
with that gap.

### 9. The detail is keyed on the project key, and the wrong id 404s quietly

| URL | result |
|---|---|
| `{api}/…/project/P2423FTH4Z22` | 200, the project |
| `{api}/…/project/ACR1275` | 404 |
| `{site}/acr/projects/registry/ACR_REGISTRY/project/ACR1275` | **200**, a CMS shell that renders an error |

The published reference (`ACR1275`) is not the key the routes take. The *page*
answers 200 for either, so a link built from the reference looks right in a
spreadsheet and is broken when a business user clicks it. Rows carry a
`detail_url` built from `projectKey`.

## Reconciliation

| what | against what | result |
|---|---|---|
| 994 projects | `totalCount` | exact |
| 3,358 / 10,724 / 1,358 ledger rows | `totalCount` per filter | exact, all three |
| ledger row keys | distinct within each ledger | 3,358 / 10,724 / 1,358 — no duplicates |
| issuance ledger summed per project | the list's own `issuedCredits` | **994 of 994 agree** |
| ledger `projectReferenceId`s | the project list | 0 orphans |
| detail `projectHoldingsTotalQuantity` / `…RetiredQuantity` | the ledgers | 45 of 45 sampled agree |
| ACTIVE + INACTIVE holdings | issued − retired − cancelled | 134,899,529 = 134,899,529 |

Repeated end to end through the adapter on **2026-08-05**: 994 projects,
3,359 / 10,725 / 1,358 ledger rows, each reconciling against its own
`totalCount`, and **0 orphan ledger rows**.

**The registry moved between the two runs, and the cross-check saw it.** One
issuance block and one retirement appeared overnight, and comparing each
project's detail-stated total against the ledger then disagreed on exactly one
project of 798 — `ACR1123`, by **129,908 units, the size of the new block**.
The detail had come from the response cache (fetched the day before) and the
ledger from the live API. That is a property of the cross-check, not a fault in
either feed: the two sources are read at different moments, and a project that
issues credits between them will differ by exactly what it issued. Worth
knowing before reading a single-project delta as a data-quality problem.

Because the registry's own per-project figures agree with the ledgers
everywhere they were checked, there is **no `iter_credit_totals`** for ACR.
Nothing is stated twice with two different answers, which is the only reason
that seam exists elsewhere.

## Record shapes

### Project (list row)

```json
{
  "projectId": "ACR1275",
  "projectKey": "P2423FTH4Z22",
  "projectName": "Line Bar Preserve Forest Carbon Project",
  "projectStatus": "ACTIVE",
  "projectMethodology": "ECY Compliance Offset Protocol: U.S. Forest Projects",
  "projectType": "Forest Carbon",
  "projectDeveloper": "Carbon Informatics LLC d/b/a/ Greenline Climate",
  "projectDeveloperProfileKey": "4003497147399405568:PROJECT_DEVELOPER",
  "creditingProgram": "Washington Department of Ecology",
  "country": "US",
  "issuedCredits": 0,
  "projectExtensionMap": {"complianceProjectId": null, "conservationEasement": "false"},
  "details":      {"type": "link", "url": "/acr/projects/registry/ACR_REGISTRY/project/P2423FTH4Z22"},
  "developerLink": {"type": "link", "url": "/acr/developer/4003497147399405568:PROJECT_DEVELOPER"}
}
```

`projectId` is `ACR` + digits on all 994, and the numeric parts are unique
(102–1275), which is what `projects.project_id` stores.

### Project (detail) — what the list does not carry

```json
{"projectDetail": {
  "projectSiteLocState": "US-OR", "projectSiteLocCity": null,
  "projectSiteLocLatitude": 44.51, "projectSiteLocLongitude": -120.28,
  "currentCreditingPeriodStartDate": "2026-07-10",
  "currentCreditingPeriodEndDate": "2051-07-09",
  "initialCreditingPeriodStartDate": "2026-07-10",
  "projectStartDate": "2026-07-10", "projectRegistrationDate": null,
  "estimatedAnnualCredits": null, "estimatedTotalCredits": 0,
  "projectMethodologyMethodologyKey": "ACR0020",
  "projectListingStatus": "Listed - Proposed Project",
  "hasAnotherCarbonProgram": false, "hasAnotherEnvironmentalMarket": false,
  "projectHoldingsTotalQuantity": 0, "projectHoldingsTotalRetiredQuantity": 0,
  "projectHoldingsTotalReserveQuantity": 0},
 "documents": {"datasets": {"publicDocuments": {"rows": [ … ]}}}}
```

Fill rates over the **full 994-project index**, measured after the complete
sync on 2026-08-05: crediting period **994/994**, methodology 994/994, state
**992/994**, `estimatedAnnualCredits` **987/994**, city **0/994**,
`estimatedTotalCredits` **0** on every project (the number, not a null),
`projectRegistrationDate` null throughout, and `hasAnotherCarbonProgram`
**true on 16 of 994** with `hasAnotherEnvironmentalMarket` true on 15 — which a
45-project sample had shown as zero, and which is the field a cross-registry
check reads first. See "What else holds these credits" below.

`documents` is the project's public PDFs — verification opinions, monitoring
reports. Not read.

### Credit row (all four ledgers share one shape)

```json
{
  "holdingKey": "H2323HK9K722",
  "holdingSerialNumber": "ACR-US-934-2024-2891-58384 to 58673",
  "holdingStatus": "RETIRED",
  "holdingQuantity": 290,
  "isReserve": false, "isEscrowed": false, "isSequestered": false,
  "projectKey": "P2323C9VRA23", "projectReferenceId": "ACR0934",
  "projectSiteLocationState": "LOUISIANA", "projectSiteLocationCountry": "US",
  "issuanceKey": "I3323C9XL44B", "issuanceQuantity": 59517,
  "issuanceDate": "2025-05-28", "vintage": "2024",
  "retirementKey": "R2323HK9K722", "retirementDate": "2026-07-31",
  "retirementReason": "ENVIRONMENTAL_BENEFIT",
  "retirementPurpose": "On behalf of ZGF Architects to offset 2025 emissions",
  "retirementOnBehalfOfName": "On behalf of ZGF Architects",
  "accountHolderProfileName": "CNaught Inc.", "registryAccountName": "ZGF Architects",
  "corsiaEligible": true, "arbEligible": "No",
  "registryLabels": [ … CORSIA and UN SDG label objects … ]
}
```

Retirement field fill rates over all 10,724 rows:
`registryAccountName` 10,724, `accountHolderProfileName` 10,724,
`retirementPurpose` 10,684, **`retirementOnBehalfOfName` 5,167**.

`retirementOnBehalfOfName` is what `beneficiary` stores — the third party the
registry itself names. `registryAccountName` is whoever holds the credits, very
often an intermediary retiring for its customers, which is BioCarbon's
`to_name` trap in another registry's field names. On the 4,774 rows where both
are present and differ, they are genuinely different parties.

`registryLabels` is why a retirement page is ~16 MB: every row carries its
CORSIA and SDG label objects in full.

## What else holds these credits

The registry states the question itself: `hasAnotherCarbonProgram` is true on
**16 of 994** projects. Checking those names against the 9,891 projects already
in the database — matching on rare name tokens and country, not on generic
words like "wind" or "wastewater" — turns up **two physical projects registered
in both Verra and ACR**:

| ACR | Verra | same? |
|---|---|---|
| `ACR0242` Cambria 33 Abandoned Mine Methane Capture, PA, 2018-06-22 → 2027-12-31, Vessels Carbon Solutions | `VCS 559` Cambria 33 Abandoned Mine Methane Capture and use, PA, 2008-05-01 → 2018-04-30, Vessels Coal Gas Inc. | yes — same mine, same operator, **consecutive** crediting periods |
| `ACR0388` Corinth Abandoned Mine Methane Recovery, IL, 2026 → 2035, DTM Clean Fuels (and `ACR1192`, the same project for 2015 → 2024) | `VCS 573` Corinth Abandoned Mine Methane Recovery, IL, 2010 → 2019 | yes — same mine, sequential periods, different proponent of record |

**The credit tranches do not overlap**, because the crediting periods do not:
Verra issued 234,591 and 610,404 units for the earlier periods, ACR 221,141 and
1,185,666 for the later ones. So neither row is a copy of the other, and this
is the same shape as Cercarbono's `CDC-106`/`CDC-107` against BioCarbon — the
only other known cross-registry duplicate in this database. Both rows ship,
cross-linked through `extra.also_registered_as`.

**One project is registered twice inside ACR**, and only one: Corinth appears
as `ACR0388` (2026 → 2035) and `ACR1192` (2015 → 2024). Two registrations of
one site for two crediting periods, with different ids and different
methodologies — not the merged-row case the legacy Markit view has, and
nothing is joined.

Both mine-methane projects also show the conversion story end to end: their ACR
issuances and cancellations are the **same number** (221,141 and 1,185,666),
because every unit was converted out to the ARB compliance registry.

## What is deliberately not read

| feed | rows | why not |
|---|---|---|
| unfiltered `holding-summaries` | 16,385 | the whole book; its RETIRED and CANCELED rows *are* the two ledgers, so it double-counts both. See trap 4 |
| `buffered-pool` | 745 | a subset of the same book (`isReserve`), 12,648,295 units. Reachable as a query once holdings are, and no column reads it today |
| `public-account` / `public-profile` | — | account-holder data, not projects or credits |
| `documents` on the detail | — | project PDFs |
| ART tenant | 30 projects | a different registry. It needs its own adapter, its own row set and its own decision — not ACR's rows |
