# Carbon Registry Scraper

Pulls every carbon project from the public carbon registries into a local
database, then exports the spreadsheet the business asked for.

Seven registries are live: **Verra VCS**, **Gold Standard**, **Cercarbono**,
**Plan Vivo**, **SocialCarbon**, **BioCarbon** and **Puro.earth** — 9,891
projects. They share one database and one spreadsheet; the `Registry` column
tells the rows apart.

Written for someone who has not built a scraper before — the sections below go
in order. If you are here to *use* the tool rather than work on it, skip to
[Installing it](#installing-it-for-the-business-team).

## What it produces

| File | What it is |
|---|---|
| `data/verra.db` | SQLite database. The source of truth: ~5,200 Verra projects plus the full Units ledger, 4,141 Gold Standard projects plus 182,989 credit blocks, 231 Cercarbono projects, 118 Puro.earth, 105 BioCarbon, 32 Plan Vivo and 19 SocialCarbon. |
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

## The window

```bash
carbon-gui
```

Tick registries, choose a folder, press a button. Two buttons, and the
difference matters:

- **Export Excel** — writes the next version of the spreadsheet from data
  already stored. Seconds, no network. This is the everyday one.
- **Update registry data** — the scrape. Hours, with the estimate shown before
  it starts, and a Cancel that takes effect in about a second. It deliberately
  writes **no** spreadsheet: press Export Excel afterwards, so a new delivery
  version only appears because someone decided to send one.

Everything below is the same pipeline, driven from a terminal instead.

## Sharing it with the business team

```powershell
verra status        # the dates in here are the dates the business will read
.\build.ps1
```

`build.ps1` runs the tests, builds the database the app carries, freezes it and
produces two artifacts. Needs PyInstaller (`pip install -e ".[build]"`) and, for
the installer only, Inno Setup (`winget install JRSoftware.InnoSetup`) — pass
`-SkipInstaller` without it.

| | |
|---|---|
| `dist/portable/CarbonRegistryScraper-0.2.0-portable.zip` | **hand this one out.** Unzip and run; no install |
| `dist/installer/CarbonRegistryScraper-0.2.0-setup.exe` | per-user installer, for anyone who wants a Start-menu entry |

Neither is signed, and that is the whole reason the ZIP is preferred. See
[SmartScreen](#smartscreen-and-why-the-zip) below.

Whichever they get, the deal is the same:

- **No admin rights and no Python.** The EXE carries its own Python; the
  installer lands under `%LOCALAPPDATA%\Programs`, the ZIP wherever they
  extract it.
- **A database in the box.** ~20 MB, every registry, so **Export Excel works
  immediately** — before anyone has scraped anything. Updating is opt-in and
  takes hours; exporting takes seconds.
- **Their data is theirs.** Everything the program writes lives in
  `%LOCALAPPDATA%\CarbonRegistryScraper` — the database, the spreadsheets, and
  their own edits to `fields-asked.txt` and the derivation rules. Uninstalling,
  or deleting the extracted folder, does not touch any of it, and the next
  version picks it back up. The shipped database is only ever copied in when
  there is no database at all.
- **Never fresher than `data/verra.db` was when you built it.** The window
  shows **"Data as of &lt;date&gt;"** at the top, taken from the *oldest*
  registry, so staleness is visible rather than assumed. Hence `verra status`
  before `build.ps1`.

Both artifacts, plus `carbon-seed.db` on its own, go up as
[GitHub release](https://github.com/raulpbarros/rb-carbon-scraper/releases)
assets:

```powershell
gh release create v0.2.0 --notes-file packaging/release-notes-pt.md `
    dist\portable\CarbonRegistryScraper-0.2.0-portable.zip `
    dist\installer\CarbonRegistryScraper-0.2.0-setup.exe `
    dist\seed\carbon-seed.db
```

### SmartScreen, and why the ZIP

Windows tags every downloaded file with a *Mark of the Web*, and shows
*"Windows protected your PC"* for anything marked and unsigned — Run is hidden
behind **More info**. Signing it away needs an Authenticode certificate, which
is neither free nor a code change.

Removing the mark is free:

> Right-click the `.zip` → Properties → tick **Unblock** → OK. **Then** extract.

One click, before anything runs, and nothing extracted afterwards trips
SmartScreen at all. `LEIA-ME.txt` inside the ZIP is the Portuguese version of
this, and it is step 1 for a reason. The same Unblock works on the setup `.exe`;
the ZIP is still preferable because one unblock covers every file in it and
there is no install step for a locked-down machine to object to.

What this does *not* solve: corporate antivirus sometimes quarantines unsigned
PyInstaller executables on heuristics alone. If that happens, the source route
below is the way in.

### Run it from the source

For colleagues who have a terminal — and the fallback if AV eats the EXE.

```powershell
git clone https://github.com/raulpbarros/rb-carbon-scraper.git
cd rb-carbon-scraper
python -m venv .venv
.venv\Scripts\activate
pip install -e .
mkdir seed        # then put carbon-seed.db from the release into it
carbon-gui
```

No downloaded executable, so no SmartScreen at any point.

Two things worth knowing:

- **`seed\carbon-seed.db` is picked up automatically.** `settings.SEED_DB` is
  `RESOURCE_ROOT/seed/carbon-seed.db`, and `RESOURCE_ROOT` is the repo root in a
  checkout, so the first run copies it to `data\verra.db` — the same first-run
  path the frozen build takes. Without it the window opens on an empty database
  and Export has nothing to write. `seed/` is gitignored.
- **`pip install -e .` — editable, deliberately.** A plain `pip install .` puts
  the package in `site-packages`, and `settings._user_root()` derives the
  writable tree from `__file__`, so the database and the spreadsheets would land
  inside the virtualenv. Fixing that properly is a path-logic change, not a
  documentation one; until then, install editable.

## Normal use

```bash
verra sync                    # scrape every registry (rate-limited on purpose)
verra totals                  # exact Verra credit totals  (~35 min)
verra derive                  # fill the classified columns
verra export                  # write the next out/carbon-projects_vN.xlsx
```

The first three together are `verra update` — a refresh, with no spreadsheet
written. Exporting stays a separate, deliberate act, because each export is a
new version and versions are what the business was sent.

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

### Running the scrape in Docker

A full sync is hours of headless HTTP with no window involved, so it can run in
a container instead of tying up the desktop:

```powershell
cd docker
docker compose run --rm sync        # sync + totals + derive, every registry
docker compose run --rm publish     # hand the database back to data/verra.db
```

The database lives on a Docker volume rather than a bind mount — SQLite runs in
WAL mode here and WAL does not survive a Windows bind mount cleanly. See
[docker/README.md](docker/README.md), which is worth reading before the first
build. It does not replace `build.ps1`: the Windows EXE and installer are still
built on Windows.

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
- **Plan Vivo's 2 rows are Plan Vivo *V5*.** The adapter reaches the PV Climate
  registry, which is the whole of the V5 system; Plan Vivo has certified
  projects since 2008 and those sit on a registry we have not found yet. See
  PLAN.md 5e. The number is right for what it covers and low for what the
  column name suggests.

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
