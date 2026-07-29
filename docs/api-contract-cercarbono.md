# Cercarbono (EcoRegistry) — API contract

Measured against the live service on **2026-07-28**. Everything below was
observed, not inferred from documentation; there is no published spec.

Cercarbono does not run its own backend. The registry at
`registry.cercarbono.com` is a Vite/React SPA in front of **EcoRegistry**, a
shared platform that also hosts other standards. The contract was derived with
plain HTTP plus a read of the site's own bundle — `verra discover` (Playwright)
was not needed.

## Endpoints

```
GET https://api-front.ecoregistry.io/platform/project/public-by-standard/cercarbono-co2
GET https://api-front.ecoregistry.io/platform/analytics/projects
GET https://api-front.ecoregistry.io/platform/analytics/get-retirements
GET https://api-front.ecoregistry.io/platform/project/public/<id>
```

Unauthenticated. No API key, no token, no cookie. The bundle's request builder
adds `Authorization: Bearer …` only on its private routes, which we never
touch.

Front end: `https://registry.cercarbono.com`, project page `/projects/<id>`.

## Volumes (2026-07-28, `cercarbono-co2`)

| Resource | Records | Requests | Notes |
|---|---:|---:|---|
| projects | 231 | 1 | whole list in one response |
| issuance serials | 2,529 | 1 | embedded in `analytics/projects` |
| retirements | 9,350 | 1 | whole ledger in one response |
| per-project detail | 231 | 231 | ~4 min at 1 req/s |

A full sync is **~234 requests, about four minutes** — the smallest registry so
far by two orders of magnitude. Registry-wide: 141,272,844 credits issued,
60,749,872 retired.

## Constraints — all measured, do not "optimise" past them

1. **Two headers are mandatory: `platform: ecoregistry` and `lng`.** Without
   them every call answers **HTTP 200** with

   ```json
   {"status":0,"codeMessages":[{"codeMessage":"ERROR_401","param":"invalid",
                                "message":"No autorizado"}]}
   ```

   That reads like a credential wall and is not one — the data is public and
   both headers ship in the site's own bundle. Same class of trap as Gold
   Standard's Cloudflare 403.

   Note the status code: **the refusal arrives as 200, in the body**. Checking
   `response.status_code` alone cannot see it, and an unnoticed `ERROR_401` is
   indistinguishable from an empty registry — no exception, no rows, no
   reason. The adapter raises on any `codeMessages` in a response.

2. **`Origin` is validated, and a wrong one returns HTTP 500.** Sending
   `Origin: https://registry.verra.org` — which is what the shared
   `settings.BROWSER_HEADERS` carries — produces a generic server fault that
   looks nothing like the CORS rejection it is. The adapter overrides `Origin`
   and `Referer` with Cercarbono's own site.

   This bit once: `http_client._send_once` merged headers into a plain `dict`,
   so httpx's lowercased `origin` and the adapter's `Origin` were *both* sent
   and the override silently did nothing. It merges through `httpx.Headers`
   now, which is case-insensitive.

3. **No paging anywhere.** Three of the four endpoints return their entire
   result set in one response. There is no page parameter to get wrong — and
   equally no page count to reconcile against, so the guard is that every
   project in the standard's own list must come back.

4. **The bulk feeds cover every standard, not just this one.** EcoRegistry
   hosts `cercarbono-co2` alongside `cercarbono-biodiversity` and
   `cercarbono-circular-economy`. `analytics/projects` and
   `analytics/get-retirements` return all of them mixed together — 7
   off-standard projects and 29 off-standard retirements at time of writing.
   Only the CO2 standard is ingested (its credits are tCO2e; the others are
   not), so both feeds are filtered against the CO2 project ids before
   anything is yielded.

5. **`analytics/projects` is not complete.** It omits projects converted in
   from another registry: `CDC-106` and `CDC-107`, both ex-BioCarbon, are
   absent from it entirely while appearing in the standard's list *and* in the
   retirement ledger. Driving projects from `analytics/projects` would lose
   them; summing only its serials shows them retiring credits they never
   issued. See "Issued totals" below.

6. **A serial can be published twice.** `CDC-196` lists its 2022 and 2023
   issuances under two serial revisions (`…_R6_…` and `…_R7_…`) with identical
   quantities, so its serial rows sum to 161,297 against a true 120,448. This
   is the registry's "never trust a row count" instance: the run finishes
   without an exception and the number is still wrong by 34%.

## Issued totals: three sources, and which one to believe

| Source | `CDC-1` | `CDC-196` | `CDC-106` |
|---|---:|---:|---:|
| sum of `analytics` serials | 81,169 | 161,297 | — (absent) |
| `certificatedVerification[].total` | 81,169 | **120,448** | **79,450** |
| `emitcertifications` available + cancelled | 81,169 | **120,448** | **79,450** |

`certificatedVerification` on the per-project detail is the registry's own
issued figure, and a third endpoint agrees with it on every case where the
serial rows do not. It is stored in `credit_totals`, which by design outranks
summing `credit_events` rows — the same mechanism Verra's `totals` command
uses, and for the same reason.

The per-serial rows are still stored: they carry the vintage, issuance date and
buffer flag that the total does not. They are just not what the spreadsheet
adds up.

Buffer credits **are** included. `certificatedVerification` counts them
(`CDC-1`: 81,169 includes 12,175 of buffer), so excluding them would disagree
with the registry's own published figure. The flag is kept in
`credit_events.unit_type` (`Buffer` / `Credit`) so splitting them out later
needs no re-scrape.

Retirement quantities carry an `is_kg` flag. No row in the live ledger uses it,
but the platform has kg-denominated retirement endpoints, so the adapter
converts rather than trusts — mixing kg and tCO2e in one column would be
silently wrong.

## Record shapes

### `project/public-by-standard/cercarbono-co2` — the authoritative list

```json
{
  "standardName": "Carbono", "standardId": 1, "creditUnit": "tCO2e",
  "projects": [{
    "id": 274, "code": "CDC-271",
    "name": "Vinaqua WWTP Carbon Project",
    "projectStage": "Verification",
    "sectors": [{"shortName": "CR", "description": "Waste handling and disposal"}],
    "methodology": [{"methodologyId": 88, "description": "CDM - AMS-III.H.: …",
                     "type_avoidance_removals": "Avoidance"}],
    "protocols": [{"description": "CVCC 4.5"}],
    "locationText": "South Africa",
    "developer": "Ammonite Environmental (Pty) Ltd"
  }]
}
```

`code` (`CDC-271`) is the human-facing reference; `id` (`274`) is an internal
key. **They do not agree** — using the id would give the business a reference
Cercarbono's own search does not find.

`sectors` and `methodology` are repeated once per verification, so a
single-sector project can publish `Land use (AFOLU)` three times. The adapter
de-duplicates while preserving order.

### `analytics/projects` — locations and issued serials

```json
{"project": [{
  "id": 1, "name": "Carbono Agroporvanda", "standard": "Carbono",
  "owner": "Agroporvanda S.A.S", "developer": "Forestry Consulting Group S.A.S.",
  "validator": "ICONTEC", "verifier": "ICONTEC",
  "evaluation_criteria": "PROTOCOL CVCC V1.1",
  "quantification_method": "CDM - AR-ACM0003: Afforestation and reforestation …",
  "stage": "Certified",
  "locations": [{"city": "Planeta Rica", "country": "Colombia",
                 "region": "Cordoba", "data_map": {"latitude": 8.41, "longitude": -75.58}}],
  "serials": [{"serial": "CDC_1_1_1_321_14_1", "issued_quantity": 62740,
               "issuance_date": "2019-03-07T18:33:46.000Z", "year": 2015,
               "is_buffer": false, "vintage_of_credits": "2010-01-01 / 2018-09-30",
               "elegible": [{"description": "Colombian Carbon Tax"}]}]
}]}
```

**`region` and `city` are `"Worldwide"` on 575 of 811 locations** — the
placeholder EcoRegistry stores when neither was entered, always as both at
once and always beside a real country. It is a blank, not a place; writing it
into Estado/Cidade would state a location the registry never published. The
adapter drops it.

`elegible` is market eligibility ("Colombian Carbon Tax", "Voluntary
compensation"), **not** a co-certification, and is deliberately not fed to the
Additional Certification column.

An issuance has **no numeric id**, only the serial string. `credit_events` is
keyed on an integer, so the key is a hash of the serial — stable across runs,
which is what keeps the upsert idempotent.

### `analytics/get-retirements` — the whole ledger

```json
{"retirements": [{
  "id": 1, "project_id": 1, "serial": "CDC_1_1_1_321_14_1",
  "quantity": 1000, "date": "2019-03-07T18:33:46.000Z", "vintage": "2010-01-17",
  "is_kg": 0, "final_user": "Primax Colombia S.A", "country_final_user": "Colombia",
  "reason_using": {"id": 1, "description": "…"}
}]}
```

Cercarbono publishes the retirement beneficiary (`final_user`) and reason,
which Gold Standard does not. That is what a beneficiary-based reading of
"sold" would need if the business ever asks for it.

### `project/public/<id>` — the only source of the crediting period

```json
{"project": {"id": 1, "code": "CDC-1", "hashId": "…",
             "periodInit": "2008-01-16", "periodEnd": "2032-01-15",
             "accreditationPeriod": "2008-01-16 / 2032-01-15",
             "standarDescription": "Carbono", "creditUnit": "tCO2e",
             "convertedFromDescription": "BioCarbon"},
 "certificatedVerification": [{"total": 81169, "verificationNumber": 1}],
 "locations": [{"regionDescription": "Cordoba", "cityDescription": "Planeta Rica",
                "area": "353.583"}]}
```

Nothing else publishes `periodInit` / `periodEnd`, which is why this endpoint
is called once per project despite the other three being bulk. It fills
`Data de Início`, `Data de Término` and, through them, `Duração`.

`convertedFromDescription` names the registry a project was migrated from —
worth keeping visible, because the same project may still exist there and
counting both would double it. Relevant before adding **BioCarbon** in Phase 5.

## Vocabulary

`sectors[].description`, carried through untranslated as `Tipo Macro de
Projeto` — Cercarbono's own words, like every other registry's:

| Sector | Projects |
|---|---:|
| Land use (AFOLU) | 84 |
| Energy industries | 78 |
| Waste handling and disposal | 15 |
| *(none stated)* | 13 |
| Fugitive emissions from fuels | 3 |
| Manufacturing industry | 2 |
| Transport | 2 |
| Ecosystem conservation and preservation | 1 |
| Technological cycle | 1 |

`projectStage` (→ `Status`): `Certified`, `Verification`, `Validation`,
`Not assigned`, `Retired`, `Finished`, `Public comments`, `Suspended`,
`Cancelled`, `Rejected`, `Registration`.

`"Not defined"` / `"No definido/Not defined"` are the registry saying it has no
value. They are treated as blank rather than written into a cell as if they
were one.

## Fields Cercarbono does not publish

Measured over the full 231-project CO2 index. These stay blank — nothing is
inferred.

| Column | Fill | Note |
|---|---|---|
| Yearly Ex Ante | 0 / 231 | Not published. Never back-computed from issuances, which are actuals, not an estimate. |
| Total Ex Ante | 0 / 231 | Follows from the above. |
| Additional Certification | 0 / 231 | No equivalent field; `elegible` is not one. |
| Total Credits Cancelled | 0 / 231 | **No cancellation ledger is published.** Blank, not zero. |
| Estado | 108 / 231 | `region` is the `"Worldwide"` placeholder on the rest. |
| Cidade | 107 / 231 | Same. |
| Bioma | 92 / 231 | Only meaningful for land-use projects; the ruleset is limited to them on purpose. |
| Metodologia | 218 / 231 | 13 projects state no methodology. |
| Tipo Macro / Micro / Durabilidade | 218 / 231 | Same 13. |
| Data de Início / Término | 220 / 231 | 11 projects have no crediting period yet. |
| Continent | 231 / 231 | **No ISO country code is published anywhere**, so Continent is derived from the country *name*; see `config/derivation/continent.yaml`. |

## Politeness

Same rules as the other registries: ~1 req/s, exponential backoff, on-disk
response cache. A full sync is ~234 requests, so this registry costs the
operator almost nothing. Do not raise concurrency.
