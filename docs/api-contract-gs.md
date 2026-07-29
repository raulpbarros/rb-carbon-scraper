# Gold Standard Impact Registry — API contract

Measured against the live service on **2026-07-28**. Everything below was
observed, not inferred from documentation; there is no published spec.

Unlike Verra, this contract was derived with plain HTTP requests — the site is
a Vite/React SPA but its backend is ordinary REST, so `verra discover`
(Playwright) is not needed here.

## Endpoints

```
GET https://public-api.goldstandard.org/projects?page=<n>&size=150
GET https://public-api.goldstandard.org/credits?page=<n>&size=25
```

Unauthenticated. No API key, no token, no cookie. The site's own JS also
references `X-Registry-API-key` and `Authorization`, which belong to the
logged-in portal — we never touch those.

Front end: `https://registry.goldstandard.org`, project page
`/projects/details/<id>`.

## Volumes (2026-07-28)

| Resource | `X-Total-Count` | Pages at max size | Notes |
|---|---:|---:|---|
| `projects` | 4,141 | 28 | ~1 min at 1 req/s |
| `credits` | 182,989 | 7,320 | ~2 h at 1 req/s |

`X-Total-Number-Of-Credits` on the credits endpoint reported **499,197,628**
credits registry-wide — an independent cross-check on locally aggregated
issuance totals.

## Constraints — all measured, do not "optimise" past them

1. **A browser `User-Agent` is required.** Without one every request returns a
   Cloudflare 403 HTML page. This is the fastest way to conclude the API is
   private when it is not.

2. **`size` is capped per resource, and the caps behave differently.**
   - `projects`: asking for 200, 300, 500 or 1000 all return **150 items**,
     silently, with no error and no marker in the body. A naive `size=1000`
     loop would walk 28 pages' worth of ids while skipping 85% of the
     registry.
   - `credits`: anything above **25** returns HTTP **403**. It reads like a
     block but is a response-size limit — credit records embed their whole
     project object, descriptions included.

3. **Filters that are not understood are silently ignored** — the same trap as
   Verra. Measured:

   | Query param | Result |
   |---|---|
   | `?project_id=1890` | `X-Total-Count: 182989` — the whole index |
   | `?status=RETIRED` | `X-Total-Count: 182989` — the whole index |
   | `?query=<nonsense>` | `X-Total-Count: 0` — honoured |
   | `?query=GS7495` | `X-Total-Count: 48` — honoured |

   Only `query` was proven to narrow. Never partition on the others.

4. **Ordering is by descending `id` and stable across pages.** There is no
   Elasticsearch result window: page 7000 returns normally. Plain sequential
   paging is safe here, unlike Verra.

5. **Counts live in headers, not the body**: `X-Total-Count`,
   `X-Total-Number-Of-Credits`. The response cache preserves `x-total-*`
   headers for exactly this reason.

## Record shapes

### `/projects`

```json
{
  "id": "5672",
  "name": "GS23711- Mozambique Off Grid Electrification VPA1",
  "status": "LISTED",
  "gsf_standards_version": "Gold Standard for the Global Goals",
  "estimated_annual_credits": 9000,
  "crediting_period_start_date": "2026-06-26",
  "crediting_period_end_date": "2031-06-25",
  "methodology": null,
  "type": "Other",
  "size": "Micro Scale",
  "sustaincert_id": 23718,
  "sustaincert_url": "https://assurance-platform.goldstandard.org/project-documents/GS23718",
  "project_developer": "Anthesis B.V.",
  "carbon_stream": "GS_VER",
  "country": "Mozambique",
  "country_code": "MZ",
  "state": null,
  "labels": [...],
  "sustainable_development_goals": [...]
}
```

**`sustaincert_id` is the canonical human reference**, not the name. The
project above is named `GS23711-…` but is itself `GS23718` — the name carries
its parent programme's number. Parsing the name would mislabel it.

### `/credits`

```json
{
  "id": "595379",
  "number_of_credits": 29743,
  "serial_number": "GS1-1-KE-GS7495-16-2025-30645-608-30350",
  "vintage": "2025",
  "status": "ISSUED",
  "certified_date": "2026-07-28",
  "product": "...",
  "labels": ["EMISSION_REDUCTION"],
  "project": { "id": "1890", "name": "...", ... }
}
```

Each credit block **embeds its whole project object**, so the credit stream
carries its own project linkage — no per-project fan-out is needed.

**`status` is the block's current state, not an event type.** Observed values:
`ISSUED`, `RETIRED`. A block that was issued and later retired reads
`RETIRED`, so issued totals are the sum of *every* block regardless of status.
Summing only `status='ISSUED'` would report zero issued credits for any
project that has since retired them. See `db.credit_totals()`.

`labels` on a credit is its product class (`EMISSION_REDUCTION`), **not** a
co-certification, and is deliberately not fed to the Additional Certification
column.

## Vocabulary

`type` is a closed list of 20 values (full sync, 2026-07-28):

| Type | Projects | | Type | Projects |
|---|---:|---|---|---:|
| Energy Efficiency - Domestic | 2,232 | | Biomass, or Liquid Biofuel - Heat | 38 |
| Wind | 380 | | Biomass, or Liquid Biofuel - Electricity | 38 |
| Other | 350 | | Energy Efficiency - Transport Sector | 37 |
| Biogas - Heat | 253 | | Solar Thermal - Heat | 31 |
| A/R | 171 | | Energy Efficiency - Industrial | 29 |
| Solar Thermal - Electricity | 161 | | Biomass, or Liquid Biofuel - Cogeneration | 28 |
| Small, Low - Impact Hydro | 156 | | Geothermal | 19 |
| Biogas - Electricity | 114 | | Energy Efficiency - Commercial Sector | 11 |
| PV | 78 | | Energy Efficiency - Public Sector | 7 |
| | | | Biogas - Cogeneration | 6 |
| | | | Energy Efficiency - Agriculture Sector | 2 |

## Fields Gold Standard does not publish

Measured over the full 4,141-project index. These stay blank — nothing is
inferred.

| Column | Fill | Note |
|---|---|---|
| Cidade | 0 / 4,141 | No city field exists. |
| Estado | 0 / 4,141 | `state` is present in the schema but null throughout. |
| Metodologia | 2,482 / 4,141 | Carried through where published; often null on newly listed projects. |
| Additional Certification | 0 / 4,141 | No equivalent field. |
| Continent | 4,120 / 4,141 | Derived from `country_code`. The 21 gaps are `XZ` / "International" multi-country projects, which have no single continent. |

## Politeness

Same rules as Verra: ~1 req/s, exponential backoff, on-disk response cache. A
full credits sync is ~7,320 requests. Do not raise concurrency.
