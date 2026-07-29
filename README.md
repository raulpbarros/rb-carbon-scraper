# Carbon Registry Scraper

Pulls every carbon project from the public carbon registries into a local
database, then exports the spreadsheet the business asked for.

Two registries are live: **Verra VCS** and **Gold Standard**. They share one
database and one spreadsheet — the `Registry` column tells the rows apart.

Written for someone who has not built a scraper before — the sections below go
in order.

## What it produces

| File | What it is |
|---|---|
| `data/verra.db` | SQLite database. The source of truth: ~5,200 Verra projects plus the full Units ledger, and 4,141 Gold Standard projects plus 182,989 credit blocks. |
| `out/carbon-projects_vN.xlsx` | The deliverable. A **new version every time** — see below. |

The spreadsheet's columns are read from `assets/fields-asked.txt` at runtime.
**To change the sheet, edit that file** — not the code.

### Deliveries are versioned, never overwritten

Each `verra export` writes the next version — `carbon-projects_v1.xlsx`,
`_v2`, `_v3` — and leaves the previous one exactly as it was. Once a
spreadsheet has gone to the business, the numbers in it stay recoverable.
The command prints which file it wrote and which one it kept.

## Setup (once)

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows.  macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium       # only needed for `verra discover`
```

## Normal use

```bash
verra sync                    # scrape every registry (rate-limited on purpose)
verra totals                  # exact Verra credit totals  (~35 min)
verra derive                  # fill the classified columns
verra export                  # write the next out/carbon-projects_vN.xlsx
```

One registry at a time, with `-r verra` or `-r gs`:

```bash
verra sync -r gs --projects-only   # 4,141 Gold Standard projects, ~1 min
verra sync -r gs                   # + 182,989 credit blocks, ~2 h
verra sync -r verra                # ~5,200 projects + Units ledgers, ~2.5 h
```

`verra totals` is Verra-only and is not optional there. The retirements ledger has 305k rows and cannot
be paged reliably (see [CLAUDE.md](CLAUDE.md) — the API silently ignores some
filters), so the credit totals are fetched per project instead. Without this
step, `Total Credits Sold` and `Total Credits Retired` undercount.

Gold Standard needs no equivalent: its whole credit stream pages cleanly and
every run reconciles against the registry's own count.

Then have a look at what you got:

```bash
verra status           # how many rows of each kind, per registry
verra coverage -r gs   # how full each spreadsheet column is
```

A full sync caches roughly 1 GB of API responses under `.cache/` so that re-runs
don't re-hit the registry. Once you are happy with the data:

```bash
verra cache --clear
```

Trying it out first is a good idea — this stops after 50 records per dataset:

```bash
verra sync --limit 50
```

## Why three separate commands

Scraping takes ~20 minutes. Classification rules are guesses that will need
correcting. Keeping them apart means **fixing a rule never re-scrapes the
registry**:

```bash
# edit config/derivation/biome.yaml
verra derive && verra export        # seconds, no network
```

## How it works

Both registries are JavaScript apps; there is no HTML to parse. Each talks to
a JSON API, and the scraper calls those APIs directly — far faster and more
reliable than driving a browser.

- **Verra** talks to a private API on S&P Global's Platts platform.
  `verra discover` is the tool that found it: it opens the real site in a
  browser, records every request, and writes the result to
  `docs/api-contract.md`. **If the Verra scraper ever breaks, run `verra
  discover` first** — S&P changes this API without notice, and that command
  tells you what changed.
- **Gold Standard** has a plain public REST API at
  `public-api.goldstandard.org`. No discovery step needed; the contract is
  written down in [docs/api-contract-gs.md](docs/api-contract-gs.md).

Each registry lives in its own adapter under
`src/carbon_scraper/registries/`. Adding a third one means writing an adapter,
not changing the pipeline.

Full detail: [CLAUDE.md](CLAUDE.md), [docs/api-contract.md](docs/api-contract.md)
and [docs/api-contract-gs.md](docs/api-contract-gs.md).

## Things worth knowing before you trust the numbers

Read [docs/field-mapping.md](docs/field-mapping.md) — it says exactly where each
column comes from. The short version:

- **Most columns are real registry data.** Project ID, name, project type,
  country, dates, yearly ex ante.
- **`Tipo Macro de Projeto` speaks each registry's own vocabulary.** Verra rows
  say "Energy industries (renewable/non-renewable sources)"; Gold Standard rows
  say "Energy Efficiency - Domestic". Both are carried through exactly as
  published — that is the intended shape of the column, so read it alongside
  `Registry`. To filter across registries, use `Tipo Micro de Projeto`, which
  is derived into one shared vocabulary.
- **Gold Standard leaves `Cidade`, `Estado` and `Additional Certification`
  blank** — it does not publish them. `Metodologia` is filled for about 60% of
  its projects.
- **`Total Credits Sold` = retired VCUs.** That is the business's chosen
  reading, so this column and `Total Credits Retired` hold the same number.
  `config/credits.yaml` can switch to a narrower definition.
- **`Tipo Micro`, `Bioma` and `Durabilidade` are classified by rules**, not
  published by either registry. They live in `config/derivation/*.yaml` and are
  a first pass awaiting your review. `Bioma` is the weakest — a state usually
  spans several biomes and the rules pick the dominant one, and it barely fills
  at all for Gold Standard, which publishes no state.
- **Blank means unknown.** The scraper never invents a value or substitutes
  zero. If a cell is empty, the registry did not publish it and no rule
  matched.

## Being a good citizen

The scraper sends the same requests the public website sends, at about one per
second, and only ever touches public endpoints. Please do not raise the rate
limits in `src/carbon_scraper/settings.py`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every request returns HTTP 500 | Missing the `registry` / `standardid` / `standardacronym` / `language` headers, or the API changed | `verra discover` |
| `Playwright is required` | Browser not installed | `playwright install chromium` |
| Scrape seems to re-fetch nothing | Responses are cached for 24h | `verra sync --refresh` |
| A column is empty everywhere | No rule matched, or the registry does not publish it | `verra coverage -r <registry>`, then check `docs/field-mapping.md` |
| Every Gold Standard request returns HTTP 403 | The browser `User-Agent` is missing — the edge blocks anything else | It is set in `settings.BROWSER_HEADERS`; do not strip it |
| Gold Standard returns far fewer projects than expected | `size` was raised above the measured cap — `projects` silently clamps to 150 | Leave `GS_PROJECT_PAGE_SIZE` / `GS_CREDIT_PAGE_SIZE` alone |
