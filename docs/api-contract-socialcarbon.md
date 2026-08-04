# SocialCarbon (Bubble.io) — API contract

Measured against the live service on **2026-08-04**. Everything here was
observed, not inferred from documentation.

`registry.socialcarbon.org` is a **Bubble.io application**, and Bubble exposes
a generic **Data API** for every readable type. It is open: no key, no token,
no browser `User-Agent` required (verified with `-A ''` — HTTP 200), no
Cloudflare, no `Origin` check. That makes it the friendliest target in this
project, and by some distance the smallest: **the whole registry is four
requests**.

Run by Wilder Earth. PLAN.md 5d had recorded this registry as *blocked, serves
a parked CDN page*; it was unblocked on 2026-07-29 and confirmed in full here.

## Endpoints

```
GET https://registry.socialcarbon.org/api/1.1/meta
GET https://registry.socialcarbon.org/api/1.1/obj/<type>?limit=100&cursor=<n>
```

Every `obj` response is the same envelope:

```json
{"response": {"cursor": 0, "results": [...], "count": 19, "remaining": 0}}
```

`count + remaining` is **the registry's own total**, restated on every page.
That is what reconciliation reads — there is no header count and no separate
count endpoint.

`meta` lists the readable types under `get`, plus a large set of `post`
actions the adapter does not touch.

## Volumes (2026-08-04)

| type | rows | scraped | why |
|---|---|---|---|
| `project` | 19 | **yes** | the project index |
| `issuance` | 17 (189,794 units) | **yes** | the issued ledger |
| `retirement` | 81 (67,991 units) | **yes** | |
| `cancellations` | 2 (20,145 units) | **yes** | |
| `asset` | 22 (349,794 units) | **no** | mirrors `issuance` and carries VCS credits — see below |
| `transfer` | 4 | no | no project link and no quantity; a token movement, not a credit event |
| `transaction` | 1 | no | not a credit event |
| `assetlisting` | 7 | no | marketplace listings |
| `document` | 147 | no | file attachments |
| `vvbs` | 13 | no | the validator/verifier directory |
| `billing`, `accountmanager`, `organisationdetails`, `user` | — | **no, deliberately** | account and personal data. Nothing the spreadsheet asks for. Not read |

A full sync is **4 requests**, ~1 minute at the default ~1 req/s.

## Constraints — all measured

### `limit` clamps to 100 in silence

Asking `limit=200` of `document` (147 rows) returns **100** rows with
`remaining: 47`, at HTTP 200, with no marker of any kind. This is the third
registry in this project to ignore a page size — Gold Standard clamps
`projects` to 150, the legacy Markit view ignores `limit` entirely.

The pager therefore advances on **`remaining`**, never on "did I get as many
rows as I asked for". Every type fits in one page today; this is written for
the day one does not.

### Filters actually work — the first registry here where they do

Bubble validates `constraints`:

```
?constraints=[{"key":"Country","constraint_type":"equals","value":"Brazil"}]
    -> 9 rows, every one of them Brazil
?constraints=[{"key":"NotAField","constraint_type":"equals","value":"zzz"}]
    -> HTTP 404 {"status":"NOT_FOUND","message":"Field not found NotAField for type project"}
```

A bad filter **refuses loudly** instead of returning the whole index. That is
the opposite of Verra and Gold Standard, where an unrecognised filter is
ignored and the entire index comes back looking filtered.

**No filter is used anyway** — the registry fits in one page per type — but it
is worth knowing before anyone needs to partition it.

### `Project ID` is not unique, and is not a key

The registry publishes a human reference of the form `SOCIALCARBON-N`. It is
**not unique and not complete**:

- `SOCIALCARBON-19` is published by **two entirely different projects** —
  *Aeco Peatland Restoration Program Poland #1* (Poland, `SCM0010-M1`,
  aeco GmbH) and *Serra Bonita - Carbon Removal Project* (Brazil, `SCM0006`,
  Invest Conservation SA). Different records, different `_id`, created two
  months apart.
- `SOCIALCARBON-15` does not appear at all.

**19 records, 18 distinct references.**

This is the legacy Markit trap inverted. There, 35 rows were 30 projects and
rows sharing an id had to be *merged*; here, merging would fuse Poland and
Brazil into one row. Bubble's own `_id` is the only unique key, so:

- `project_id` = `hashed_id(REGISTRY, _id)` — an integer, because
  `credit_events.project_id` is one and Bubble's id is a string. Hashed rather
  than counted so the key survives the registry inserting a project.
- `external_id` = `Project ID` as published, duplicate and all.
- `extra.bubble_id` = the raw `_id`, since the hash cannot be read back.

The collision is the registry's. It is raised in `docs/field-mapping.md`, not
repaired here.

### `asset` is not a ledger — scraping it would double-count twice

`asset` holds 22 rows totalling 349,794 units, and looks like a holdings
ledger. It is not:

- **17 of its rows mirror the 17 issuances**, summing to the identical
  **189,794** units. Ingesting both would double every issued figure.
- **5 rows read `"Standard": "VCS"`** with no `Project` link and a `Deposit
  Request` instead of an `Issuance` — 160,000 units of *Verra* credits
  deposited into this platform's tokenisation layer. Those are another
  registry's credits seen from a second angle.

Same shape as the legacy-Markit SocialCarbon rows, which are also not ingested:
SocialCarbon appears there under `standardId=100000000000007` as an
*additional certification*, where its rows read "No Established Standard". The
Bubble registry is the current system and the one used.

### An issuance can be a request rather than credits

An issuance carries the registry's own `Approved` and `Issuance complete`
flags. All 17 rows have both set today, so the ledger sums to exactly what the
registry reports. A row with either unset would be a *request*, not units in
existence — so `iter_credit_totals` sums only approved, complete rows, and the
flag is stored on the event as `status` (`Issued` / `Pending`). Every row is
stored either way: it is published, and dropping it would make the ledger
disagree with the registry.

## Record shapes

Bubble returns human-readable field names, spaces and all, and spells the same
idea differently across types.

### `obj/project`

```json
{
  "_id": "1663921969278x388468748169510900",
  "Project ID": "SOCIALCARBON-1",
  "Project Name": "Spekboom Regeneration and Carbon Sequestration",
  "Standard": "SOCIALCARBON",
  "Project Status": "Listed",
  "Project Type": "Agriculture Forestry and Other Land Use",
  "Methodology": "SCM0004",
  "Country": "South Africa",
  "Total Project Area": 7311,
  "Crediting period start": "2023-01-31T22:00:00.000Z",
  "Crediting period end": "2033-01-31T22:00:00.000Z",
  "Start Date": "...", "End Date": "...",
  "Estimated Annual Emission Reductions": null,
  "Project Proponent(s)_TEXT": ["Spekboom Net Zero (Pty) Ltd"],
  "Project Proponent(s)": ["1660319514524x624894049101856400"],
  "Latitude": -33.05, "Longitude": 24.54,
  "Address": {"address": "XG3P+H8, South Africa", "lat": …, "lng": …},
  "validator": "TBC", "verifier": "TBD",
  "SDGs": [...], "Photos": [...], "Description": "…"
}
```

Fill rates over all 19 projects: everything above is 19/19 except
`Estimated Annual Emission Reductions` (18), `Address` (14), `SDGs` (13) and
`Photos` (8).

`validator` / `verifier` carry **`TBC` / `TBD`** before validation happens —
a placeholder, not the name of a body, and treated as blank.

`Description` contains mojibake in several records (`SNZâ€™s` for `SNZ's`) —
UTF-8 bytes stored as if Latin-1, upstream in the registry's own data. The
column is not used; noted so nobody "fixes" it downstream.

### `obj/issuance`

```json
{
  "_id": "…", "Project": "1693400358193x754855025125621800",
  "Quantity requested": 12913,
  "Asset type": "SCU - Removal",
  "Vintage": "2023 - 2023",
  "Serial Number Batch": "…",
  "Approved": true, "Issuance complete": true, "Payment received": true,
  "CORSIA eligible": false,
  "Monitoring period start": "…", "Monitoring period end": "…",
  "Verifier": "Earthood Services Private Limited",
  "Created Date": "2024-04-09T09:17:55.665Z"
}
```

### `obj/retirement` and `obj/cancellations`

```json
{
  "_id": "…", "Project": "…",
  "Quantity": 1, "Asset Type": "SCU - Removal", "Vintage": "2023 - 2023",
  "Serial Numbers": "SCU-SOCIALCARBON-7-Brazil-2020-14052025145100-1-10121",
  "Retiree": "Bluegreen Water Technologies Ltd", "Retiree_ID": "…",
  "Beneficiary": null, "Purpose": null, "certificate": null,
  "Notes": "Beneficiary: BlueGreen Water Technologies, Purpose: In acknowledgment of …",
  "txid": "0167709b…", "Created Date": "…"
}
```

Cancellations additionally carry `Canceller` and `Non-Permanence` (both of the
two rows are non-permanence reversals).

Note the naming drift across types: `Quantity requested` against `Quantity`,
`Asset type` against `Asset Type`, `Serial Number Batch` against
`Serial Numbers`. Same meanings, three spellings.

**Every one of the 100 ledger rows links to one of the 19 projects.** No
orphans, no foreign-standard rows, no missing project — unusual, and checked
rather than assumed.

### Beneficiary

`Beneficiary` and `Purpose` are filled on **20 of 81** retirements. The other
61 state the same thing as prose inside `Notes`
(`"Beneficiary: …, Purpose: …"`).

**Only the structured fields are read** (user's decision, 2026-08-04): a
wording change in that prose would stop matching in silence. The whole `Notes`
string is stored in `credit_events.reason`, which matters because
`credit_events` keeps **no raw payload** — only `projects` do — so that column
is the only copy, and it is what makes the decision reversible without a
re-scrape.

**`Retiree` is not a beneficiary.** It is filled on all 81 rows and names the
account that retired the units, not the third party they were retired for.
Using it as a fallback would make every retirement look like a third-party
sale the moment `sold_equals_retired` is flipped.

## Vocabulary

`Project Type` — carried through untranslated, as every registry's is:

| value | projects |
|---|---|
| `Agriculture Forestry and Other Land Use` | 17 |
| `AFOLU` | 1 |
| `Harmful Algae Bloom Treatment` | 1 |

The bare **`AFOLU`** matters: `config/derivation/biome.yaml`'s `applies_when`
gate carried `Land use \(AFOLU\)`, which requires the parenthesised form, so
that project would have had no biome and nothing in the log to say why. The
gate now uses `\bAFOLU\b`. Blast radius measured against the real database:
**zero existing rows change.**

`Project Status` — `Listed` (12), `Certified Project` (6),
`Certified Design` (1).

`Methodology` — `SCM0003` … `SCM0010-M1`, one project carrying two
(`SCM0003, SCM0009`).

Countries — 9, all published as names with **no ISO code anywhere**, so
Continent is derived from the name. One of them is
**`Congo, Democratic Republic of the`**, a *third* ISO inversion that
`continent.yaml` did not carry (it had `Democratic Republic of the Congo` and
`Congo, the Democratic Republic of the`). These are exact-match lists; one
missing comma is a missing continent.

## Fields SocialCarbon does not publish

Measured over the full 19-project index. Blank, never filled from elsewhere:

- **`Estado` / `Cidade`** — no state or city field. `Address` is a free-text
  string plus a lat/lng pair, filled on 14 of 19; reading a state out of
  `"XG3P+H8, South Africa"` would be inventing one. The coordinates go to
  `extra`.
- **`country_code`** — no ISO code anywhere.
- **`Total Ex Ante`** — not published. `Estimated Annual Emission Reductions`
  is the yearly figure, and `derive.py` builds the total from it as it does
  for every other registry.
- **`Additional Certification`** — no equivalent field. `CORSIA eligible` on
  an issuance is market eligibility, exactly like Cercarbono's `elegible`
  list, and is deliberately not used as a co-certification.
- **`region_name`** — only Verra publishes one.
- **`afolu_names`, `project_size`** — not published.

## The public project page

```
https://registry.socialcarbon.org/project_details/<_id>
```

Verified HTTP 200; `/project/<_id>` and `/projects` are 404. The route is keyed
on Bubble's record id, **not** on `SOCIALCARBON-N` and not on our hashed
`project_id` — which is why the adapter writes `detail_url` onto every project
row, and why the `settings.PROJECT_DETAIL_URLS` fallback cannot build a working
link for this registry. A test pins that every row carries its own URL.

## Politeness

Unchanged from the rest of the project: ~1 req/s, on-disk response cache,
backoff on failure. A full sync is four requests, so there is nothing here to
tune. No `robots.txt` is served. The data is public by design and the API is
unauthenticated by the operator's choice; we send what the site's own
application sends and nothing more, and we do not read the account types the
API also exposes.
