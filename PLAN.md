# PLAN — from developer CLI to a GUI across 10 registries

**2026-08-05 — the installed-app plan is abandoned.** User decided against
building/shipping a signed installer: distribution is the GitHub repo link
itself, handed to the boss directly. A colleague clones the repo and runs it
from source (README, "Run it from the source") — `pip install -e .`, then
either `carbon-gui` or the CLI. Phase 4 (Packaging) and 4b (Distribution) are
closed as **superseded**, below — not reworked toward this, and not deleted;
the code still exists and still passes its tests, it is just no longer the
target. The GUI itself is unaffected and stays the everyday way to run this.

Two things changed along the way:

- **More registries.** Verra, Gold Standard, Cercarbono, Plan Vivo,
  SocialCarbon, BioCarbon, Puro.earth, ACR and **CAR** are live — CAR added
  after ACR, because ACR's dead old host named the platform CAR is still on.
  Ten in total. Phase 5 keeps each independently droppable.
- **A different user.** The deliverable stopped being a spreadsheet a
  developer mails out and became a GUI a colleague runs from a checkout: tick
  the registries, pick a folder, press a button — same interaction, no
  installer underneath it.

The hard part was never the GUI. It was that the pipeline assumed a writable
repo checkout at a path derived from `__file__` — false under PyInstaller and
under any non-editable install — and that a full scrape of nine-then-ten
registries is a working day of wall-clock time. Phase 0 fixed the first; the
shipped-database model from Phase 4 still fixes the second, and still applies
to a from-source checkout via `seed/carbon-seed.db` — see README.

**Settled decisions** (do not re-open without a reason): ship a prebuilt
database so export is instant and scraping is an explicit opt-in; Tkinter/ttk;
**distribute by repo link and "run from source", not a built installer.**

---

## Where things stand — 2026-08-05

| Phase | | |
|---|---|---|
| 0 — Foundation | ✅ | path split, dispatch table, `pipeline.py`, cancellation |
| 1 — Cercarbono | ✅ | 231 projects, live and reconciled |
| 2 — Plan Vivo | ✅ | Verra adapter generalised to `platts/`; 2 projects, reconciled |
| 3 — Tkinter GUI | ✅ | `carbon-gui` — checkboxes, folder picker, two buttons, Cancel |
| 4 — Packaging | 🛑 superseded | built and working — 20.8 MB shipped DB, EXE, per-user installer — but not the distribution path anymore |
| 4b — Distribution | 🛑 superseded | was portable ZIP + GitHub release; replaced by repo link + "run from source" |
| 4c — Docker | ✅ | the ~16,500-request scrape, headless, off the desktop |
| **5 — More registries** | **✅** | **5a ✅, 5b ✅, 5c ✅, 5d ✅, 5e ✅, 5f ✅, Verra JNR ✅** |
| 6 — Hardening | | incremental sync, `verra doctor` |

**619 tests, green, offline.** Nine registries live and **eleven standards
across eight platforms**: Verra VCS 5,245 + JNR 5, Gold Standard 4,141,
**CAR 1,277**, ACR 994, Cercarbono 231, Puro.earth 118, BioCarbon 105,
Plan Vivo V5 2 + V4 30, SocialCarbon 19 — **12,162 projects in one database**,
every one carrying a derived Tipo Micro / Durabilidade (Bioma only where the
project is land use). Last delivery is `out/carbon-projects_v2.xlsx`; a `_v3`
has still not been cut, because nobody has asked for one.

**Three overlaps between registries are now known, and the third is a
different animal.** BioCarbon's `BCR-CO-319-14-002`/`-005` are Cercarbono's
`CDC-106`/`CDC-107`, and `ACR0242`/`ACR0388` are Verra's `VCS 559`/`573` — both
pairs are *different tranches* of one physical project, so both rows ship
cross-linked (user's decision, 2026-08-04). **CAR's `CAR400`/`CAR498` against
Verra's 1528/1527 are not**: CAR states the conversion itself, Verra's entire
issued figure for both is exactly what CAR says it converted, and CAR cancels
nothing — so 50,000 tCO2e are counted twice and nothing is subtracted to hide
it. Together they are what stops "sum every registry's Total Credits Issued"
being a safe query. See `docs/field-mapping.md`.

**The "0 projects retiring more than they issued" invariant no longer holds,
and that is a finding rather than a regression.** Six Plan Vivo V4 projects
retire more than the registry publishes as issued — Sofala most starkly at
**0 issued against 273,836 retired**, confirmed by querying the issuance feed
directly rather than inferred from a gap. Nothing is back-computed to make the
arithmetic close. See `docs/field-mapping.md`.

**An installer exists and works, and is no longer the plan.** `.\build.ps1`
still produces `dist/installer/CarbonRegistryScraper-0.2.0-setup.exe` — installs
per-user, no admin rights, carries a 20.8 MB database — but the user decided
against handing that to the boss. The hand-over is the repo link instead:
clone it, `pip install -e .`, `carbon-gui`. Phase 4b's ZIP-plus-GitHub-release
plan is dropped for the same reason — there is nothing to sign or unblock
when nothing is downloaded as an executable in the first place.

**Phase 5 is finished.** ACR (5c) was the last name on the original list, and
it was not where the plan said: it left the APX platform for **ICE
GreenTrace**, which turned out to be a seventh platform — and one with a second
tenant, ART, sitting there for whenever somebody wants it. Every item on the
phase is done, including the two it grew: the Plan Vivo adapter reached the V5
registry only and V4 turned out to be a whole second platform, and the host ACR
*left* turned out to still be serving **CAR** (5f), which nobody had put on the
list at all.

What is left is Phase 6. Phase 4b's open items (code signing, staleness
threshold) are closed as moot — see "Open questions" below.

---

## Phase 0 — Foundation ✅

No new registries, no GUI. Everything downstream is blocked on this, and none of
it may change observable CLI behaviour — the existing suite is the guard.

- [x] **0.1** Split `settings.ROOT` into `RESOURCE_ROOT` (read-only, bundled,
      `sys._MEIPASS` when frozen) and `USER_ROOT` (writable,
      `%LOCALAPPDATA%\CarbonRegistryScraper` when frozen). Env overrides
      `CARBON_HOME`, `CARBON_DATA_DIR`, `CARBON_OUT_DIR`, `CARBON_CACHE_DIR`,
      `CARBON_LOG_DIR`, `CARBON_CONFIG_DIR`, `CARBON_ASSETS_DIR`. First-run
      seeding of the editable config into `USER_ROOT` via `seed_user_files()`,
      so an installed user can still edit `fields-asked.txt` rather than code.
- [x] **0.2** `registries.ADAPTERS` dispatch table replaces the `if VERRA /
      else GoldStandard` fall-through, which silently returned the Gold Standard
      adapter for any registry it did not recognise.
- [x] **0.3** `excel._project_url` uses `settings.PROJECT_DETAIL_URLS`; a
      registry with no template gets a blank cell instead of a Verra link.
- [x] **0.4** `pipeline.py` extracted from `cli.py`, with a `ProgressSink`
      protocol and central `_Throttle` (adapters call progress once per record —
      ~183k times on a full Gold Standard sync). `cli.py` is now a thin Typer
      layer passing a `ConsoleSink`.
- [x] **0.5** Cancellation: a `threading.Event` threaded through
      `RegistryClient`, checked between requests and slept on during retry
      backoff, so a Cancel button works instead of blocking for up to 45 s.
- [x] **0.6** `VERRA_NO_CACHE` / `VERRA_CACHE_TTL` / `MAX_RETRIES` read per call
      instead of at import, which previously made `--refresh` a no-op for any
      eager importer.
- [x] **0.7** Dropped `pandas` and `pydantic` — declared, imported nowhere,
      ~60-80 MB of bundle.
- [x] **0.8** This file, plus the CLAUDE.md updates.

**Verified:** 58 tests green throughout; `CARBON_HOME=<tmp>` puts the database,
cache and seeded config under the override and leaves the checkout untouched;
live `sync -r gs --limit 25` → `derive` → `export` runs clean end to end;
exporting twice into a redirected folder produces `_v1` then `_v2` with `_v1`
unchanged.

---

## Phase 1 — Cercarbono ✅

Base `https://api-front.ecoregistry.io/platform`, headers `platform:
ecoregistry` and `lng`. Contract in `docs/api-contract-cercarbono.md`.
**CO2 standard only** (user's decision) — EcoRegistry also hosts
`cercarbono-biodiversity` and `cercarbono-circular-economy`, whose credits are
not tCO2e.

- [x] `docs/api-contract-cercarbono.md`, structured like `api-contract-gs.md`
- [x] `settings`: identifier, label, API base, headers, detail-URL template,
      standard slug (overridable via `CARBON_CERCARBONO_STANDARD`)
- [x] `registries/cercarbono/api.py` — three cached bulk feeds drive both
      iterators; the per-project detail supplies the crediting period
- [x] Ledgers named `issuances` / `retirements`, **not** `credits`:
      `db.credit_totals()` branches on that exact string to pick Gold
      Standard's status-bucketing semantics
- [x] Fixtures + tests (29, mirroring `tests/test_goldstandard.py`), including
      `assert set(row) <= set(db.PROJECT_FIELDS)`. The fixtures deliberately
      carry off-standard records so the CO2 filter is tested, not assumed
- [x] Derivation rules for its own vocabulary: methodology-keyed AFOLU rules
      (no sub-type code is published), country-name Continent table (no ISO
      code is published), Colombia split by department

Three things the plan did not anticipate, all now documented:

- **`Origin` is validated.** The shared `BROWSER_HEADERS` carries Verra's site
  and EcoRegistry answers HTTP 500 to it. Worse, `http_client._send_once`
  merged headers into a plain `dict`, so httpx's lowercased `origin` and the
  adapter's `Origin` were both sent and the override did nothing. Now merged
  through `httpx.Headers`.
- **The serial feed is not the issued total.** `CDC-196` publishes its 2022 and
  2023 issuances under two serial revisions (161,297 rows against a true
  120,448); `CDC-106`/`CDC-107` are converted-in ex-BioCarbon projects missing
  from the feed entirely while still having retirements. The registry's own
  `certificatedVerification` is authoritative and a third endpoint confirms it.
- **New optional adapter seam**: `iter_credit_totals(resource)`, reached
  through `registries.base.credit_totals_of()`. Registries without one are
  unaffected.

**Verified:** 87 tests green; full live sync reconciles exactly — 231/231
projects, 2,529/2,529 issuance serials, 9,350/9,350 retirements, no
`INCOMPLETE`; 0 projects report retiring more than they issued; three
spot-checked projects match the live pages; `verra coverage -r cercarbono`
shows 100% on Project ID / País / Continent / Total Credits Issued.

---

## Phase 2 — Plan Vivo, by generalising the Verra adapter ✅

Plan Vivo runs on **the same S&P Platts backend as Verra**:
`registry.spglobal.com/config/environment.config.json` returns the identical
routing table, and the app bundle declares
`{"name":"pvclimate","registry":"PVCL","defaultStandard":"PV"}`.

- [x] `discover` gains `-r/--registry` and moves to
      `registries/platts/discovery.py`. Captures are named per registry so two
      tenants of one platform cannot overwrite each other's contract file or
      fixtures
- [x] `registries/verra/api.py` → `registries/platts/api.py`, parameterised by
      registry/standard; Verra and Plan Vivo are thin subclasses. Everything
      measured stays shared: `MAX_LIMIT = 400`, `MAX_WINDOW = 10_000`, the
      mandatory `entityId` sort, `filter_narrows`, per-partition reconciliation
- [x] Ledger set decided from the platform's own `publicReportExport`, not
      assumed from Verra. It matches Verra's four; `ASSIGNMENTS` and
      `NOT_DELIVERED` are exposed but have no searchable resource
- [x] `docs/api-contract-planvivo.md`
- [x] Verra suite re-run: **87 tests green, not one assertion changed**
- [x] New `verra standards -r <name>` command — the cheap half of `discover`

**The `standardId` never needed Playwright.** The platform publishes it
unauthenticated at `{cmsResources}/public/standardsByRegistry/<CODE>`, which
the app calls on every page load. `discover` was still refactored and run, but
as the diagnostic it is, not because the contract needed it.

Four things the plan did not anticipate:

- **A wrong `standardId` is invisible.** The planning guess
  (`100000000000004`, from a legacy Markit URL) returns HTTP 200 with
  `totalEntities: 0` — no exception, no rows, nothing in the log. The real one
  is `671000000000001`.
- **`BCCR` is the "BC Carbon Registry" — British Columbia, not BioCarbon.**
  Phase 5a assumed it might be BioCarbon and can stop assuming.
- **A field name that exists is not a field that is populated.** Verra's column
  map applied cleanly and produced three empty columns: Plan Vivo leaves
  `sectoralScope`, `vcsProjectId` and `regionName` null and carries the
  vocabulary in `projectType` instead. The map is rebuilt with `sectoralScope`
  *removed*, not shadowed — two fields writing one column would let dict order
  decide whether a null overwrote a real value.
- **`unitType` cannot see the reserves.** Plan Vivo issues `rPVC`/`fPVC`
  alongside `Achievement Reserve` and `Future Risk Buffer`, and `unitType`
  reads the same across them. The adapter records `unitClass`. `fPVC` are
  forward credits (the platform's own lookup flags them `isVerified: false`)
  and are 103,246 of the 213,145 issued units — counted as issued, because the
  registry's own published figure counts them, and **raised with the business
  in `docs/field-mapping.md` rather than silently reinterpreted**.

**Verified:** 117 tests green (87 unchanged + 30 new); live sync reconciles
exactly — 2/2 projects, 27/27 issuances, 10/10 holdings, 0/0 retirements, 0/0
cancellations, no `INCOMPLETE`; issued total 213,145 matches the "Issued Units"
figure on Plan Vivo's own public page; both projects spot-checked against
`registry.spglobal.com/pvclimate/public/pv/projects/<id>`; a full export from
the real database produces **9,619 rows × 25 columns** across all four
registries, with the previous delivery byte-untouched.

**Caught during that verification, and worth recording as a process failure
rather than a code one:** the real database had **no Cercarbono rows at all**.
Phase 1's live sync ran under a scratch `CARBON_HOME`, which proved the adapter
and left the deliverable database untouched — a run that finished clean, with
nothing in any log to say the data had gone somewhere else. Fixed by re-running
`sync -r cercarbono` against the real database (231/231 projects, 2,529/2,529
serials, 9,351/9,351 retirements). The general lesson is the one this repo keeps
re-learning: a run finishing without an exception says nothing about where its
rows landed. `verra status` is the check, and it belongs at the end of a phase,
not only at the start of the next one.

**Blast radius of the derivation changes, measured:** the two new country-name
biome bands (Miombo, Mesoamerica) also refine 70 African and 49 Central
American rows in Verra and Gold Standard, including 12 Panamanian projects that
were reading "Floresta Temperada Norte-Americana". The Phase-1 Colombian
department split also landed on the real database for the first time, moving 63
Verra projects out of the Amazon basin. No project lost a biome.

---

## Phase 3 — Tkinter GUI ✅

`src/carbon_scraper/gui/` — `state.py`, `worker.py`, `app.py`, split by what is
allowed to touch a widget. Registered as `carbon-gui` under `gui-scripts` so no
console appears. Drives `pipeline`, **never** the Typer commands — and a test
enforces that rather than trusting anyone to remember it.

- [x] Registry checkboxes generated from `REGISTRY_LABELS`, each showing local
      row count and last sync from the `runs` table (`db.registry_summary`).
      A test fails if any registry identifier is hard-coded in `app.py`
- [x] Output folder picker, remembered; feeds `pipeline.export(out_dir=...)`,
      which keeps versioning — it is not an overwrite flag
- [x] **Two buttons, deliberately separate**: `Export Excel` (derive + export,
      seconds, no network) and `Update registry data` (sync + Verra's exact
      totals + derive, hours, opt-in, estimate shown before it starts)
- [x] Progress bar + per-registry line fed by a queue sink drained on
      `root.after`; never a widget call from the worker thread
- [x] Log pane via a `logging.Handler` installed before `basicConfig`, so
      `INCOMPLETE` reconciliation errors are visible; everything also to
      `LOG_DIR/gui.log`
- [x] Cancel wired to the Phase 0.5 event; **measured at 0.6 s** against a live
      Cercarbono sync, and reported as "stopped", not as a crash
- [x] Uncaught worker exceptions → dialog + `LOG_DIR/error-<task>-<stamp>.log`,
      with the path in the dialog

Four things the plan did not anticipate:

- **The checkboxes could not honour both buttons without widening the registry
  filter.** `db.all_projects` took one registry or all; a subset ticked would
  have scraped four and exported everything. `pipeline.selected/only`,
  `db.all_projects` and `excel.build_rows` now take a sequence. The trap it
  opened is that an **empty** selection must mean *no rows*, not everything —
  treating it as falsy would make an untick silently export the whole database.
  `db.registry_clause` pins that, and a test mutates it to prove the test bites.
- **The time estimate cannot come from the `runs` table.** A repeat run reads
  most of its responses from the ~1 GB cache and finishes in minutes, so
  measured history would promise four minutes and then take two hours. The
  estimate is a static table in `settings`; past durations are reported as
  history, never as a forecast. A registry missing from the table makes the
  estimate *unknown* rather than quietly short.
- **A cancelled first sync looked like a fresh install.** Found by running it,
  not by reading it: the caption said "nothing stored yet" for a Cercarbono
  sync that had been cancelled, which is indistinguishable from a registry
  nobody has ever tried — and one of those means "press the button again".
  Failed and cancelled attempts are now shown even when the registry is empty.
- **`pipeline.sync(refresh=True)` leaked.** It set `VERRA_NO_CACHE` and never
  cleared it, which is invisible in a CLI that exits and permanent in a window
  that does not: one refreshed run would have disabled the cache for every run
  after it, for the life of the process.

**Verified:** 164 tests green (117 unchanged + 47 new), all offline and none
opening a window. The window itself was built, driven and torn down headlessly,
then opened for real through `main()`. End to end against live registries in a
scratch tree: a Plan Vivo update wrote **no** spreadsheet (correct — that is the
other button), two exports produced `_v1` then `_v2` with `_v1` byte-identical
afterwards, and a live Cercarbono sync cancelled in 0.6 s leaving a readable
database. The real `out/` and `data/` were never touched.

### 3b — The visual pass ✅ (2026-08-05)

Same behaviour, same seams, a designed surface: `gui/theme.py` (palette, fonts,
ttk styles, DPI, icon), a header band carrying the headline and the "data as of"
trust line, ledger rows that double as the progress display, ranked buttons, a
collapsed log, and `assets/app.ico` from `packaging/make_icon.py`.

**Every defect below was found by opening the window and looking at it**, not
by reading the code — which is the whole argument for doing that here:

- **The packaged app was blurry on every screen above 100%.** No process-level
  DPI awareness, so Windows rendered at 96 DPI and upscaled the bitmap. Invisible
  on a development box at 100% and unfixable after `tk.Tk()` exists.
- **The window opened smaller than its own layout**, clipping the right of every
  ledger row and the folder block off the bottom. `minsize` does not prevent it;
  an explicit `geometry` does.
- **And then the opposite**: opening the log pushed the window past a 1080-line
  screen at 150%, and clamping it squeezed the log *and its own toggle* out of
  view. The button moved into the body so only the log carries the row weight.
- **`indicatorsize` does not scale with the screen**, so the tick boxes were a
  third of their apparent size — and clam draws a **cross** in a ticked box.
  Both fixed by taking the indicator out of the layout and using a `☑` glyph.
- **A 12-character count column silently truncated `305,144 / 305,144`** into
  `305,144 / 30`, which reads as a smaller number rather than a cut one.
- The screenshots that showed all of this were themselves wrong twice over:
  a stale process from before the edits, then a capture that was not DPI-aware
  and returned a 150%-scaled crop of the window. **Check the tool before
  believing what it says about the thing** — the same rule as every registry
  count in CLAUDE.md.

**Verified:** 507 tests green, all offline, none opening a window. The window
was then opened for real and driven through both its idle and mid-scrape states
with fake progress, and each defect above re-checked against a screenshot.

---

## Phase 4 — PyInstaller, Inno Setup, shipped database ✅, superseded 2026-08-05

**Superseded, not deleted.** Built and verified as below; the user then
decided against handing the boss an installer at all — the repo link is the
delivery now (see top of file). Nothing here is wrong, it is just not the
path taken. `db.export_slim()` / `verra slim-db` still matter: the
"run from source" route in the README still uses a shipped `seed/carbon-seed.db`
so a checkout doesn't force a from-scratch scrape.

- [x] `db.export_slim()` + `verra slim-db`: `projects`, `credit_totals`,
      `project_derived`, `runs` only. **214.8 MB → 20.8 MB, 91% smaller**, and
      the sheet it builds is identical — 9,619 rows × 25 columns, zero cell
      mismatches against the full database
- [x] PyInstaller one-folder spec, `--noconsole`, playwright and pytest
      excluded. Data files and hidden imports are **derived** in
      `packaging/bundle.py` from `settings.SEEDED_FILES` and
      `registries.ADAPTERS`, not listed
- [x] `packaging/carbon-registry.iss`: per-user install (no admin rights),
      Start-menu and optional desktop shortcut, and **no `[UninstallDelete]`**
      — the uninstaller leaves `%LOCALAPPDATA%\CarbonRegistryScraper` alone
- [x] `build.ps1`: clean → pytest → slim-db → PyInstaller → Inno Setup, with
      the bundle checked for the four files that must be in it
- [x] `settings.seed_database()`: first run adopts the shipped database, and
      **only when there is no database at all**

**Verified**, on this machine rather than a clean VM (see below): the full
build produces a 53.5 MB folder and a 16.7 MB installer; installing it raises
no UAC prompt and lands in `%LOCALAPPDATA%\Programs`; the installed EXE creates
`%LOCALAPPDATA%\CarbonRegistryScraper`, adopts the 20.8 MB database and seeds
the editable config, with an empty log and no traceback file; `derive` against
that installed tree writes the same 47,582 values the full database does, and
two exports produce `_v1` then `_v2`; uninstalling removes the program, the
shortcut and the Add/Remove entry and **leaves every byte of the data**.

**Still open — a genuinely clean machine.** Everything above ran on the
development box, which has Python 3.12 and the venv on it. The frozen EXE
carries its own `python312.dll` and was observed running from the installed
folder, so nothing suggests a hidden dependency; but "no Python installed" has
not actually been tested, and that is the one claim the installer makes that
this machine cannot check.

**SmartScreen:** the installer is unsigned, so the first run shows "Windows
protected your PC" with Run anyway behind *More info*. Not fixable in code — it
needs an Authenticode certificate. `build.ps1` prints the warning at the end so
whoever sends the file knows to warn the recipient. Raise signing separately.

Three things the plan did not anticipate:

- **`SELECT *` across two databases assumes column *order*.** `ALTER TABLE ADD
  COLUMN` appends, so `runs.registry` sits last in any migrated database and
  third in `SCHEMA`. The positional copy shifted every value one place along
  and only failed because `started_at` is NOT NULL — with one more nullable
  column it would have shipped a database full of plausible nonsense. Copied by
  name now, and pinned by a test.
- **A shipped total has to lose to a scraped one.** The slim database carries
  the credit buckets in `credit_totals` because `credit_events` is gone. Left
  at equal authority they would outrank the ledgers of any registry the user
  later re-scraped, and the sheet would stay frozen at whenever the installer
  was cut — no error, right shape, wrong numbers. `credit_totals.source`
  separates `seed` from `registry`, `credit_totals()` ranks them, and
  `pipeline.sync_one` drops the seed once a registry's ledgers have been
  scraped **in full** (not after `--limit`, not after `--projects-only`).
- **PowerShell 5.1 treats a native program's stderr as a terminating error**
  under `$ErrorActionPreference = "Stop"`. PyInstaller logs its entire INFO
  stream to stderr, so a successful freeze aborted the build — but only when
  the output was piped, which is exactly the case nobody tries before handing
  the script over. `Invoke-Native` checks the exit code instead.

---

## Phase 4b — Getting it to the business team, unsigned — superseded 2026-08-05

**Superseded.** The two checklist items below (cut a GitHub release, verify on
a clean machine) are dropped, not merely deferred: the user chose repo-link
distribution instead of chasing SmartScreen/AV on an unsigned EXE. The rest of
this phase's work (ZIP packing, READ-ME, README's "run from source" section)
stays, because "run from source" is now the *only* route, not a fallback for
people without a terminal.

Phase 4 built an installer. It cannot be *sent*: it is unsigned, so the first
person to double-click it meets SmartScreen's "Windows protected your PC" with
the Run button hidden behind *More info*. An Authenticode certificate is the
only thing that removes that, and it costs money and — since June 2023 — a
hardware token. **The blocker was never the build.**

So the fix is not to make the warning trustworthy, it is to remove what
triggers it. SmartScreen's reputation check fires on files carrying a **Mark of
the Web**, the `Zone.Identifier` stream Windows attaches to anything
downloaded, and Explorer copies that mark onto everything extracted from a
downloaded archive. Unblocking the `.zip` first — right-click → Properties →
Unblock — strips it once, before any executable has run, and nothing extracted
afterwards is checked at all.

Two routes, both free, both off the existing public repo:

| | Portable ZIP | Clone + editable install |
|---|---|---|
| For | everyone, including non-technical | the colleagues with a terminal |
| Needs | nothing — carries its own Python | Python 3.11+ |
| SmartScreen | gone, if the ZIP is unblocked first | never appears — no downloaded EXE |
| Data | seed DB inside the ZIP, adopted on first run | seed DB dropped into `seed/` |
| Code change | ZIP step in `build.ps1` + a pt-BR READ-ME | **none** |

- [x] `build.ps1` packs `dist/portable/CarbonRegistryScraper-<v>-portable.zip`
      after the bundle check and before the installer, so `-SkipInstaller`
      still produces the artifact that actually gets shared. Version read from
      `pyproject.toml`, not hardcoded a third time
- [x] `packaging/LEIA-ME.txt` — Portuguese, travels inside the ZIP, unblock as
      step 1. It quotes the window's real English button labels rather than
      translating them
- [x] `packaging/release-notes-pt.md` — the same unblock instruction, because
      people download from the release page and never open the READ-ME first
- [x] README: three ways to get it, plus why the source route needs
      `pip install -e .` specifically
- [x] `seed/` gitignored — the source route has people dropping a 21 MB
      database into the checkout
- [x] Tests: the `.iss` version pinned to `pyproject.toml`'s, and the READ-ME
      pinned to `build.ps1` still copying it in
- [~] Cut `v0.2.0` on GitHub with the ZIP, the installer and `carbon-seed.db`
      — **dropped.** No release is being cut; the boss gets a repo link.
- [~] **Verify on a machine that is not this one** — **dropped**, moot once
      nothing is downloaded as an executable.

**The antivirus question is now academic rather than open.** Corporate AV
heuristically quarantining an unsigned PyInstaller EXE was the risk that made
"run from source" the documented fallback. It has become the plan by user
choice, not because the risk was confirmed — so the source route needs to
actually work for a non-technical boss, which is the thing to check now
instead.

**The installer still exists** (`build.ps1` still builds it) but is no longer
being sent anywhere. Reopen this phase only if the user decides they want a
Start-menu entry after all.

---

## Phase 5 — Remaining registries

Same recipe each time, each independently droppable: discover or bundle-grep →
contract doc → settings entries → adapter → fixtures and tests → derivation
rules. The GUI checkbox appears on its own.

**5e is not like the others.** It is not a registry nobody has looked at — it
is a registry we are already half-scraping and did not know it.

**Before writing any new adapter, check the four platform tables.**
`verra standards -r all` names every S&P tenant in eight GETs,
`docs/api-contract-markit.md` lists the 21 programmes on legacy Markit,
`docs/api-contract-acr.md` lists the two ICE GreenTrace tenants and
`docs/api-contract-car.md` the Xpansiv/APX ones. That is 32 registries
reachable by subclassing something that already works, and it is how 5e turned
out to be an afternoon instead of a week — and how 5f turned out to exist at
all.

**Then check whether the site ships the data in its own HTML**, before going
looking for an API. 5b was filed as "server-rendered HTML, find a JSON
endpoint" and is neither: it is a Next.js app whose server-rendered payload
carries the whole registry as JSON inside the page. There is no endpoint, and
looking for one is time spent proving a negative.

**Then check whether the registry is already reachable from another angle, and
refuse it if it is.** SocialCarbon is on the legacy Markit list *and* has its
own current system; ingesting both would have counted the same credits twice.
Two of the four registries below had a known double-count risk and **both
turned out to be real**: 5d against legacy Markit, and 5a against Cercarbono's
converted-in projects. 5d carried a third one *inside its own API*, and 5a's is
the one case where the answer was to keep both rows rather than drop one — the
two registries publish different tranches of the same project. Ask what else
already holds these credits before scraping, not after.

- [x] **5a BioCarbon — done, 2026-08-04.** ~~first check whether `BCCR` is
      BioCarbon~~ **it is not**: the S&P standards lookup names it "BC Carbon
      Registry", British Columbia (`140000000000001` / `BC`). Settled in
      Phase 2. Not on legacy Markit either. So it was a real adapter — and
      the Vite-bundle grep was the whole of the discovery, exactly as
      Cercarbono's was.

      **BioCarbon publishes through Global CarbonTrace**
      (`globalcarbontrace.io`), a Laravel API behind a Vue SPA;
      `biocarbonregistry.com` no longer resolves. Contract in
      `docs/api-contract-biocarbon.md`, adapter in `registries/biocarbon/`.

      **105 projects, 626 issuance blocks, 11,439 retirements and 3
      cancellation records in ~225 requests.** Both credit totals match the
      registry's own published `impact-stats` figures **to the unit** —
      85,177,570 issued and 50,157,520 retired — which is what makes them
      trustworthy rather than merely self-consistent.

      What the plan expected and what was there:

      - **The double count was real and the user's call settled it.**
        Cercarbono's `CDC-106`/`CDC-107` are BioCarbon's
        `BCR-CO-319-14-002`/`-005`, still `Registered` here. The credits are
        *different tranches*, not the same units twice: 3,945,085 and
        8,029,639 here against Cercarbono's 79,450 and 170,550. **Both rows
        ship, cross-linked** (user's decision, 2026-08-04) —
        `extra.also_registered_as`, because the linkage is published only
        from Cercarbono's side. This is the only known overlap between any
        two registries in the database.
      - **A 403 wearing an HTTP 200.** Without the public `x-api-key` the
        body reads `{"status": 403, "data": []}` under a 200 status line —
        indistinguishable from an empty registry, the fourth registry here
        to report a refusal this way. The key ships in the site's own
        `assets/api-*.js`, like Verra's `appkey`.
      - **`per_page` is honoured**, verified at 100 through 5000. The *first*
        registry in this project that does not silently clamp or ignore a
        page size.

      Three things the plan did not anticipate:

      - **Two feeds disagree about cancellations, and the fuller one has no
        dates.** The `cancellations` endpoint publishes 3 rows / 477,859
        units; 14 issuance blocks carry a `dropouts` totalling **584,940**,
        and `amount = active + outof + dropouts` holds on every one. Rows
        from the endpoint, total from the blocks, through
        `iter_credit_totals` — the same seam as Cercarbono's
        `certificatedVerification`.
      - **`verified_reductions` is not an issued total, and this is
        Cercarbono's trap inverted.** It equals the ledger for 103 of 105
        projects; `BCR-TR-152-1-001` states 322,687 verified and has **no
        issuance blocks at all**. There the ledger was the incomplete feed,
        here it is the authoritative one — and only checking both against the
        registry's own headline figure says which.
      - **`País` arrives in two languages.** "Colombia" beside "Malasia",
        "Perú", "Panamá", "Brasil". No language switch exists; it is what was
        typed. `Continent` is safe (ISO code, 105/105), `biome.yaml` now
        carries both spellings, and the blast radius of adding them was
        **zero existing rows**. Re-running `derive` moved exactly **one**
        pre-existing value: SocialCarbon's bare-`AFOLU` project, which was
        missing a `Durabilidade` for the same reason it was missing a `Bioma`
        a week earlier
- [x] **5b Puro.earth — done, 2026-08-05.** ~~server-rendered HTML; look for a
      JSON endpoint before writing a parser~~. Neither guess held.
      `registry.puro.earth` is a **Next.js App Router app**, and the first
      target here with **no API at all**: the server renders each route and
      streams its React Server Components payload *inside the HTML*, where the
      lists sit as ordinary JSON. Contract in `docs/api-contract-puro.md`,
      adapter in `registries/puro/` (`api.py` + `flight.py`).

      **118 projects, 583 issuance transactions, 1,519 retirement
      transactions — three requests for all of it**, plus one detail page per
      project. ~121 requests, about four minutes. Every count reconciled on
      the first live run.

      What the plan expected and what was actually there:

      - **There is no JSON endpoint, and an hour proving it was the cost of
        not knowing.** `RSC: 1`, `?_rsc=`, and a full
        `Next-Router-State-Tree` each return the same prerendered HTML, and no
        API host appears in any of the 15 client chunks. The data is fetched
        server-side. `flight.py` reads it out of the `__next_f.push` stream —
        stdlib only, the Puro half of what `registries/tables.py` is for Markit.
        **The pushes must be joined before decoding**: the 5.2 MB retirement
        page splits mid-object.
      - **No paging, no filtering, no key, no `Origin` check.** The friendliest
        contract here after SocialCarbon's, once you stop looking for an API.

      Four things the plan did not anticipate:

      - **A transaction is not a credit record.** Each carries a `bundles`
        list and a retirement routinely draws from several production
        facilities at once: **1,519 retirements are 2,099 bundles**. A row per
        transaction would file each multi-facility retirement against
        whichever facility came first.
      - **`countryCode` is `"NA"` for Namibia**, where two projects are. Three
        adapters here carry a `NOT_STATED` table listing `na`; reusing one
        deletes a real country code and takes the Continent with it.
        `puro.NOT_STATED` is empty on purpose and a test pins it.
      - **The registry publishes no row count anywhere** — not in the body,
        not in a header, not on the site. So reconciliation is not a count at
        all: every bundle's facility must resolve to a project, every
        transaction's volume must equal its bundles', and every project's
        bundles must sum to the total its own page states. **All 118 agree
        exactly**, on both ledgers — which is also the only place the country
        *name* is published.
      - **Withdrawal is a label with no quantity.** 20 issuances carry
        `FULLY_WITHDRAWN` or `PARTIALLY_WITHDRAWN`, `withdrawalDetails` is
        null on all 2,102 transactions, and the registry's own issued total
        counts the units in full. So there is no cancellation ledger and
        `Total Credits Cancelled` is blank, not zero.

      And one thing no other registry has offered: **Puro publishes a
      durability**, in years, on every labelled bundle. Seven of its eight
      methodologies therefore have a `Durabilidade` band that is *checked*
      rather than inferred — the first time that has been possible. Wooden
      Building Elements is the exception and the one to review first. Blast
      radius of the new rules, measured against the real database before the
      change: **471 values added, 0 removed, 0 changed**, all of them Puro's
- [x] **5c ACR — done, 2026-08-04.** ~~APX ASP platform, form posts and HTML
      tables. Highest effort~~. **ACR is not on APX any more**, and that is the
      finding the rest of it followed from. `acr2.apx.com` still answers — with
      HTTP 200 and "You have reached an invalid page" for every path, including
      the old report URLs, so a scraper written against the plan would have
      found nothing and raised nothing. ACR's own public-reports page named the
      new home in a single fetch: **ICE GreenTrace**. Contract in
      `docs/api-contract-acr.md`, adapter in `registries/greentrace/` +
      `registries/acr/`.

      **994 projects, 3,359 issuance blocks, 10,725 retirements and 1,358
      cancellations**, every count reconciled against the registry's own on a
      full live sync (2026-08-05). Issued credits reconcile three ways — the
      ledger, the project list's own per-project figure (994 of 994 agree), and
      the whole holdings book, whose ACTIVE and INACTIVE parts equal issued
      minus retired minus cancelled to the unit. Coverage: crediting period
      994/994, Estado 992/994, Yearly Ex Ante 987/994, Tipo Micro 993/994,
      Durabilidade 994/994.

      What the plan expected and what was actually there:

      - **No form posts against HTML, but form posts against JSON.** The site
        is an ICE CMS app whose table component publishes its own contract in
        the page: a `reportUrl`, and a chunk that shows it being called as
        `POST {reportUrl}/results` with an
        `application/x-www-form-urlencoded` body. The same criteria as a query
        string or as JSON both return the generic HTTP 500 — as does a GET on
        the `reportUrl` itself, which is not an endpoint at all.
      - **It is a platform, the seventh.** GreenTrace serves ACR and **ART**
        (Architecture for REDD+ Transactions, 30 projects), and ART is the same
        API with one path segment changed — verified, not assumed. It has no
        adapter and is a subclass away.
      - **`max` clamps at 2000 in silence**, the fifth registry here to ignore
        or clamp a page size. Paging advances on `offset` against the stated
        `totalCount`, never on the row count that came back.

      Four things the plan did not anticipate:

      - **This registry bans rather than throttles.** A sync at the usual 1/s
        reached ~93 requests and then took HTTP 429 with `Retry-After: 3600` on
        every retry; a 20-request probe minutes later tripped the same rule.
        Roughly 100 requests per ten minutes, and a sync needs ~1,005 because
        only the per-project detail carries a crediting period. It now runs at
        one request per seven seconds — about two hours. `RegistryClient` grew
        a per-registry rate that takes the **minimum** of the adapter's and the
        global one, so it can only ever be used to go slower, and the retry
        path now honours a 429's own `Retry-After` up to a cap.
      - **One URL is four ledgers, and the unfiltered view is all of them at
        once.** `holdingStatus` selects the ledger *and* the key it answers
        under. With no status the report returns the whole holdings book —
        16,385 records summing to exactly the issued total, whose RETIRED and
        CANCELED subsets are key-identical to the two ledgers. Ingesting it
        would have restated both, which is SocialCarbon's `asset` trap again.
      - **Most of its "cancellations" are conversions.** 1,166 of 1,358 read
        "Convert to ARB Offset Credits" or "…Ecology…": the units leave for
        California's or Washington's compliance registry and go on existing.
        They are stored as the registry states them, with the reason on every
        row, and raised in `docs/field-mapping.md` — it is the first thing to
        agree with the business about this registry.
      - **It publishes no country name at all.** An ISO code in the list, the
        same code on the detail, and no country field in the report's own
        filters. `País` is blank for all 994 (user's decision, 2026-08-04);
        Continent derives from the code. `biome.yaml` gained an ISO-code band
        as its last rule so that 351 forest projects are not blank for the sake
        of a field name — blast radius against the real database measured at
        **4 rows added, 0 changed**.

      And one thing found by following this file's own rule — *ask what else
      already holds these credits* — rather than by scraping: **two ACR
      projects are also Verra projects.** `ACR0242` is `VCS 559` and
      `ACR0388`/`ACR1192` are `VCS 573`, two mine-methane projects whose
      crediting periods are consecutive rather than concurrent, so the tranches
      are disjoint and both rows ship cross-linked. That is the **second**
      known cross-registry duplicate in the database, after Cercarbono against
      BioCarbon. ACR's own `hasAnotherCarbonProgram` flag (true on 16 of 994,
      and naming no programme) is what pointed at them.

      **The refusals are the operational story.** Reaching all 994 took three
      attempts across two days: a 429 at 1/s, then a blanket
      `401 "Invalid API Key"` on every route a few hundred requests into a
      7-second-spaced run, which cleared after ~6.5 hours. The finished run
      then re-ran from the response cache in **16 seconds**, which is what
      makes "re-run it later" a real answer rather than a hopeful one
- [x] **5d SocialCarbon — done, 2026-08-04.** ~~**blocked**: serves a parked
      CDN page~~ ~~**unblocked, 2026-07-29**~~. `registry.socialcarbon.org` is
      a **Bubble.io application with an open, unauthenticated Data API**, run
      by Wilder Earth — no key, no browser `User-Agent` needed, no Cloudflare.
      Contract in `docs/api-contract-socialcarbon.md`, adapter in
      `registries/socialcarbon/`.

      **19 projects, 17 issuances, 81 retirements, 2 cancellations — four
      requests, about a minute.** The smallest and cheapest registry here, and
      every count reconciled exactly on the first live run.

      What the plan expected and what was actually there:

      - **Paging is fine, filters are fine.** Bubble validates `constraints`
        and answers a bogus field with **HTTP 404 `Field not found`** rather
        than returning the whole index — the *first* registry in this project
        that refuses a filter loudly. No partitioning is needed anyway.
      - **`limit` clamps to 100 in silence** (asked 200 of a 147-row type, got
        100 with `remaining: 47` at HTTP 200). Third registry to ignore a page
        size, so the pager advances on `remaining`.
      - **The double-count was real, and it was not only Markit.** As
        anticipated, SocialCarbon's legacy-Markit rows
        (`100000000000007`, "No Established Standard") are not ingested. What
        the plan did not anticipate: the Bubble registry's own **`asset` list
        double-counts too** — 17 of its 22 rows mirror the issuances to the
        same 189,794 units, and the other 5 read `"Standard": "VCS"` and are
        Verra credits deposited into the platform. `asset` is not scraped.

      Three things the plan did not anticipate at all:

      - **`Project ID` is not unique.** Two entirely different projects —
        Poland and Brazil — both publish `SOCIALCARBON-19`, and
        `SOCIALCARBON-15` is absent. 19 records, 18 references. This is the
        Markit merge *inverted*: there a repeated id was one project and rows
        had to be joined, here joining would fuse two countries into one row.
        The key is hashed from Bubble's own `_id`; the duplicate reference is
        published as-is and raised in `docs/field-mapping.md`.
      - **An issuance can be a request.** `Approved` and `Issuance complete`
        are the registry's own flags; all 17 rows carry both today, so
        `iter_credit_totals` states exactly what the rows sum to. It exists so
        the first pending request does not quietly inflate an issued total.
      - **Two silent derivation gaps**, both measured. `biome.yaml`'s gate
        carried `Land use \(AFOLU\)` and this registry says the bare
        **`AFOLU`**, so one project would have had no biome and nothing in the
        log; the gate is now `\bAFOLU\b`, blast radius **zero existing rows**.
        And `continent.yaml` did not carry
        **`Congo, Democratic Republic of the`** — a *third* ISO inversion,
        differing from the one already listed by a single "the".
- [x] **5e Plan Vivo V4 — done, 2026-07-29.** It was on the **legacy Markit
      Environmental Registry** (`mer.markit.com/br-reg/public`), the system
      S&P inherited with IHS Markit. 30 projects, 411 issuances, 442 holdings
      and **5,034 retirements**, all reconciled; Plan Vivo goes from 2 rows to
      32. Contract in `docs/api-contract-markit.md`, adapter in
      `registries/markit/` + `registries/planvivo/v4.py`.

      **The `standardId` was `100000000000004`** — the exact id this plan had
      recorded as "the planning guess, from a legacy Markit URL, that returns
      `totalEntities: 0`". It was never a wrong id. It was the right id for
      the wrong platform, and the S&P sweep below is what made that provable
      rather than suspected.

      What the search actually cost: `verra standards -r all` (eight
      unauthenticated GETs, and a new `-r all` mode) ruled out every S&P
      tenant in one command, and Plan Vivo's own registry page named Markit.
      No Playwright, no guessing.

      **Both eras store under one `PLAN_VIVO` registry** (user's decision).
      `standard_name` keeps it reversible: "Plan Vivo Standard V4" against
      "PV Climate".

      Five things the plan did not anticipate, each of them a measured trap:

      - **Legacy Markit is a platform, not a registry** — 21 programmes behind
        one `standardId`, including Social Carbon, ACRE, CCB and Peru REDD+.
        The adapter is written as a platform for that reason.
      - **It is HTML**, so this is the first adapter here that parses a page.
        Stdlib `html.parser`; no new dependency in a bundle the business
        downloads.
      - **The pager is useless and asking past the end is not free.** "Next →"
        is never disabled, even 200 pages past the end of a 35-row feed — and
        a past-the-end request answers HTTP 500 on the large feed while
        answering empty on the small one. Paging stops on a **short page**.
      - **35 rows are 30 projects, and some rows are not records at all.** The
        view emits `<tr>`s whose `style` attribute has swallowed a data
        payload, merges cells with `rowspan`, and repeats a project id across
        sub-projects of one master. Each of those produces plausible nonsense
        if read positionally.
      - **`name` is not a project search.** It narrows — so it passes the
        usual check — but `name=N'hambita` returns *Mikoko Pamoja* rows. Only
        checking *which* rows came back caught it.

- [x] **5f Climate Action Reserve — done, 2026-08-05.** Not on the original
      list at all. It was found by reading 5c's own finding: ACR *left*
      `acr2.apx.com`, and the platform it left is still serving somebody —
      **Xpansiv/APX**, the eighth platform here, whose largest offset tenant is
      the Climate Action Reserve. Contract in `docs/api-contract-car.md`,
      adapter in `registries/apx/` + `registries/car/`.

      **1,277 projects, 5,173 issuance rows, 11,047 retirements and 2,277
      cancellations**, every count reconciled against the registry's own
      printed total on a full live sync. Issued credits reconcile twice — the
      ledger, and the project list's own per-project figure, which agree on
      **all 901 projects that have issued anything, to the unit**. Coverage
      after the fixes below: Tipo Micro 1,277/1,277, Durabilidade 1,277/1,277,
      Estado 1,277/1,277, Data de Início 1,274/1,277.

      What the platform turned out to be:

      - **A bulk CSV export, unauthenticated.** `POST rptdownload.asp` returns
        a whole report in one request, so all three ledgers — 18,497 rows —
        cost **8 requests**. No other registry here is that cheap.
      - **And a 1,277-request fan-out anyway**, because the crediting period is
        published only on the per-project page. 99.4% of the sync is that, so
        `--projects-only` saves almost nothing — the opposite shape from every
        other registry.
      - **A CSRF token that is not in the form it belongs to**, appended by
        script a second after load, per *session* — and a POST without it
        answers HTTP 200 with the site's **home page**, which parses as an
        empty report. `APXRefused` says so rather than letting a caller read it
        as a registry with no records.
      - Sixth registry to ignore a page size; second to clamp a past-the-end
        page instead of ending; a CSV whose embedded quotes are not doubled, so
        13 retirement rows have one field too many and `csv.reader` says
        nothing.

      Three things the plan could not have anticipated, because CAR was not in
      it:

      - **It sends Windows-1252 and declares no charset anywhere.** No
        `Content-Type` charset, no `<meta>`, no BOM — so httpx assumes UTF-8
        and replaces every accent. `STATE OF MÉXICO` arrived as
        `STATE OF <?>XICO` at HTTP 200 with nothing in the log, on a registry
        that is **38% Mexican**, and it is not repairable afterwards. Fixed in
        `http_client.decoded` (declared charset → UTF-8 *strictly* → the
        platform's measured fallback) and re-run from the response cache, which
        stores raw bytes. Four fixtures had been captured through the damage
        and were repaired against a correctly decoded copy of the same source;
        a test now fails if any carries a replacement character.
      - **It is the one registry that does not write ISO dates**, and storing
        `10/7/2018` as published left `Data de Início`, `Data de Término` and
        `Duração` blank on all 1,277 rows while the sync reconciled perfectly.
        **`verra coverage` is what caught it**, which is the argument for
        running it at the end of every registry rather than at the start of the
        next one. Converted in the adapter, month-first measured rather than
        assumed, and deliberately not added to `db.DATE_FORMATS` where it would
        misread the first registry that writes day-first.
      - **A third cross-registry overlap, and the first of its kind.** The
        registry states it itself — `Offset Credits Converted to VCUs`, on 2 of
        5,173 rows — and both are Verra projects whose *entire* issued figure
        is that converted quantity. Same units, both books, nothing netted out.
        See the note above and `docs/field-mapping.md`.

      **Climate Forward** (36 projects) is the other offset tenant on this
      platform, was checked rather than assumed — identical module, forms and
      field names — and is **not** ingested: its units are ex-ante forecasts,
      which is a business decision rather than a scraping one. The remaining
      `*.apx.com` tenants are renewable-energy and fuel certificates and are
      not tCO2e

- [x] **Verra JNR — done, 2026-07-29.** Not in the original plan. The
      `standards -r all` sweep showed the Verra tenant publishes **six**
      standards and the scraper read one. JNR is tCO2e and comparable, so it
      is in (user's decision): 5 jurisdictional projects, and **all four
      ledgers empty** — blank, not zero. CCBS, SDVISTA, PWRS and S3S stay out,
      each for a stated reason; CCBS in particular is a co-certification of
      VCS projects and would double-count. See `registries/verra/jnr.py`.

      **It also found a real bug.** JNR first reported 5,247 projects
      identical to VCS: `_cache_key` hashed method, URL and body but **not
      headers**, and the S&P platform selects a standard entirely in headers.
      The second standard scraped in a run silently served the first one's
      cached responses — right shape, wrong data, nothing raised.
      `http_client.IDENTITY_HEADERS` fixes it and a test pins it. Any two
      adapters sharing a URL were exposed to this.

- [ ] ~~**5e Plan Vivo V4** — **we are only scraping V5.**~~ *(kept below for
      the reasoning that led here.)* What
      `registries/planvivo/api.py` reaches is the **PV Climate** registry, the
      Plan Vivo Standard **V5** system launched on S&P in 2025, and its 2
      projects are all of it. Plan Vivo has certified projects since 2008, and
      those **V4-and-earlier projects are on a different registry** —
      `planvivo.org/buy-credits/pv-climate-registry` is the page that names
      both. A sheet claiming to cover Plan Vivo with 2 rows understates the
      registry by an order of magnitude, and nothing in the pipeline can see
      that: the sync reconciles 2/2 exactly, because 2 is what the V5 tenant
      publishes. **This is the ignored-filter trap wearing a different hat —
      the scrape is not wrong, its scope is.**

      What is already ruled out, cheaply:

      - **Not a second standard on the same tenant.** `verra standards -r
        planvivo` returns exactly one for `PVCL`: `671000000000001` / `PV` /
        "PV Climate". If V4 were another standard behind the same registry
        code it would be in that list, with its own `standardId`, and the
        adapter would be three class attributes away.

      So the first question is which system holds it, and the S&P tenant table
      in the app bundle is where to look first — `UKLR`, `RAAS`, `OxCP`,
      `KRR`, `GCC`, `BCCR` are all unidentified from our side, and
      `GET {cmsResources}/public/standardsByRegistry/<CODE>` names each in one
      unauthenticated request. **Do that before assuming a new platform.**
      Plan Vivo's older certificates were issued through the Markit
      Environmental Registry, which S&P now owns — a lead, not a finding, and
      it needs confirming rather than believing.

      Then the usual traps apply, and two specifically:

      - **A wrong `standardId` answers HTTP 200 with `totalEntities: 0`.** If
        V4 turns out to be another S&P tenant, guessing its id looks exactly
        like the empty registry we already have.
      - **Do not merge V4 into `PLAN_VIVO`.** If it is a separate system it
        gets its own registry identifier, the same way Cercarbono's
        ex-BioCarbon projects are kept traceable — otherwise nobody can tell a
        V4 row from a V5 one, and the credit columns of two standards get
        added together. Whether the business wants them as one row set or two
        is theirs to decide, and the database should be able to answer either
        way.

Each registry brings its own `Tipo Macro` vocabulary, carried through
untranslated by design, plus a `docs/field-mapping.md` section with measured
fill rates and its deliberate blanks stated rather than filled.

---

## Phase 6 — Hardening

- [ ] Incremental sync using each registry's modified date, so the update path
      is minutes rather than hours and the shipped-database model stays viable
- [ ] `verra doctor` / GUI self-test: one call per registry reporting reachable
      / contract changed / broken — how the team learns S&P moved something,
      instead of finding out mid-delivery
- [ ] `docs/field-mapping.md` kept current; it is what the business reviews

---

## Open questions

1. ~~**Cercarbono standards**~~ — **answered 2026-07-28: CO2 only.** The
   standard slug is `settings.CERCARBONO_STANDARD`, overridable via
   `CARBON_CERCARBONO_STANDARD`, if the business ever wants the others in a
   separate run.
2. ~~**SocialCarbon** — real registry URL needed.~~ **Answered 2026-08-04:**
   `registry.socialcarbon.org`, a Bubble.io app with an open Data API. Built
   and reconciled; see 5d. What it left behind is a question for the business,
   not for the code: **two different projects publish `SOCIALCARBON-19`** and
   one reference is missing entirely, so the registry has no unique public
   reference per project. Both rows ship, told apart by name and URL.
2b. ~~**Code signing**~~ — **closed 2026-08-05, moot.** No installer is being
   sent, so there is nothing to sign or route around. Reopens only if the
   user decides to distribute a built EXE again.
3. **Shipped-database freshness** — still open, but reframed: it is no longer
   "how stale before a new installer is cut", it is how stale before someone
   regenerates `seed/carbon-seed.db` for the boss's checkout to adopt. The
   window still shows **"Data as of &lt;date&gt;"** at the top, deliberately
   the *oldest* registry rather than the newest, since a sheet is only as
   current as its stalest source — so the staleness is visible rather than
   assumed. The threshold for "time to re-scrape and re-seed" is still yours
   to decide. **Run `verra status`** before cutting a new seed database; the
   dates it prints are the dates the boss will read.

5. **Plan Vivo V4** — see 5e. Not a question about how to build something, a
   question about what the sheet currently claims: two rows are presented as
   Plan Vivo, and they are Plan Vivo *V5*. Worth deciding whether the existing
   rows should say so — `Standard` reads "PV Climate" today, which is accurate
   but easy to read as the whole registry.
4. **Plan Vivo forward credits (fPVC)** — 103,246 of Plan Vivo's 213,145 issued
   units are forward credits, issued against future sequestration and flagged
   `isVerified: false` by the platform itself. They are currently counted in
   `Total Credits Issued`, because the registry's own published figure counts
   them. If the business wants them reported separately, the class is already
   stored in `credit_events.unit_type` — a `config/credits.yaml` change, not a
   re-scrape. Raised 2026-07-28; see `docs/field-mapping.md`.

6. **Climate Action Reserve — three questions, all about the sheet rather than
   the scrape.** Raised 2026-08-05; see `docs/field-mapping.md`.

   a. **`Total Credits Cancelled` is 90% conversions.** 2,056 of its 2,277
      cancellations — 122.3M of 124.2M units — are credits leaving for
      California's ARB or Washington's Ecology programme, where they go on
      existing. ACR's identical situation was settled on 2026-08-04 as "report
      the registry's own figure, keep the reason on every row"; the same answer
      is assumed here and is worth confirming, because it is a larger share of
      a larger number.

   b. **The Mexican highlands have no Bioma.** 395 projects — Sierra Madre
      pine-oak, a temperate conifer forest — sit on "México (bioma não
      determinado)", because the only temperate band in the file is named
      *Norte-Americana*. Either that value covers them or a Sierra Madre band
      is added; both are decisions about the deliverable, and neither registry
      publishes anything finer than the state. The tropical states (90) are
      already placed.

   c. **50,000 tCO2e are counted twice**, in CAR and in Verra, because CAR
      converted them into VCUs and neither registry nets them out. Both
      published figures stand today and the link is stored. Whether the sheet
      should subtract is the business's call — as it is for the two other
      known overlaps, which do *not* have this problem.
