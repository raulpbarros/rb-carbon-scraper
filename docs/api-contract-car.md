# Climate Action Reserve — API contract

Measured against the live service on **2026-08-05**, in **28 requests**, all
HTTP 200. Everything here was observed, not inferred from documentation; where
a figure is quoted it is what the registry returned on that date. Where
something was not proved, it says so.

## What it is

| | |
|---|---|
| Front end | `https://thereserve2.apx.com` (`climateactionreserve.org/registry` 301s to `/mymodule/mypage.asp`) |
| Backend | **the same host.** Classic ASP — `Version 40.0.0`, `Powered by APX Technology` |
| Shape | one report page, `GET /myModule/rpt/myrpt.asp?r=<id>`, paged by **POSTing its own form back to itself** |
| Auth | none. No key, no login, no `Origin` check, no Cloudflare challenge. A session cookie and a CSRF token are required for the POSTs — both come free from the GET |
| Projects | 1,277 |
| Credit records | 5,170 issuance rows + 11,044 retirements + 2,277 cancellations |
| Requests per sync | **~1,285** — 8 for the whole ledger set via CSV, and 1,277 per-project detail pages, which are the only source of a crediting period |
| Contract doc | this file |

**There is a bulk CSV export and it is unauthenticated.** That is the single
most important fact here. Every one of the four reports downloads complete in
one POST — 1,277 / 5,170 / 11,044 / 2,277 rows — so the ledger half of a sync
is **8 requests, not ~400 pages**. See "The CSV export" below, and its traps,
which are real.

### Xpansiv/APX is a platform, not a registry

The fifth platform this project has met, after S&P Platts, legacy Markit,
EcoRegistry and ICE GreenTrace. The same ASP module (`myrpt.asp`, the `xxxx2`
form, `submitform2`, `rptdownload.asp`) serves every tenant below.

| tenant | host | what it is | in scope |
|---|---|---|---|
| **Climate Action Reserve** | `thereserve2.apx.com` | 1,277 projects, tCO2e offsets | **yes — this doc** |
| **Climate Forward** | `climateforward.apx.com` | **36 projects**, *ex-ante forecast* units, not issued offsets | a different unit and a different business decision. One subclass away — see below |
| ~~American Carbon Registry~~ | `acr2.apx.com` | **left this platform.** The host answers HTTP 200 with "You have reached an invalid page" for every path | no — ACR is on ICE GreenTrace, see `docs/api-contract-acr.md` |
| NAR, TIGR, I-REC(E), I-TRACK(G) | various `*.apx.com` | renewable-energy certificates | **out of scope — not tCO2e** |
| Digital Fuels, Fly-i, MiQ | various `*.apx.com` | fuel and methane-intensity certificates | **out of scope — not tCO2e** |

**The Climate Forward tenant was checked, not assumed** — one GET of
`climateforward.apx.com/myModule/rpt/myrpt.asp?r=111` returned the identical
five forms (`frmRequery`, `frmSearch`, `frmPrint`, `frmDownload`, `xxxx2`), the
identical ten `xxxx2` field names, the same `submitform2` / `downloadnow`
functions and the same `rptdownload.asp` action, and printed `1 - 36 : 36`.
Same contract, same module. Two differences matter and are covered under
trap 7.

## The form contract

`GET /myModule/rpt/myrpt.asp?r=<id>` renders the report and **six forms**. Only
two of them are the data path:

| form | action | method | what it does |
|---|---|---|---|
| `xxxx2` | `/myModule/rpt/myrpt.asp` | POST | **paging and sorting** |
| `frmDownload` | `/myModule/include/rptdownload.asp` | POST | **the bulk export** |
| `frmSearch` | `/myModule/include/search.asp` | POST | opens a popup search window |
| `frmPrint` | `/myModule/include/print.asp` | POST | print / PDF preview |
| `frmRequery` | *(none — same page)* | POST | clears a search |
| `Login` | `/mymodule/checkLogin.asp` | POST | not used |

### Paging — form `xxxx2`

Ten fields, all `<input type="hidden">`, plus a CSRF token that is **not in the
markup** (see trap 1):

| field | page 1 value | what paging sets |
|---|---|---|
| `r` | `111` | unchanged — the report id |
| `X999myquery` | `` | unchanged |
| `X999tablenumber` | `2` | unchanged |
| `X999csv` | `` | unchanged |
| `X999sort` | `` | `Asc` / `Desc` when sorting |
| `X999action` | `` | unchanged |
| `X999actionfield` | `` | unchanged |
| `X999field` | `` | the **column heading**, verbatim, when sorting |
| `X999paging` | `` | **`On`** |
| `X999whichpage` | `` | **the 1-based page number** |
| `c16e` | *(injected by JS)* | the CSRF token — see trap 1 |

The page's own JS is the whole specification:

```js
function submitform2(X999sort,X999field,X999paging,X999whichpage,X999csv,X999action,X999actionfield) {
     document.xxxx2.X999csv.value = X999csv;
     document.xxxx2.X999action.value = X999action;
     document.xxxx2.X999actionfield.value = X999actionfield;
     document.xxxx2.X999sort.value = X999sort;
     document.xxxx2.X999field.value = X999field;
     document.xxxx2.X999paging.value = X999paging;
     document.xxxx2.X999whichpage.value = X999whichpage;
     document.xxxx2.submit();
}
```

- Next page → `submitform2('','','On','2','','','')`
- Last page → `submitform2('','','On','26','','','')`
- Sort → `submitform2('Asc','Project ID','','','','','')`
- Goto → `submitform2('','','On',ThePage,'','','')`, and the page's own
  `GotoPage()` validates `1 <= ThePage <= 26`, which is where the page count is
  published in machine-readable form.

**Proved, not assumed.** POSTing that body for page 2 of `r=111` returned
`51 - 100 : 1277`, 50 rows, 24 columns, and **zero** Project IDs in common with
page 1's 50. Fixtures `car-projects.html` and `car-projects-page2.html` are
those two responses.

### The export — form `frmDownload`

`downloadnow(1)` sets one field and submits:

```js
function downloadnow(iFormatType) {
    var hdnFormatType = eval("frmDownload.FormatType");
    switch(iFormatType) {
        case 1: hdnFormatType.value = "csv"; break;
        case 2: hdnFormatType.value = "txt"; break;
        case 3: hdnFormatType.value = "pdf"; break;
    }
    frmDownload.submit();
}
```

`POST /myModule/include/rptdownload.asp`, `application/x-www-form-urlencoded`,
15 fields — **all of them scraped from the rendered page, none invented**:

```
c16e, myFilter, Data, Title, Exclude, Columns, Masks, ClassMasks,
Headings, Parameters, ParametersOriginal, SortORder, FormatType,
ReplaceExpression, ReplaceValue
```

`Data=Stamp_0` is the session's stored recordset handle; `Title` is the report
name; `Exclude` / `Columns` / `Headings` are the column presentation. **Note
`SortORder`, whose `name` attribute is spelled with a capital `R` in the middle
while its `id` is `SortOrder`.** Copy the form's `name`, never its `id`.

Response: `HTTP 200`, `Content-Type: application/save`,
`Content-Disposition: attachment; filename=temp.csv;` — the whole report,
never just the current page.

## Report ids

Scraped from the site's own "Public Reports" menu, with the record counts each
one printed on 2026-08-05:

| `r=` | report | records | ingested |
|---|---|---|---|
| **111** | **Projects** | **1,277** | **yes** |
| **112** | **Project Offset Credits Issued** | **5,170** | **yes** |
| **206** | **Retired Offset Credits** | **11,044** | **yes** |
| **308** | **Canceled Offset Credits** | **2,277** | **yes** |
| 211 | Compliance Projects | not fetched | no — an ARB/WA-ECO view of projects already in `r=111` |
| 309 | Credit Status | not fetched | no |
| 706 | Buffer Pool Account Balance | not fetched | no — reserve balances, same shape as ACR's `buffered-pool` |
| 207 | Search Serial Numbers | not fetched | no |
| 109 | Accounts Disclosed to Public | not fetched | no — account data |
| 210 | ODS Projects' Certificates of Destruction | not fetched | no |
| 1 | Participating Companies | not fetched | no — account data |
| 101 | "More…" | not fetched | no |

The four ingested reports were fetched and exported; the other eight were read
off the menu only, and their counts are **not** measured. Do not quote a number
for them from this file.

## Reconciliation

**Every report prints its own total**, in a `<td align="center">` of its own,
directly above the pager:

```html
<td align="center">1 - 50 : 1277</td>
```

`<first> - <last> : <total>`. Present on all four reports, on every page
(`51 - 100 : 1277` on page 2, `1251 - 1277 : 1277` on the last), and it is
what `INCOMPLETE` is checked against. There is no total in the CSV, in a
header, or anywhere else — which is why the HTML GET is not optional even on
the CSV route.

Measured on 2026-08-05, entirely from the four CSVs:

| what | against what | result |
|---|---|---|
| 1,277 / 5,170 / 11,044 / 2,277 CSV rows | the printed `: <total>` | **exact, all four** |
| CSV `Project ID`s | distinct, `r=111` | 1,277 of 1,277 — no duplicates |
| ledger `Project ID`s | the project list | **0 orphans** on all three ledgers |
| issuance rows summed per project | the list's own `Total Number of Offset Credits Registered` | **1,277 of 1,277 agree, to the unit** — 268,262,717 both ways |
| `r=111` page 1 ids | `r=111` page 2 ids | 0 overlap |

Because the registry's own per-project figure agrees with the ledger on every
project, there is **no need for `iter_credit_totals`** — the same conclusion
ACR reached, and for the same measured reason. Nothing here is stated twice
with two different answers.

Ledger totals for the record: 268,262,717 issued (901 projects with any),
84,941,473 retired (419 projects), 124,235,312 cancelled (398 projects).

## The traps

### 1. `c16e` is a CSRF token, it is not in the form, and without it every POST silently becomes the home page

The `xxxx2` form's markup contains **no** `c16e` field. It is appended by
script, one second after `window.load`:

```js
function addhiddenCsrfInputToQTable() {
    setTimeout(function () {
        const elements = document.querySelectorAll("[id='xxxx2']");
        for (var i = 0; i < elements.length; i++) {
            if (elements[i].querySelectorAll(":scope > [name='c16e']").length == 0) {
                const hiddenInput = document.createElement('input');
                hiddenInput.setAttribute('name', "c16e");
                hiddenInput.setAttribute('value', 'cde72a6cfa7f72b986a8cba1aa01e181');
                elements[i].appendChild(hiddenInput);
            }
        }
    }, 1000);
}
```

Read the value out of that `<SCRIPT>` block, or — easier and equivalent — out
of any of the four other forms, which do carry it as ordinary markup.

**And the failure mode is the reason this is trap number one.** A POST with a
missing *or stale* token returns **HTTP 200**, 16 KB, and the site's public
**home page** — nav, login box, and a three-column "Message Type / Message /
Receive Date" table reading "No Records!". Nothing raises. A scraper that
counts rows sees an empty report; a scraper that looks for a heading finds one.
This cost an hour here: the first page-2 attempt reused a token copied from an
earlier session's page and got exactly that.

| what was sent | what came back |
|---|---|
| fresh session cookie + that session's token | **the report, `51 - 100 : 1277`** |
| fresh session cookie, **no** `c16e` | the home page, 200 |
| fresh session cookie, a **previous session's** `c16e` | the home page, 200 |
| the same, plus `?r=111` on the URL as well as in the body | the home page, 200 |
| the same, plus a `Referer` | the home page, 200 |

The token is **per session, not per response**: four consecutive responses in
one client all carried `a769d346108b2013d43ad454c031778c`, and a new client
got a new one. So it is scraped once per session and reused, which is what
makes a cached-response adapter workable.

### 2. A session cookie is required for every POST, and the GET is what mints it

`GET myrpt.asp?r=111` works with no cookie at all and replies
`Set-Cookie: ASPSESSIONIDCQSDTQQD=…; path=/; HttpOnly; secure; SameSite=Strict`.
Both POST paths need that cookie — `frmDownload` sends `Data=Stamp_0`, a
handle to a recordset the server stored in **that** session when it rendered
the page. So the sequence is fixed and cannot be shortened:

```
GET  myrpt.asp?r=<id>            -> session cookie + c16e + the printed total + the column headings
POST rptdownload.asp             -> the entire report as CSV
```

Two requests per report, four reports, **8 requests for every credit record in
the registry**. The ASP session name is a per-tenant string
(`ASPSESSIONIDCQSDTQQD` here, `ASPSESSIONIDCWTDSRTD` on Climate Forward) and
must not be hardcoded — accept whatever cookie the GET sets.

### 3. The CSV is not correctly quoted, and a standard reader mis-columns 13 rows without complaining

Every field is wrapped in `"`, and **an embedded `"` is not doubled.** A value
that contains both a quote character and a comma therefore splits into an
extra field. From `r=206`, raw:

```
…,"Environmental Benefit",""Retirement of Carbon Offsets in behalf of Palo Alto City Utilities, from CARBIOIN project in Oaxaca. Developed under Climate Action Reserve Mexico Forest Protocol"",
```

The registry's own value has literal quotes around it; the exporter wrapped it
again. RFC-4180 reads the leading `""` as one escaped quote, then ends the
field at the internal comma.

| report | rows | rows whose field count ≠ the header's |
|---|---|---|
| `r=111` projects | 1,277 | 0 |
| `r=112` issuances | 5,170 | 0 |
| `r=206` retirements | 11,044 | **13** |
| `r=308` cancellations | 2,277 | 0 |

`csv.reader` raises nothing on any of them; the row simply has 24 fields where
the header has 23. In **all 13 observed cases** the break falls in
`Retirement Reason Details`, the last populated column, so every column before
it stays aligned — which is why the orphan and quantity checks above still came
out clean. **Do not rely on that position.** The rule that holds is
structural: compare each row's field count against the header's and handle the
mismatch, rather than assuming which column absorbed it. Fixture
`car-retirements.csv` carries the header, 8 clean rows and **all 13 malformed
ones**, precisely so this can be tested offline.

A second, unrelated quoting fact, which is fine but will fool a line counter:
`r=308` is 2,417 physical lines and 2,277 records — some `Cancellation Reason`
and `Export Notes` values contain newlines, correctly quoted. **Never count
`\n` to count rows.**

### 4. The CSV columns are not the HTML columns

Close, but not equal, and the differences run both ways:

| report | HTML grid | CSV | difference |
|---|---|---|---|
| `r=111` | 24 | 25 | one **trailing empty** column (the exporter emits a final comma) |
| `r=112` | 33 | 34 | trailing empty |
| `r=206` | 22 | 23 | trailing empty |
| `r=308` | **15** | **17** | trailing empty **plus `Export Notes`**, which the grid's own `Exclude` list hides and the CSV ships (67 of 2,277 populated) |

So the CSV is a superset on `r=308` and identical-plus-padding elsewhere. Map
columns **by header name**, never by position, and drop the unnamed trailing
column.

**The CSV also loses every hyperlink.** In the grid, `Documents`, `Data`,
`Project Name`, `Project Developer` and `Project Owner` are `<a>` elements; in
the CSV they are their bare text, so `Documents` and `Data` both read the
literal string `View` on all 1,277 rows. The project's detail URL is therefore
not in the CSV — see trap 6, where it is derived instead.

### 5. There is no page-size lever, and the pager clamps past the end instead of ending

The grid is **50 rows**, fixed. The complete `X999` vocabulary on the page is
ten names (`X999myquery`, `X999tablenumber`, `X999csv`, `X999sort`,
`X999action`, `X999actionfield`, `X999field`, `X999paging`, `X999whichpage`,
`X999mydata`) and none of them is a page size; there is no `<select>` on the
page at all.

Not concluding that from an absence, it was probed: one POST carrying
`PageSize=200`, `RecordsPerPage=200`, `X999pagesize=200`, `rowsperpage=200`,
`Length=200` and `max=200` all at once returned **HTTP 200,
`1 - 50 : 1277`, and 50 rows counted**. Ignored, in the usual silence. This is
the sixth registry here to ignore or clamp a page size — but the first where it
does not matter, because the CSV route makes paging unnecessary.

**Asking for a page past the end returns the last page, not an empty one.**
`X999whichpage=27` against a 26-page report returned `1251 - 1277 : 1277` and
**27 rows** — page 26 again. So a pager that stops on "an empty page" never
stops, and a pager that stops on "a short page" stops one page early on any
report whose last page is partial. If anything ever has to page here, stop on
the printed range: `<last> >= <total>`. (This is the legacy Markit pager trap
in a new shape — there "Next →" was never disabled; here the page number is
silently clamped.)

### 6. The per-project detail page exists, is public, and is the only source of a crediting period

**Answer to "is there a per-project page": yes.** The `Project Name` cell of
the projects grid links to

```
https://thereserve2.apx.com/mymodule/reg/prjView.asp?id1=<numeric part of the CAR id>
```

`CAR1957` → `id1=1957`. Checked on every grid row available — **227 rows,
216 distinct projects across five fetched pages, 0 mismatches** — so the URL is
derivable from `Project ID` alone and does not need the grid's hyperlink (which
the CSV strips anyway). It is served to a **cold client with no cookie**.

It is also the only place the crediting period appears. Labels on the page,
verbatim:

```
Project ID · ARB ID · Offset Project Operator · Authorized Project Designee ·
Project Name · Project Description · Project is Being Transferred From Another
Registry · Crediting Period · Project Type · Project Commencement Date ·
Project Reporting Start Date · Project Website · Project Site Location ·
State/Province/Department · Country · Project Status · Crediting Period Expires ·
Project Listed Date · Project Registered Date · Verification Bodies ·
Reporting Periods Eligible for Early Action ·
Reporting Periods Approved for Early Action ·
Early Action Offset Quantification Methodology · Documents
```

`Project Commencement Date`, `Project Reporting Start Date` and
`Crediting Period Expires` are the dates; the projects report carries none of
them. **That, and only that, is what makes a sync ~1,285 requests instead of
8.** The two other linked cells go nowhere useful: `Documents` →
`/mymodule/reg/TabDocuments.asp?…&id1=<same id>` is the project's public PDFs,
and `Data` → `/mymodule/reg/TabProjectEmissions.asp?…&id1=<same id>` is its
emissions/reductions table; neither is read.

**`Crediting Period` on the issuance report is not a date and not a
substitute.** It is a label — `Initial` (291), `Renewed-Second` (30),
`Renewed-Third` (1) — and blank on 4,848 of 5,170 rows. The column name is the
trap.

**A bad `id1` is a soft 404.** `prjView.asp?id1=999999` returns **HTTP 200**
and a 3 KB page reading *"Invalid URL, the Reserve Administrator has been
notified!"*. `raise_for_status()` sees nothing. Same shape as EcoRegistry's
`ERROR_401` inside a 200.

### 7. Climate Forward is the same module and a different build — do not key a parser on colour

The tenant check found the contract identical and **two things that would break
a parser written against Climate Action Reserve's markup**:

| | Climate Action Reserve | Climate Forward |
|---|---|---|
| platform version | `Version 40.0.0` | `Version 1.5.7` |
| `c16e` CSRF token | present, required | **absent — the page has none** |
| session cookie | `ASPSESSIONIDCQSDTQQD`, `HttpOnly; SameSite=Strict` | `ASPSESSIONIDCWTDSRTD`, neither flag |
| grid header colour | `#92b7d6` | `#F9B25F` |
| grid row colour | `eef6f9` | `FDE5C9` / `F0DBBE` |
| `Server:` header | suppressed | `Microsoft-IIS/10.0` |
| forms, `xxxx2` fields, `submitform2`, `downloadnow`, `rptdownload.asp` | — | **identical** |

So `c16e` must be sent **if the page publishes one** and omitted otherwise, and
the grid must be found **structurally** — the table whose header cells carry
the `submitform2('Asc',…)` sort links — not by matching a hex colour. The
column set differs too: Climate Forward says `Project Proponent` where the
Reserve says `Project Developer`, and has no compliance columns.

Climate Forward printed `1 - 36 : 36`. Its units are **ex-ante forecast**
credits rather than issued offsets, so whether it ships at all is a business
decision, not a scraping one — the same call `iter_credit_totals` forced for
Plan Vivo's `fPVC`. It is not ingested and has no adapter.

### 8. No country name is published, only an ISO code

`Project Site Country` is `US` (774), `MX` (491), `CA` (7), `CN` (3), `AR` (2)
on all 1,277 projects — a code, never a name — and the detail page states the
same code. **This is ACR's gap exactly**, in a second registry, and
`continent.yaml` reads `country_code`, so Continent derives cleanly while
`País` has nothing to fill it from.

**And it lands hardest on Bioma, which is worth measuring before the adapter is
written rather than after.** Running `biome.yaml`'s current `applies_when`
regex over the 1,277 project types:

| | projects |
|---|---|
| pass the land-use gate | **674** |
| of those, `US` — reach `north-america-temperate-by-code` | 189 |
| of those, `MX` — reach **nothing** | **485** |

Mexico is 485 of the 674, all of them `Forestry - MX`, and there is no Mexican
band in `biome.yaml` keyed on a code — every existing country band matches the
country *name*, which this registry does not publish. So the largest single
group of land-use projects the registry has would come out blank, silently, in
the exact way `CLAUDE.md` warns about. That is a rule to measure and propose,
not a gap to assume away, and the blast-radius count belongs on the real
database before anything is added.

Separately, `Avoided Grassland Conversion` (39 projects) is land use and
misses the gate on every alternative — a wording that belongs in the
`applies_when` discussion alongside it.

### 9. It sends Windows-1252 and declares no charset anywhere

Measured 2026-08-05, on the report pages, the detail pages **and** the CSV
exports:

| | |
|---|---|
| `Content-Type` | `text/html` — no `charset` |
| `<meta charset>` / `<meta http-equiv>` | **none, on any page** |
| BOM on the CSV | none |
| bytes on the wire | **Windows-1252** — `0xC9` for `É`, `0xE9` for `é`, `0x92` for the curly apostrophe in the site's own English text |

So every HTTP client that follows the spec assumes UTF-8, and httpx decodes
with `errors="replace"`. The result is **HTTP 200 with every accented character
in the registry replaced by `U+FFFD`**, and nothing raised:

```
STATE OF M<?>XICO      MICHOAC<?>N      Oaxaca de Ju<?>rez
ASOCIACI<?>N DE SILVICULTORES DE LA REGI<?>N FORESTAL
"The project<?>s submittal form"   "the Reserve<?>s program"
```

**This registry is 38% Mexican** — 491 of 1,277 projects, and the accented
states are `STATE OF MÉXICO` (52), `MICHOACÁN` (45), `YUCATÁN` (5) and
`QUERÉTARO` (1) — so it is not a cosmetic issue in a free-text field. It is
also **not recoverable afterwards**: every accented character collapses onto
the same replacement character, so a database written this way cannot be
repaired without re-fetching.

The fix is `http_client.decoded(response, fallback)`, wired in through
`APXReportAPI.encoding = "cp1252"`. Order: **what the server declared**, then
**UTF-8 strictly**, then the fallback — strict so that a tenant on a newer
build (Climate Forward is a different platform version) still decodes as UTF-8
rather than being mojibaked by a constant measured here.

The response cache stores raw bytes, so fixing this cost a **re-run**, not a
re-scrape.

### 10. `?r=111&pg=2` returns page 1

A silently ignored query parameter, at HTTP 200 — established before this
session and consistent with everything above: paging is a POST, and `myrpt.asp`
reads nothing else from the query string but `r`.

## What was checked and *not* found

Stated so nobody spends the afternoon again:

- **No rate limiting observed.** 28 requests in about six minutes at roughly
  one every 1.5 seconds, including four full-report exports totalling 7 MB —
  **28 of 28 were HTTP 200**. No `429`, no `Retry-After`, no `cf-ray`, no
  Cloudflare interstitial, no slowdown, no `401`. This is **not** evidence of
  no limit: it is 28 requests. ACR's Cloudflare rule only bit at ~100 in ten
  minutes, and a full sync here is ~1,285 requests. Treat the default ~1 req/s
  as unproven-safe and watch the first long run.
- **No `robots.txt`** — not checked this session; not fetched, so nothing is
  claimed about it.
- **No JSON anywhere.** No API host, no XHR, no embedded payload. The HTML
  table and the CSV are the two transports and there is no third.
- **No `groupKeys`-style aggregation, no filter parameters proved.** The
  `frmSearch` popup and `myFilter` / `Parameters` / `myWHERE` fields plainly
  exist and **none of them was exercised** — the whole registry fits in four
  exports, so nothing needed narrowing. **If a filter is ever used here, prove
  it narrows before partitioning on it**; this codebase has met the
  silently-ignored filter at Verra, Gold Standard and Markit, and the `pg=2`
  behaviour above says this host ignores parameters quietly too.
- **`downloadnow(2)` (TXT) and `downloadnow(3)` (PDF)** exist in the JS and
  were not tried. CSV works; there was no reason.
- **No `rowspan` or `colspan` in any of the four grids** — checked on all four:
  zero occurrences. The table is rectangular, so the legacy Markit
  rowspan-continuation reader is not needed here.
- **Sorting works and paging does not need it** — see below.

## Sorting

`X999sort=Asc` + `X999field=Project ID` was sent and **applied**: page 1 came
back `CAR1000, CAR1001, CAR1002, CAR1003, CAR1004 … CAR1065` where the
unsorted default had begun `CAR1957, CAR1460, CAR1458`. Page 2 of that sorted
view began `CAR1066` — **contiguous with page 1's last row, no gap and no
overlap**. Every column heading is offered as a sort key, `Asc` and `Desc`,
and the value passed is the heading verbatim (`Project ID`,
`Cooperative/ Aggregate ID`, …).

Two things follow. The sort is **lexicographic, not numeric** — `CAR1000`
sorts before `CAR102` — so it orders pages stably but does not order projects
sensibly. And **it is not needed**: the CSV route does not page at all. Verra's
pager *must* sort by `entityId`, because without it Elasticsearch reordered
between pages and an early run silently lost 1,271 of 5,244 projects; that
whole class of bug is avoided here by not paging. If the HTML grid is ever
paged anyway, sort by `Project ID` first — a stable key is available and
costs nothing.

## The four report tables — columns, verbatim

The next adapter maps these onto `db.PROJECT_FIELDS` / `db.CREDIT_EVENT_FIELDS`.
Order is the order the registry emits. CSV adds one **unnamed trailing** column
to each (omitted below) and, on `r=308` only, `Export Notes` before it.

### `r=111` Projects — 24 columns, 1,277 rows

```
Project ID · Compliance Program ID · Cooperative/ Aggregate ID ·
Project Developer · Project Owner · Project Name · Offset Project Operator ·
Authorized Project Designee · Verification Body · Project Type · Status ·
Compliance Program Status · Project Site Location · Project Site State ·
Project Site Country · Additional Certification(s) · SDG Impact ·
Project Notes · Total Number of Offset Credits Registered  ·
Project Listed Date · Project Registered Date · Documents · Data ·
Project Website
```

**`Total Number of Offset Credits Registered ` has a trailing space** in the
header, in both the grid and the CSV. Strip on read or match exactly; do not
retype it from memory.

Fill rates over all 1,277, from the CSV:

| column | filled |
|---|---|
| Project ID, Compliance Program ID, Project Developer, Project Owner, Project Name, Project Type, Status, Project Site Location, Project Site State, Project Site Country, Documents, Data | 1,277 |
| Project Listed Date | 1,272 |
| Compliance Program Status | 1,264 |
| Total Number of Offset Credits Registered | 901 |
| Project Registered Date | 910 |
| Offset Project Operator | 504 |
| Authorized Project Designee | 482 |
| Project Website | 473 |
| Verification Body | 426 |
| Cooperative/ Aggregate ID | 378 |
| SDG Impact | 231 |
| Project Notes | 95 |
| **Additional Certification(s)** | **0** |

Vocabularies as published: `Status` is `Registered` (540), `Completed` (461),
`Listed` (268), `Transitioned` (8). `Project Type` is **32 distinct values** led by
`Forestry - MX` (485), `Improved Forest Management - ARB Compliance` (141),
`Landfill Gas Capture/Combustion` (126), `Livestock - ARB Compliance` (108),
`Ozone Depleting Substances - U.S. - ARB Compliance` (96). Carried through
untranslated, like every registry here.

`Compliance Program ID` is populated on all 1,277 but reads the literal string
`N/A` where there is none — it is not a blank, and a naive `stated()` keeps it.

### `r=112` Project Offset Credits Issued — 33 columns, 5,170 rows

```
Date Issued · Project ID · Cooperative/ Aggregate ID · Project Name ·
Project Developer · Project Owner · Project Type · Reduction/Removal ·
Reversible/Non-Reversible · Protocol and Version · ARB Eligible ·
WA ECO Eligible · Eligible for CORSIA 2021-2023 Compliance Period ·
Eligible for CORSIA 2024-2026 Compliance Period · Corresponding Adjustment ·
ICVCM CCP Eligible · Crediting Period · Vintage ·
Total Offset Credits Issued · Zero-Credit Reporting Period ·
Offset Credits Currently in Reserve Buffer Pool ·
Offset Credits Intended for Compliance Buffer Pool ·
Offset Credits Converted to VCUs · Canceled for Compliance · Canceled ·
Project Site Location · Project Site State · Project Site Country ·
Activity Area Type · Additional Certification(s) · Verification Body ·
Project Website · Documents
```

Fill: `Total Offset Credits Issued`, `Vintage`, `Date Issued`,
`Protocol and Version`, `Project Type` all 5,170/5,170;
`Verification Body` 5,164; `Offset Credits Currently in Reserve Buffer Pool`
1,781; `ICVCM CCP Eligible` 1,682; `Project Website` 1,571;
`Canceled for Compliance` 1,358; `Activity Area Type` 1,351;
`Cooperative/ Aggregate ID` 1,109; `Reduction/Removal` 2,025;
`Crediting Period` 322; `Canceled` 166;
`Zero-Credit Reporting Period` 41; `Reversible/Non-Reversible` 18;
`Offset Credits Converted to VCUs` **2**; `Additional Certification(s)` **0**.

`Protocol and Version` (5,170/5,170) is the methodology, and it is the only
thing separating the forestry projects into improved management,
reforestation and avoided conversion — ACR's `Metodologia` situation exactly.

**`Offset Credits Converted to VCUs` is populated on 2 rows.** A cross-registry
overlap with Verra is stated by the registry itself, on those two rows, and
should be read before ingesting anything — see "what else holds these credits"
in `CLAUDE.md`. Not investigated in this session.

### `r=206` Retired Offset Credits — 22 columns, 11,044 rows

```
Vintage · Offset Credit Serial Numbers · Quantity of Offset Credits ·
Status Effective · Project ID · Project Name · Project Type ·
Reduction/Removal · Reversible/Non-Reversible · Protocol and Version ·
Project Site Location · Project Site State · Project Site Country ·
Activity Area Type · Additional Certification(s) ·
Eligible for CORSIA 2021-2023 Compliance Period ·
Eligible for CORSIA 2024-2026 Compliance Period · Corresponding Adjustment ·
ICVCM CCP Eligible · Account Holder · Retirement Reason ·
Retirement Reason Details
```

`Account Holder` 11,044/11,044, `Status Effective` (the retirement date)
11,044/11,044, `Retirement Reason Details` 8,859/11,044.

`Retirement Reason`: `On Behalf of Third Party` (5,894),
`Environmental Benefit` (2,451), `Retirement for Person or Organization`
(1,044), `Retail Program Requirements` (855), `Other` (609),
`Compliance Requirements` (184), `Compliance – Queretaro` (4),
`Compensation for Avoidable Reversal` (2), blank (1).

**`Account Holder` is the holder, not the beneficiary** — 5,894 rows say `On
Behalf of Third Party` and name that third party only in the free-text
`Retirement Reason Details`. That is BioCarbon's `to_name` and ACR's
`registryAccountName` again, and there is **no structured beneficiary field**
here at all. Whether to parse prose is a decision for the business, not the
adapter; `credit_events.reason` is the only copy, which is what keeps it
reversible.

### `r=308` Canceled Offset Credits — 15 columns in the grid, 16 named in the CSV, 2,277 rows

```
Vintage · Offset Credit Serial Numbers · Quantity of Offset Credits ·
Status Effective · Project ID · Project Name · Project Type ·
Reduction/Removal · Reversible/Non-Reversible · Protocol and Version ·
Project Site Location · Project Site State · Project Site Country ·
Additional Certification(s) · Cancellation Reason
[· Export Notes — CSV only, hidden from the grid by its own Exclude list,
   67 of 2,277 populated]
```

`Cancellation Reason`: `ARB` (2,006), `Canceled` (241),
`Canceled for ARB` (21), `WA ECO` (8),
`Canceled for Regulatory Compliance` (1).

**2,035 of 2,277 cancellations are conversions**, out to California's ARB or
Washington's Ecology compliance registry, where the units continue to exist —
**ACR's trap 6 in a second registry**, and at a higher proportion (89% here
against 86% there). ACR's settled answer was to report the registry's own
figure and store the reason on every row so the split is a query
(user's decision, 2026-08-04); the same shape applies, but it is the business's
call to make again, not this document's.

## Cost, and the shape it implies

| step | requests |
|---|---|
| `GET r=111` + `POST` CSV | 2 |
| `GET r=112` + `POST` CSV | 2 |
| `GET r=206` + `POST` CSV | 2 |
| `GET r=308` + `POST` CSV | 2 |
| `GET prjView.asp?id1=…` × 1,277 | 1,277 |
| **total** | **~1,285** |

About 7 MB for the four CSVs (3.9 MB of it retirements) and ~11 MB for the
detail pages at ~8.5 KB each. At the default ~1 req/s that is a little over
twenty minutes — but see "no rate limiting observed": 1,285 requests is far
past what was tested, and this is the same volume that earned ACR an hour-long
Cloudflare ban.

**Two things follow for the adapter's shape.** The ledger half is cheap, so
`--projects-only` saves almost nothing here — the crediting-period fan-out is
the whole cost, and it is on the project side. And because the detail page is
99.4% of the requests, the response cache is what makes a second run fast;
every write being an idempotent upsert means a run stopped part-way through the
fan-out resumes rather than restarts.

## Fixtures

All fetched 2026-08-05 from the live public reports. Public registry data only;
no credentials, no account data. The `c16e` values they contain are expired
per-session CSRF tokens and are not reusable.

| file | what | trimmed? |
|---|---|---|
| `tests/fixtures/car-projects.html` | `GET myrpt.asp?r=111` — page 1 | **yes** — grid data rows 50 → 10 |
| `tests/fixtures/car-projects-page2.html` | the page-2 **POST** response, `51 - 100 : 1277` | **yes** — 50 → 10 |
| `tests/fixtures/car-issuances.html` | `GET myrpt.asp?r=112` | **yes** — 50 → 10 |
| `tests/fixtures/car-retirements.html` | `GET myrpt.asp?r=206` | **yes** — 50 → 10 |
| `tests/fixtures/car-cancellations.html` | `GET myrpt.asp?r=308` | **yes** — 50 → 10 |
| `tests/fixtures/car-projects.csv` | the `r=111` CSV export | **yes** — 1,277 → first 60 rows |
| `tests/fixtures/car-retirements.csv` | the `r=206` CSV export | **yes** — header, 8 clean rows, and **all 13 malformed rows** of trap 3 |

**Four of them were captured before trap 9 was found**, through the UTF-8
decode, so `ASOCIACIÓN`, `REGIÓN`, `José María Morelos`, `Indígena`, `Xiacuí`,
`Juárez`, `México` and the site's own curly apostrophes arrived as `U+FFFD`.
They were repaired on 2026-08-05 by looking each damaged run up in a correctly
decoded copy of the same live source — a replacement character stands for
exactly one byte that is *not* ASCII, which is what tells `Juárez` from the
`Juarez` the same feed also contains — so the characters restored are the
registry's own bytes and not a guess about which accent it was. A test asserts
no `car-*` fixture carries a replacement character, because a capture taken
through a broken decoder is evidence of nothing.

**What the trim kept, byte for byte:** every `<form>` and all its inputs, every
`<script>` including `addhiddenCsrfInputToQTable`, `submitform2`, `downloadnow`
and `GotoPage`, the column-header row with its sort links, the printed
`<first> - <last> : <total>` line, and the whole pager block. Only data rows
11-50 of each grid were removed, and each file ends with an HTML comment saying
so. **The printed range still reads `1 - 50` while ten rows are present** —
that is the trim, not the registry, and a test must not assert row count
against it.
