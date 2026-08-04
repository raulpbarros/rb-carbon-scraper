# Puro.earth — page contract

Measured against the live site on **2026-08-05**. Everything here was observed,
not inferred from documentation.

`registry.puro.earth` is a **Next.js App Router application**, and it is the
first target in this project with **no API to call at all**. The browser never
issues an XHR for registry data: the server renders each route and streams its
React Server Components payload inside the HTML, where the project and
transaction lists sit as ordinary JSON. There is no key, no `Origin` check, no
Cloudflare, no paging and no filtering.

**Three requests carry the entire registry.** A full sync is ~121 because one
detail page per project is read as well — see below for what only that page
publishes.

## Getting at the payload

Each route's HTML carries the stream split across `<script>` tags:

```html
<script>self.__next_f.push([0])</script>
<script>self.__next_f.push([1,"17:[\"$\",\"$L1f\",null,{\"data\":[{\"projectId\":\"181856\",…"])</script>
<script>self.__next_f.push([1,"…\"methodology\":{\"code\":\"C03000000\"}}]}]\n"])</script>
```

Concatenating every push's string argument rebuilds one text stream; the list a
route hands its table is the first `{"data":[…]}` object in it.

**The pushes must be joined before anything is decoded.** The 5.2 MB retirement
page splits its stream across dozens of them and the split lands mid-object, so
a reader that decodes each push in turn finds truncated JSON and gives up.

**There is no JSON route.** Three ways of asking for the flight payload on its
own were tried and all three returned the same prerendered HTML, byte for byte:

| asked | got |
|---|---|
| `RSC: 1` request header | `200 text/html`, the full page |
| `?_rsc=1` query parameter | `200 text/html`, the full page |
| `RSC: 1` + a full `Next-Router-State-Tree` | `200 text/html`, the full page |

The client bundles were checked too: no API host appears in any of the 15
chunks the site loads. The data is fetched server-side and shipped in the page.

## Endpoints

```
GET https://registry.puro.earth/projects            the project index
GET https://registry.puro.earth/issuances           the issuance ledger
GET https://registry.puro.earth/retirements         the retirement ledger
GET https://registry.puro.earth/projects/<code>     one project
```

`/issuances` and `/retirements` are two tabs of one view, and each returns only
its own transaction type — 583 records all reading `"type": "Issuance"`, 1,519
all reading `"type": "Retirement"`. There is no third tab.

## Volumes (2026-08-05)

| route | transactions | bundles | units |
|---|---|---|---|
| `/projects` | 118 | — | — |
| `/issuances` | 583 | 583 | 1,819,251 |
| `/retirements` | 1,519 | **2,099** | 1,041,121 |

117 of the 118 projects have at least one issuance. The other, `861867`, is
certified with no credits and no crediting period.

## Constraints — all measured

### A transaction is not a credit record

Every transaction carries a `bundles` list, and a retirement routinely draws
from several production facilities at once: **1,519 retirements are 2,099
bundles**. The bundle is what names a facility, a vintage and a credit class,
so a row per transaction would file every multi-facility retirement against
whichever facility happened to be first and lose the rest.

Issuances are 1:1 today — 583 transactions, 583 bundles — and that is a fact
about the data, not a rule. Both ledgers are read the same way.

`transaction.volume` equals the sum of its bundles' volumes on all 2,102
transactions.

### The registry publishes no row count, anywhere

Not in the payload, not in a response header, not on the home page, not on the
site. `len(data)` is the only number available for a list route, so reconciling
against it proves nothing. Three checks that *can* fail are used instead:

1. every bundle's `productionFacilityCode` must be a project in the index —
   **0 orphans across all 2,682 bundles**, unlike Cercarbono and the legacy
   Markit view, which both have real ledger rows for projects their own
   project list omits;
2. every transaction's `volume` must equal its bundles';
3. every project's bundles must sum to the total its own detail page states.

`puro.earth` (the marketing site, not the registry) states "Removed tonnes of
CO2: 1,795,705" against the ledger's 1,819,251. It lags the registry by about a
month and is **not** used for reconciliation.

### `countryCode` is `"NA"` for Namibia

`583695` is in Namibia and its ISO code is the two-letter string `NA`. Three of
the adapters in this project carry a `NOT_STATED` table listing `na`, and
reusing one here would delete a real country code and take the project's
Continent with it. Puro states absence as JSON `null` throughout and uses no
placeholder text at all, so its table is deliberately empty.

### Withdrawal is a label, and no quantity is published

20 issuance transactions carry an extra label:

| label | transactions | units in those transactions |
|---|---|---|
| `PARTIALLY_WITHDRAWN` | 14 | 54,843 |
| `FULLY_WITHDRAWN` | 6 | 10,229 |

`withdrawalDetails` exists in the schema and is `null` on all 2,102
transactions. **No withdrawn amount is published for either label** — and for
the partial ones no amount could be inferred anyway.

The registry's own `Issued credits` counts them in full. That was checked
rather than assumed: `517437` has a `FULLY_WITHDRAWN` issuance of 3,272 units
and its page states 9,694 issued, which is the sum of all four of its bundles
including that one. So a withdrawal is a flag on units that stay issued, not a
cancellation, and there is no cancellation ledger to scrape.

### The detail page is the only source of three things

One request per project, 118 of them, ~900 KB each:

* **the country name.** The list route publishes `countryCode` and no name.
  The detail page renders it twice, in the header and under "Location", each
  time as `[<flag>, " ", <name>]`. The flag is the one the project's own ISO
  code builds, so the pair can be checked rather than trusted.
* **`Issued credits` and `Retired credits`** — the only counts the registry
  states anywhere, in `{"label": "Issued credits", "value": 1126}` blocks.
* nothing else. No state, no city, no estimate, no status beyond a
  "Certified project" pill that reads the same on every page sampled.

All 118 detail pages were fetched and compared against the transaction feeds:
**118 of 118 agree exactly, on both ledgers.** 1,819,251 issued and 1,041,121
retired, summed either way.

### Beneficiary embargo — and this registry honours it

A retirement states a `beneficiaryName` (1,468 of 1,519), a `beneficiaryType`
and a `beneficiaryHiddenUntil` (108 of 1,519, all future dates). The 51 rows
with no name are exactly the ones whose embargo has not expired: **the name is
simply absent from the API**, not returned with a flag.

That is the opposite of BioCarbon, which marks 7,033 retirements `private` and
returns the name regardless. Nothing is invented here; a later sync picks the
name up when the registry publishes it.

`accountHolderName` and `beneficiaryName` agree on only 88 of 1,519 rows, so
the account holder is usually an intermediary — the same reading that made
BioCarbon's `final_user` the beneficiary rather than its `to_name`.

## Record shapes

### `/projects`

```json
{
  "projectId": "227253",
  "code": "227253",
  "gsrn": "643002406801000848",
  "name": "Alcom-01-NuevaEcijaPhilippines",
  "countryCode": "PH",
  "supplierName": "Alcom Carbon Markets Pte Ltd",
  "supplierListingUrl": "https://retired.puro.earth/…",
  "methodologyCode": "C03000000",
  "creditingPeriodStart": "2022-06-10",
  "creditingPeriodEnd": "2027-06-09",
  "sdgs": [{"goal": 13, "name": "Climate action"}],
  "generalRules": {"url": "…", "version": "Puro Standard General Rules Version 3.1"},
  "latitude": null,
  "longitude": null,
  "methodology": {"code": "C03000000", "name": "Biochar, 2022", "edition": "Edition 2022 V3", "url": "…"}
}
```

Fill rates over all 118:

| field | filled |
|---|---|
| `projectId`, `code`, `name`, `countryCode`, `supplierName`, `methodologyCode`, `methodology`, `generalRules` | 118 (100%) |
| `creditingPeriodStart` / `End` | 117 (99%) |
| `sdgs` | 111 (94%) |
| `gsrn` | 79 (67%) |
| `supplierListingUrl` | 61 (52%) |
| `latitude` / `longitude` | 31 (26%) |

`projectId` and `code` are the same value on all 118, the code is unique across
them, and it is what the public URL, the bundles' `productionFacilityCode` and
the certificate serials all key on. Unlike SocialCarbon's `SOCIALCARBON-N`,
**this published reference really is a primary key** — checked, not assumed.

### `/issuances` and `/retirements`

```json
{
  "id": "cccf79ef-…",
  "type": "Issuance",
  "accountHolderName": "Exomad SRL",
  "completedOn": "2026-07-31T11:41:32.098Z",
  "volume": 12678,
  "issuanceDetails": {"auditBody": "Energy Link Services Pty Ltd", "issuanceDate": "2026-07-31T…"},
  "retirementDetails": null,
  "withdrawalDetails": null,
  "transactionAdditionalLabels": [{"type": "CORC100+", "order": 0}],
  "bundles": [
    {
      "id": "c42e7f8d-…",
      "certificates": "PURO_PR_CORC100+_BO_432524_2026_cccf79ef-…_1-12678",
      "volume": 12678,
      "methodologyCode": "C03000000",
      "methodologyName": "Biochar, 2022",
      "productionFacilityCode": "432524",
      "productionFacilityName": "Exomad Green, Concepción",
      "vintage": 2026,
      "productionStartDate": "2026-06-12",
      "productionEndDate": "2026-07-13",
      "creditType": "CORC100+",
      "durability": 100,
      "issuanceDate": "2026-07-31"
    }
  ]
}
```

`retirementDetails`, on a retirement:

```json
{
  "usageType": "SPECIFIC_ACTIVITY_LIKE_FLIGHTS",
  "beneficiaryName": "Tampereen kaupunki",
  "beneficiaryType": "END_CONSUMER",
  "retirementPurpose": "Tampereen kaupungin työntekijöiden…",
  "publicStatementUrl": "https://retirements.puro.earth/retirement-statement/…",
  "beneficiaryLocation": "Finland",
  "countryOfConsumption": "FI",
  "beneficiaryHiddenUntil": null,
  "consumptionPeriodStartDate": "2025-01-01T00:00:00.000Z",
  "consumptionPeriodEndDate": "2025-12-31T00:00:00.000Z"
}
```

Bundle certificate serials are unique: 583 of 583 issuance bundles and 2,099 of
2,099 retirement bundles.

## Vocabulary

### Methodologies — the only classification published

There is no sector field. The methodology name is the whole vocabulary, and it
reaches both Tipo Macro and Metodologia.

| code | name | projects |
|---|---|---|
| `C03000000` | Biochar, 2022 | 80 |
| `C01000000` | Wooden Building Elements | 14 |
| `C06000000` | Terrestrial Storage of Biomass | 8 |
| `C07000000` | Enhanced Rock Weathering, 2022 | 6 |
| `C09000000` | Carbonated Materials | 5 |
| `C05000000` | Geologically stored carbon | 3 |
| `C05202402` | Geologically stored carbon, 2024 | 1 |
| `C04000000` | Soil Amendment | 1 |

The edition year is part of some names and not others, and one methodology
appears under two names. The derivation rules therefore match on a stem, never
on equality.

### Credit classes, and a published durability

| `creditType` | issuance bundles | retirement bundles | `durability` |
|---|---|---|---|
| `CORC100+` | 373 | 1,125 | 100 |
| `CORC` | 161 | 729 | *none* |
| `CORC1000+` | 47 | 198 | 1000 |
| `CORC20+` | 2 | 32 | 20 |
| `CORC_100` | — | 15 | — |

**Puro is the only registry here that publishes a durability**, as a number of
years on the bundle. Seven of its eight methodologies therefore have a
Durabilidade band that is *checked* rather than inferred. The exception is
Wooden Building Elements: its credits are the bare `CORC` class, which carries
no durability at all.

`CORC_100` appears on 15 retirement bundles and nowhere else — an older
spelling of `CORC100+`, carried through as published.

### Retirement usage types

`GENERIC_COMPENSATION` (995), `OTHER` (240),
`BUNDLED_WITH_PRODUCT_OR_SERVICE` (156), `SPECIFIC_ACTIVITY_LIKE_FLIGHTS` (99),
`DISCLOSURE` (22), `SUPPORT` (7). `beneficiaryType` is `END_CONSUMER` (1,387)
or `SUPPLIER` (132).

## Fields Puro does not publish

* **state or city.** Nothing sub-national exists on the list or the detail
  page. A latitude/longitude pair is published for 31 of 118.
* **any ex-ante estimate**, yearly or total. Puro certifies removals that have
  already happened.
* **a cancellation quantity.** See the withdrawal labels above.
* **an additional certification.** The `sdgs` list is a Sustainable
  Development Goal claim, not a co-certification — the same call Cercarbono's
  `elegible` list gets.
* **a standard name.** Every project names a *General Rules* version
  (13 versions across 118 projects) and none names the standard, which the
  adapter asserts as "Puro Standard".
* **a project status.** Every detail page sampled renders the same
  "Certified project" pill.

## Other hosts

`retired.puro.earth` and `retirements.puro.earth` are linked from the registry
— supplier listings and retirement statements. They are document hosts, not
second feeds, and nothing is scraped from either.

## Politeness

Three list requests and one detail request per project: ~121 requests at the
default ~1 req/s. The pages are large — 5.2 MB for the retirement feed, ~900 KB
per detail page, about 120 MB for a cold sync — so the on-disk response cache
matters more here than anywhere else. No `robots.txt` is served; the data is
public by design and the site is read exactly as a browser reads it.
