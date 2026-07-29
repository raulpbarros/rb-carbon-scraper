# Plan Vivo Climate Registry — API contract

Measured against the live service on **2026-07-28**. Everything below was
observed, not inferred from documentation.

Plan Vivo does not run its own backend. `registry.spglobal.com/pvclimate` is
one tenant of **S&P Global's "Carbon Registry" (Platts) platform** — the same
platform, the same host and the same request shape as Verra. Three header
values differ and nothing else does.

**This document is the short one on purpose.** The platform's measured
constraints — the 400-row page limit, the 10,000-row Elasticsearch window, the
mandatory `entityId` sort, the silently-ignored-filter trap — are written up
once in `docs/api-contract.md` and implemented once in
`registries/platts/api.py`. They all apply here. Only the differences follow.

## Identity

```
registry:         PVCL
standardid:       671000000000001
standardacronym:  PV
language:         en
```

Read, not guessed:

```
GET {cmsResources}/public/standardsByRegistry/PVCL
[{"id":"671000000000001","name":"PV Climate","metaData":"PV",
  "publicReportExport":{"ACCOUNTS":true,"PROJECTS":true,"ISSUANCES_LISTINGS":true,
                        "HOLDINGS":true,"RETIREMENTS":true,"ASSIGNMENTS":true,
                        "CANCELLATIONS":true,"NOT_DELIVERED":true,"BUFFERS":false}}]
```

Unauthenticated, one GET, and the only published source of the `standardId`.
`verra standards -r planvivo` prints it.

**This matters more than it looks.** A wrong-but-plausible `standardId` — an
earlier attempt used `100000000000004`, taken from a legacy Markit URL —
returns **HTTP 200 with `totalEntities: 0`**. No error, no exception, no log
line: indistinguishable from a registry with no projects. Omitting the header
entirely gives the familiar generic 500 instead, which is at least visible.

`publicReportExport` is also how the ledger set was decided rather than copied
from Verra. Plan Vivo publishes the same four we scrape, plus `ASSIGNMENTS` and
`NOT_DELIVERED`, which have no `publicReportPageSearch` resource (both answer
500) and are not credit events we model.

## Endpoints

```
POST {raasReportPublicManager}/{resource}/publicReportPageSearch
POST {raasReportPublicManager}/{resource}/aggregateResultBySearchFilter
GET  https://registry.spglobal.com/config/environment.config.json
GET  {cmsResources}/public/standardsByRegistry/PVCL
```

`resource` is `project`, `issuances`, `holdings`, `retirements` or
`cancellations`. Base URIs come from `registry.spglobal.com`'s own routing
table, which today is byte-identical to `registry.verra.org`'s — read
separately anyway, so that a future split costs nothing.

Front end: `https://registry.spglobal.com/pvclimate/public/pv`, project page
`/pvclimate/public/pv/projects/<projectId>`. That route was read off the app's
own router (`/:context/public/:standardParam/projects/:projectId`) and
confirmed by loading a project.

## Volumes (2026-07-28)

| Resource | Records | Registry's own figure |
|---|---:|---|
| projects | 2 | "Projects 2" on the public page |
| issuances | 27 | — |
| holdings | 10 | — |
| retirements | **0** | "Retired Units 0" |
| cancellations | **0** | — |
| accounts | 7 | "Accounts 7" |

A full sync is **7 requests, a few seconds**. Nothing here comes close to the
10k window, so no partitioning is ever attempted and `verra totals` has nothing
to fix — every ledger pages in one request.

Registry-wide sums, straight from the server-side aggregate:

| Ledger | Sum |
|---|---:|
| issuances | **213,145** — matches "Issued Units 213,145" on the public page exactly |
| holdings | 136,297 |
| retirements | 0 |
| cancellations | 0 |

This is a young registry (first project activated 2025-04-30), not a broken
scrape. Both empty ledgers are wired anyway: an empty ledger is a fact about
the registry today, and the day it fills nothing needs changing.

## Where the published data differs from Verra

Same envelope, same field names, but three fields Verra populates are null
throughout — and one Verra leaves empty is populated here.

| Field | Verra | Plan Vivo |
|---|---|---|
| `sectoralScope` | the vocabulary | **null** |
| `projectType` | unused | **the vocabulary** — "Afforestation / Reforestation" |
| `vcsProjectId` | `VCS1234` | **null** — no human reference is published |
| `regionName` | a region | **null** |
| `methodologies` | populated | **null** (0/2) |
| `avgAnnualVolVcu`, `exanteQuantity` | populated | **null** |

Consequences, all handled in `registries/planvivo/api.py`:

- **`Tipo Macro de Projeto` comes from `projectType`**, carried through
  untranslated like every other registry's vocabulary. The map is rebuilt with
  `sectoralScope` *removed* rather than shadowed — two platform fields writing
  one column would let dict order decide whether a null overwrote a real value.
- **`Project ID` falls back to the numeric `projectId`.** Nothing else in the
  record is a human-facing reference, and the numeric id is what the
  registry's own public URL uses, so the cell stays checkable.
- **`Continent` is derived from the country name**, the same path Cercarbono
  takes. No ISO code is published either.

## Unit classes: the reserves are not a separate ledger

An issuance states both a `unitType` and a `unitClass`, and they are not the
same thing:

| `unitClass` | Records | Quantity | |
|---|---:|---:|---|
| fPVC | 3 | 103,246 | forward credits |
| rPVC | 6 | 50,220 | |
| Future Risk Buffer | 9 | 41,286 | |
| Achievement Reserve | 9 | 18,393 | |

`unitType` reads the same across a project's reserve and non-reserve rows, so
recording it alone would make the reserves invisible. The adapter records
`unitClass` (holdings spell it `unitClassName`) into `credit_events.unit_type`,
which keeps the split available without a re-scrape — the same reasoning as
Cercarbono's buffer flag. Verra is untouched and still records the bare
`unitType`.

**`Total Credits Issued` counts all four classes: 213,145.** That is the
registry's own published figure, and disagreeing with the number on Plan Vivo's
public page would be worse than the alternative readings.

**Worth raising with the business, not silently reinterpreted:** the platform's
own unit-type lookup flags `fPVC` as `isVerified: false` and `rPVC` as
`isVerified: true`. fPVC are forward credits — issued against future
sequestration. Nearly half of Plan Vivo's issued units (103,246 of 213,145) are
fPVC. Verra keeps that idea in a separate `Total Ex Ante` estimate column;
Plan Vivo issues them as real registry units. If the business wants them split
out, the class is already stored and it is a `config/credits.yaml` change, not
a re-scrape. Until then the registry's own total stands.

## Record shapes

### `project/publicReportPageSearch`

```json
{"entities": [{
  "entityId": 110200000000034, "projectId": 110200000000034,
  "standardId": 671000000000001, "standardName": "PV Climate",
  "projectName": "Kukumuty", "status": "Active", "stateCode": "ACTIVE",
  "projectType": "Afforestation / Reforestation", "sectoralScope": null,
  "vcsProjectId": null, "regionName": null, "methodologies": null,
  "countryName": "Mozambique", "stateProvince": "Sofala", "city": "Mangunde",
  "latitude": -20.196, "longitude": 33.713,
  "creditPeriodStartDate": "2022-05-01T00:00:00",
  "creditPeriodEndDate": "2052-05-01T00:00:00",
  "accountName": "Climate Lab bv", "validatorName": null
}], "totalEntities": 2}
```

`entityId` and `projectId` are **not always equal** — one of the two projects
carries `entityId 110200000000299` against `projectId 110200000000070`.
`projectId` is the key, and it is what the public URL uses; `entityId` is the
document id and exists to make paging deterministic.

### `issuances` / `holdings`

```json
{"entities": [{
  "entityId": 110200000000085, "projectId": 110200000000034,
  "publicProjectId": 110200000000034, "publicProjectName": "Kukumuty",
  "holdingQuantity": 2550.0, "vintage": "2022 - 2023",
  "unitType": "rPVC", "unitClass": "rPVC", "unitMeasurementName": "tCO2e",
  "serialNo": "PV-rPVC-MZ-110200000000034-01052022-30042023-1-2550-SPG",
  "issueDate": "2025-06-05T12:53:00",
  "periodStartDate": "2022-05-01T00:00:00", "periodEndDate": "2023-04-30T00:00:00",
  "countryName": "Mozambique", "stateName": "Issued"
}], "totalEntities": 27}
```

`vintage` is a range (`"2022 - 2023"`), not a year — do not partition on it
expecting Verra's single-year values.

## Vocabulary

`projectType` → `Tipo Macro de Projeto`, untranslated:

| Type | Projects |
|---|---:|
| Afforestation / Reforestation | 2 |

**One value deep, and it will grow.** `config/derivation/project_type.yaml`
and `durability.yaml` match it with `equals`, which cannot reach a Verra or
Cercarbono row. An unrecognised future type leaves the cell blank, which is the
correct failure — there is no catch-all.

`status` (→ `Status`): `Active` on both projects today.

## Fields Plan Vivo does not publish

Measured over the full 2-project index. These stay blank — nothing is inferred.

| Column | Fill | Note |
|---|---|---|
| Metodologia | 0 / 2 | `methodologies` is null. |
| Yearly Ex Ante | 0 / 2 | `avgAnnualVolVcu` is null. Never back-computed from issuances. |
| Total Ex Ante | 0 / 2 | Same. See the fPVC note above — forward credits are issued units here, not an estimate. |
| Additional Certification | 0 / 2 | Null on both. |
| Total Credits Retired / Sold | 0 / 2 | **The ledger is genuinely empty.** Blank, not zero. |
| Total Credits Cancelled | 0 / 2 | Same. |
| Continent | 2 / 2 | Derived from the country name; no region and no ISO code is published. |
| Bioma | 2 / 2 | From country-name rules — the continental bands read `region_name`, which only Verra publishes. |

## Politeness

Same rules as every other registry: ~1 req/s, exponential backoff, on-disk
response cache. A full sync is 7 requests. Do not raise concurrency.

## Adding the next Platts registry

The platform's own bundle lists them: `VERRA`, `UKLR`, `RAAS`, `PVCL`, `OxCP`,
`KRR`, `GCC`, `BCCR`. For any of them:

1. `verra standards -r <name>` — or the raw
   `{cmsResources}/public/standardsByRegistry/<CODE>` — for the real
   `standardId`, the acronym and the public ledger set.
2. Add a settings block and a `PlattsAPI` subclass with those values, its site
   and its public routes.
3. Check which fields the registry actually populates before reusing Verra's
   column map. Plan Vivo needed two changed and one dropped.

**`BCCR` is the "BC Carbon Registry" — British Columbia, not BioCarbon.** The
plan assumed otherwise; the standards lookup settled it
(`{"id":"140000000000001","name":"BC Carbon Registry","metaData":"BC"}`).
BioCarbon still needs its own adapter in Phase 5a.
