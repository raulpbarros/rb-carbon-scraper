# Field mapping

Where every column in `out/carbon-projects_vN.xlsx` comes from. **This is the
document to review with the business** — it separates what the registries
actually publish from what this tool infers.

The sheet holds **every registry stacked in one tab**: Verra VCS, Gold Standard,
Cercarbono and Plan Vivo. The `Registry` and `Standard` columns tell them apart.
The sections below give the source for each, because they are not the same.

Four confidence levels:

- 🟢 **Direct** — copied from a registry field. Trust it.
- 🔵 **Computed** — arithmetic on registry fields. Reliable, but check the method.
- 🟡 **Classified** — a rule in `config/derivation/*.yaml` guessed it. **Review before trusting.**
- ⚪ **Not published** — the registry has no such field. The cell stays blank.

---

## Verra VCS — the 22 requested columns

| # | Column | Level | Source | Notes |
|---|---|---|---|---|
| 1 | Project ID | 🟢 | `vcsProjectId` | The number in the registry URL. |
| 2 | Project Name | 🟢 | `projectName` | Blank on a few withdrawn projects. |
| 3 | Standard | 🟢 | `standardName` | Always "Verified Carbon Standard" while scope is VCS. |
| 4 | Tipo Macro de Projeto | 🟢 | `sectoralScope` | **Confirmed by the business as the sectoral scope.** Not derived. |
| 5 | Tipo Micro de Projeto | 🟡 | `config/derivation/project_type.yaml` | AFOLU sub-types (ARR/REDD/IFM/ALM/WRC/ACoGS) come from Verra's own `afoluNames`, so those are solid. Everything else is a scope/methodology guess. |
| 6 | Metodologia | 🟢 | `methodologies` | e.g. `VM0042 (Version NA)`. |
| 7 | Additional Certification | 🟢 | Units ledger | **Not on the project record** — Verra leaves that null. Collected from unit rows (e.g. CORSIA). Only projects with issued units can have one. |
| 8 | Durabilidade | 🟡 | `config/derivation/durability.yaml` | Distinguishes reversible biological storage from emission reductions. Bands need business sign-off. |
| 9 | Bioma | 🟡 | `config/derivation/biome.yaml` | **Weakest column.** A state usually spans several biomes; the rule picks the dominant one. Only filled for AFOLU projects. |
| 10 | Duração | 🔵 | `creditPeriodEndDate − creditPeriodStartDate` | In whole years. Falls back to Verra's `creditPeriod` field. |
| 11 | Data de Início | 🟢 | `creditPeriodStartDate`, else `projectStartDate` | |
| 12 | Data de Término | 🟢 | `creditPeriodEndDate`, else `projectEndDate` | |
| 13 | Continent | 🟢 | `regionName` | **A real Verra field** — no derivation needed. |
| 14 | País | 🟢 | `countryName` | |
| 15 | Estado | 🟢 | `stateProvince` | ~87% filled. |
| 16 | Cidade | 🟢 | `city` | Free text; sometimes lists several municipalities. |
| 17 | Yearly Ex Ante | 🟢 | `avgAnnualVolVcu` | Verra's estimated average annual VCUs. 100% filled. |
| 18 | Total Ex Ante | 🔵 | `avgAnnualVolVcu × Duração` | Verra's own `exanteQuantity` is null throughout the public index, so the total is built from the yearly figure. |
| 19 | Total Credits Issued | 🟢 | Σ `issuances.holdingQuantity` | From the Units ledger. `unitsIssued` on the project record is always null. Ledger captured in full (19,521 rows). |
| 20 | Total Credits Sold | 🟢 | exact API SUM of retirements | **Business decision: retired VCUs are treated as sold.** See below. |
| 21 | Total Credits Retired | 🟢 | exact API SUM of retirements | Fetched per project by `verra totals`, **not** summed from rows — see the data-quality note below. |
| 22 | Total Credits Cancelled | 🟢 | Σ `cancellations.holdingQuantity` | Ledger captured in full (1,778 rows). |

### Extra columns (not requested, added because they are useful)

| Column | Source | Why |
|---|---|---|
| Registry | the adapter that produced the row | "Verra VCS" or "Gold Standard". The two registries share one sheet. |
| Status | `status` | The scrape covers every status (Registered, Under validation, Withdrawn…). Without this you cannot filter the sheet. |
| Project URL | built from the project id | One click to verify any row against its registry. |

---

## Gold Standard — the same 22 columns, different sources

Measured over the full 4,141-project index on 2026-07-28. Percentages are
actual fill rates, not estimates.

| # | Column | Level | Source | Fill | Notes |
|---|---|---|---|---:|---|
| 1 | Project ID | 🟢 | `GS{sustaincert_id}` | 100% | **Not** parsed from the project name: a project named `GS23711-…` can itself be `GS23718`, because the name carries its parent programme's number. |
| 2 | Project Name | 🟢 | `name` | 100% | |
| 3 | Standard | 🟢 | `gsf_standards_version` | 100% | e.g. "Gold Standard for the Global Goals". |
| 4 | Tipo Macro de Projeto | 🟢 | `type` | 100% | **Carried through untranslated.** See the vocabulary warning below. |
| 5 | Tipo Micro de Projeto | 🟡 | `config/derivation/project_type.yaml` | 94% | Rules key on the 20-value `type` list. The 6% blank are `type: Other` (350 projects), left blank on purpose. |
| 6 | Metodologia | 🟢 | `methodology` | 59% | Published where it exists; null on many newly listed projects. |
| 7 | Additional Certification | ⚪ | — | 0% | **No equivalent field.** The credit `labels` value is a product class (`EMISSION_REDUCTION`), not a co-certification, and is deliberately not used here. |
| 8 | Durabilidade | 🟡 | `config/derivation/durability.yaml` | 91% | `A/R` is the only Gold Standard type that stores carbon; the rest are emission reductions. |
| 9 | Bioma | 🟡 | `config/derivation/biome.yaml` | 1% | Only `A/R` projects qualify (171), and only those in named countries match — Gold Standard publishes no region, so the continent-level fallbacks cannot fire. |
| 10 | Duração | 🔵 | `crediting_period_end_date − crediting_period_start_date` | 100% | In whole years. |
| 11 | Data de Início | 🟢 | `crediting_period_start_date` | 100% | |
| 12 | Data de Término | 🟢 | `crediting_period_end_date` | 100% | |
| 13 | Continent | 🔵 | `config/derivation/continent.yaml`, from `country_code` | 99% | Gold Standard publishes no region. The 21 gaps are `XZ` / "International" multi-country projects, which have no single continent. |
| 14 | País | 🟢 | `country` | 100% | |
| 15 | Estado | ⚪ | — | 0% | `state` exists in the schema but is null across all 4,141 projects. |
| 16 | Cidade | ⚪ | — | 0% | **No city field exists.** |
| 17 | Yearly Ex Ante | 🟢 | `estimated_annual_credits` | 100% | |
| 18 | Total Ex Ante | 🔵 | `estimated_annual_credits × Duração` | 99% | |
| 19 | Total Credits Issued | 🟢 | Σ all credit blocks | | Every block was issued — see the status note below. |
| 20 | Total Credits Sold | 🟢 | Σ blocks with `status = RETIRED` | | Same business rule as Verra: retired = sold. |
| 21 | Total Credits Retired | 🟢 | Σ blocks with `status = RETIRED` | | |
| 22 | Total Credits Cancelled | 🟢 | Σ blocks with `status = CANCELLED` | | No `CANCELLED` blocks observed in sampling; the bucket exists in case they appear. |

⚪ = the registry does not publish it. **The cell stays blank — nothing is
inferred, and no other source is consulted.**

---

## Cercarbono — the same 22 columns, different sources

Full contract in `docs/api-contract-cercarbono.md`. Fill rates measured over
the complete 231-project CO2 index on 2026-07-28.

Only the **CO2 standard** (`cercarbono-co2`) is scraped. EcoRegistry also hosts
`cercarbono-biodiversity` and `cercarbono-circular-economy`, whose credits are
not tCO2e; mixing them would put non-comparable units in the credit columns.
User's decision, 2026-07-28.

| # | Column | Level | Source | Fill | Notes |
|---|---|---|---|---:|---|
| 1 | Project ID | 🟢 | `code` | 100% | `CDC-271`. **Not** the numeric `id` (274) — they do not agree, and the id is an internal key Cercarbono's own search does not accept. |
| 2 | Project Name | 🟢 | `name` | 100% | |
| 3 | Standard | 🟢 | `evaluation_criteria` | 100% | The protocol version, e.g. "PROTOCOL CVCC V2.1". Falls back to the registry label where the registry says "Not defined". |
| 4 | Tipo Macro de Projeto | 🟢 | `sectors[].description` | 94% | **Carried through untranslated**, like every registry. De-duplicated: Cercarbono repeats a sector once per verification. |
| 5 | Tipo Micro de Projeto | 🟡 | `config/derivation/project_type.yaml` | 94% | Cercarbono states no AFOLU sub-type code, so the rules key on the methodology string instead (`AR-ACM0003`, `M/LU-REDD+`, …). |
| 6 | Metodologia | 🟢 | `methodology[].description` | 94% | Full CDM/CCB methodology names. |
| 7 | Additional Certification | ⚪ | — | 0% | No equivalent field. The serials' `elegible` list is market eligibility ("Colombian Carbon Tax"), not a co-certification. |
| 8 | Durabilidade | 🟡 | `config/derivation/durability.yaml` | 94% | Reforestation vs REDD+ read off the methodology, since no sub-type code is published. |
| 9 | Bioma | 🟡 | `config/derivation/biome.yaml` | 39% | Only land-use projects qualify. Colombia (140 of 231 projects) is now split by department — see below. |
| 10 | Duração | 🔵 | `periodEnd − periodInit` | 95% | |
| 11 | Data de Início | 🟢 | `periodInit` | 95% | **Only on the per-project detail endpoint** — the reason a sync makes one request per project. |
| 12 | Data de Término | 🟢 | `periodEnd` | 95% | Same. 11 projects have no crediting period yet. |
| 13 | Continent | 🔵 | `config/derivation/continent.yaml`, from `country_name` | 100% | **Cercarbono publishes no ISO country code anywhere**, so a second table in that file matches on the country name. It sits below the code rules, so a registry that does state a code is still read from the code. |
| 14 | País | 🟢 | `locations[].country` | 100% | Every project is single-country. |
| 15 | Estado | 🟢 | `locations[].region` | 46% | The rest carry EcoRegistry's `"Worldwide"` placeholder, which is a blank, not a place. |
| 16 | Cidade | 🟢 | `locations[].city` | 46% | Same. Multi-site projects list each distinct municipality. |
| 17 | Yearly Ex Ante | ⚪ | — | 0% | Not published. **Never back-computed from issuances**, which are actuals, not an estimate. |
| 18 | Total Ex Ante | ⚪ | — | 0% | Follows from the above. |
| 19 | Total Credits Issued | 🟢 | `certificatedVerification[].total` | 100% | The registry's own figure, stored in `credit_totals`. **Not** the sum of serial rows — see below. |
| 20 | Total Credits Sold | 🟢 | Σ `retirements` | 49% | Same business rule as the other registries: retired = sold. |
| 21 | Total Credits Retired | 🟢 | Σ `retirements` | 49% | The ledger arrives complete in one response and reconciles exactly. |
| 22 | Total Credits Cancelled | ⚪ | — | 0% | **No cancellation ledger is published.** Blank, not zero. |

### Why Cercarbono's issued total is not the sum of its serial rows

`analytics/projects` embeds every issuance serial, and summing them looks like
the obvious route. It is wrong on three of the 231 projects, and the run
finishes cleanly either way:

| Project | Serial rows sum | Registry's own figure |
|---|---:|---:|
| CDC-196 | 161,297 | **120,448** |
| CDC-106 | — (absent from the feed) | **79,450** |
| CDC-107 | — (absent from the feed) | **170,550** |

CDC-196 publishes its 2022 and 2023 issuances twice, under two serial
revisions (`…_R6_…` and `…_R7_…`) — a 34% overstatement. CDC-106 and CDC-107
were converted in from BioCarbon and are missing from the bulk feed entirely
while still appearing in the retirement ledger, so summing rows showed them
retiring credits they had never issued.

`certificatedVerification` on the per-project detail is the registry's own
issued figure, and a third endpoint (`emitcertifications`, available +
cancelled) agrees with it in every case. It is stored in `credit_totals`, which
by design outranks summed rows. The per-serial rows are still kept — they carry
the vintage, issuance date and buffer flag the total does not.

Buffer credits are **included**: the registry's own figure counts them
(CDC-1: 81,169 includes 12,175 of buffer), so excluding them would disagree
with Cercarbono. The flag lives in `credit_events.unit_type`, so splitting
them out later needs no re-scrape.

### Bioma: Colombia is now split by department

140 of Cercarbono's 231 projects are Colombian. The previous country-level rule
placed every Colombian project in the Amazon basin, which is wrong for most of
the country — the Andes and the Caribbean coast are not the Amazon. Colombian
departments now map to Andes / Amazonía / Orinoquía / Caribe / Pacífico, and a
project whose department was never entered reads "Colômbia (bioma não
determinado)" rather than being placed anywhere.

---

## Plan Vivo — the same 22 columns, different sources

Full contract in `docs/api-contract-planvivo.md`. Fill rates measured over the
complete 2-project index on 2026-07-28.

**This registry is very small and very new.** Its first project was activated
on 2025-04-30 and it holds two projects, 213,145 issued units and no
retirements at all. Percentages over two rows are not a quality signal; the
column that matters is whether the source is right.

Plan Vivo runs on the **same S&P backend as Verra** and returns the identical
record shape, so most columns come from the same fields. The differences are
what Plan Vivo leaves null.

| # | Column | Level | Source | Fill | Notes |
|---|---|---|---|---:|---|
| 1 | Project ID | 🟢 | `projectId` | 100% | Plan Vivo publishes **no human reference** — `vcsProjectId` is Verra's field and is null here. The numeric id is what the registry's own public URL uses, so the row stays checkable. |
| 2 | Project Name | 🟢 | `projectName` | 100% | |
| 3 | Standard | 🟢 | `standardName` | 100% | "PV Climate". |
| 4 | Tipo Macro de Projeto | 🟢 | **`projectType`** | 100% | Not `sectoralScope`, which is null throughout. Carried through untranslated: "Afforestation / Reforestation". |
| 5 | Tipo Micro de Projeto | 🟡 | `config/derivation/project_type.yaml` | 100% | Matched with `equals` against Plan Vivo's own type vocabulary, which is one value deep today. |
| 6 | Metodologia | ⚪ | — | 0% | `methodologies` is null on both projects. |
| 7 | Additional Certification | ⚪ | — | 0% | Null on both. |
| 8 | Durabilidade | 🟡 | `config/derivation/durability.yaml` | 100% | A/R builds a reversible biological store; same band as Verra's ARR and Gold Standard's A/R. |
| 9 | Bioma | 🟡 | `config/derivation/biome.yaml` | 100% | From country-name rules — see below. |
| 10 | Duração | 🔵 | `creditPeriodEndDate − creditPeriodStartDate` | 100% | 30 and 31 years. |
| 11 | Data de Início | 🟢 | `creditPeriodStartDate` | 100% | |
| 12 | Data de Término | 🟢 | `creditPeriodEndDate` | 100% | |
| 13 | Continent | 🔵 | `config/derivation/continent.yaml`, from `country_name` | 100% | `regionName` is null here, so Plan Vivo takes the same country-name path as Cercarbono. |
| 14 | País | 🟢 | `countryName` | 100% | Mozambique, Guatemala. |
| 15 | Estado | 🟢 | `stateProvince` | 100% | |
| 16 | Cidade | 🟢 | `city` | 100% | |
| 17 | Yearly Ex Ante | ⚪ | — | 0% | `avgAnnualVolVcu` is null. Never back-computed from issuances. |
| 18 | Total Ex Ante | ⚪ | — | 0% | `exanteQuantity` is null. See the fPVC note below. |
| 19 | Total Credits Issued | 🟢 | Σ `issuances` | 100% | 213,145 — matches "Issued Units" on Plan Vivo's own public page exactly. |
| 20 | Total Credits Sold | ⚪ | Σ `retirements` | 0% | **The retirement ledger is genuinely empty.** Blank, not zero. |
| 21 | Total Credits Retired | ⚪ | Σ `retirements` | 0% | Same. |
| 22 | Total Credits Cancelled | ⚪ | Σ `cancellations` | 0% | Same. |

### Forward credits (fPVC) are counted as issued — a question for the business

Plan Vivo issues two kinds of credit as real registry units, and the platform's
own unit-type lookup labels them:

| Class | Units | `isVerified` | |
|---|---:|---|---|
| fPVC | 103,246 | **false** | forward credits, against future sequestration |
| rPVC | 50,220 | true | |
| Future Risk Buffer | 41,286 | — | |
| Achievement Reserve | 18,393 | — | |

`Total Credits Issued` counts all four: **213,145**, which is the figure on Plan
Vivo's own public page. Nearly half of it is fPVC.

Verra keeps this idea in a separate `Total Ex Ante` *estimate* column. Plan Vivo
does not estimate — it issues. Reporting a subset would disagree with the
registry's published total, so the registry's own number stands and this note
exists instead. **If the business wants forward credits reported separately,
the class is already stored in `credit_events.unit_type` and it is a
`config/credits.yaml` change, not a re-scrape.**

### Bioma: two country bands added

The continent-level biome bands read `region_name`, which **only Verra
publishes**. Plan Vivo and Cercarbono rows never reach them, so their projects
fell through to blank. Two country-name bands were added for the regions
actually present: the **Miombo** woodland belt (Mozambique, Zambia, Zimbabwe,
Malawi, Tanzania, Angola) and **Mesoamerica** (Guatemala, Belize, Honduras,
Nicaragua, Costa Rica, Panama, El Salvador).

Because they match on country rather than region, they also refine rows in the
other registries. Measured over the full database, 70 African projects moved
from the coarse "Savana / Floresta Africana" to Miombo, and 49 Central American
projects moved from "Floresta Tropical Latino-Americana" — 12 Panamanian
projects among them out of "Floresta Temperada Norte-Americana", which they
plainly were not. No project lost a biome.

---

### `Tipo Macro` is registry-dependent by design

Verra rows read "Energy industries (renewable/non-renewable sources)". Gold
Standard rows read "Energy Efficiency - Domestic". Cercarbono rows read "Land
use (AFOLU)". Plan Vivo rows read "Afforestation / Reforestation". These are
different taxonomies and the column shows each registry's own words, as
published — four registries, four vocabularies for the same idea.

This is the settled shape of the column (decision of 2026-07-28), not a gap
awaiting cleanup: each registry states its macro type in its own vocabulary,
and a registry added later brings another. Nothing is translated or
rewritten. Read the column together with `Registry` — filtering it across
registries needs a mapping the business has not defined, and `Tipo Micro de
Projeto` (derived, one shared vocabulary) is the column to filter on until
then.

### Why Gold Standard credit totals are safe to sum locally

Verra's retirements need a special exact-SUM pass (below). Gold Standard does
not: its whole 182,989-block credit stream pages cleanly in id order, with no
result window and no partitioning, and every run reconciles against the
registry's own `X-Total-Count`.

One thing to be aware of when reading the numbers: **a credit block's `status`
is its current state, not an event.** A block that was issued and later
retired reads `RETIRED`. So "issued" is the sum of every block regardless of
status. A project showing 350 issued and 250 retired has 100 blocks still
reading `ISSUED` — not 350 separate issuance events.

---

## Data-quality note: why retirement totals come from a different route

Three of the four Units ledgers download completely and reconcile exactly
against the registry's own counts:

| Ledger | Registry says | We captured | |
|---|---:|---:|---|
| issuances | 19,518 | 19,521 | complete |
| holdings | 21,536 | 21,538 | complete |
| cancellations | 1,778 | 1,778 | complete |
| **retirements** | **305,144** | **266,622** | **87% — see below** |

(The small overshoots are new records added between the count and the fetch.)

Retirements cannot be paged completely. Elasticsearch caps paging at 10,000
results, so the ledger has to be split into filtered partitions — and **this
API silently returns the entire index for any filter it does not understand**,
with no error. Those bogus partitions overlap and then get truncated, which
produces duplicates and gaps simultaneously.

So the retirement **totals** are not summed from the downloaded rows. `verra
totals` asks the API for an exact `SUM` per project, one request per project.
That path uses only `Number/equals` on `projectId` — a filter proven to narrow —
and is immune to both the paging cap and the ignored-filter trap. The result is
cross-checked against the registry-wide total (920,282,316 VCUs retired).

**Practical consequence:** the four credit columns in the spreadsheet are exact.
The retirement *rows* in `credit_events` are ~87% complete, which only matters
for the optional beneficiary-based Sold/Retired split described below. If the
business wants that split, the row coverage needs finishing first.

### Verified

The 2,185 per-project totals sum to **920,284,723** VCUs retired against the
registry's own all-projects figure of **920,282,487** — a 0.0002% difference,
caused by retirements posted during the ~70 minutes the run takes. Not a
reconciliation error.

Nine values re-queried live against the API after export, all matching exactly:

| Project | Retired | Issued |
|---|---:|---:|
| VCS934 (Mai Ndombe REDD+) | 23,329,499 | 36,854,622 |
| VCS1728 (Mytrah Wind) | 3,349,963 | 6,030,640 |
| VCS1052 | 56,196 | 612,915 |

### Why the credit columns are only ~41% filled

Only **2,185 of 5,245** projects have ever issued a credit. The rest are under
validation, under development, or withdrawn. An empty credit cell means the
project has no units — not that the scrape missed something. Filter on the
`Status` column to see only projects that can issue.

---

## The Sold / Retired duplication

`Total Credits Sold` and `Total Credits Retired` currently hold **the same
number**. That follows directly from the agreed rule that retired VCUs count as
sold. It is the intended behaviour, not a bug.

If the business wants them to differ, the natural split is already supported:
retirements that name a third-party beneficiary are arguably sales, while a
proponent retiring its own credits is not. Set this in `config/credits.yaml`:

```yaml
sold_equals_retired: false
```

then run `verra derive && verra export`. The beneficiary is stored on every
retirement row, so no re-scrape is needed.

---

## Fields Verra publishes but nobody asked for

Kept in `data/verra.db` in case they become useful: `proponents` (project
developer), `projectSize`, `area`, `creditPeriodTerm`, `validatorName`,
`sdContributions` (SDG claims), latitude/longitude, and the complete raw JSON
for every record in `raw_snapshots`.

Because the raw payloads are stored, **new columns can be added without
re-scraping**.

---

## How to correct a classification

1. Find the wrong value: `verra coverage`, or query the database:
   ```sql
   SELECT p.project_name, d.value, d.rule_name
   FROM project_derived d JOIN projects p USING (project_id)
   WHERE d.column_name = 'Bioma';
   ```
   `rule_name` names the exact rule that produced it.
2. Edit that rule in `config/derivation/`.
3. `verra derive && verra export` — seconds, no network.
