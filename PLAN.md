# PLAN — from developer CLI to an installable app across 8 registries

Two things are changing at once:

- **More registries.** Verra, Gold Standard, Cercarbono and Plan Vivo are live.
  ACR, Puro.earth, BioCarbon and SocialCarbon were added during scoping, and
  Plan Vivo V4 during Phase 4 — the live Plan Vivo adapter turned out to cover
  only the V5 registry. Nine in total. Phase 5 keeps the last five
  independently droppable.
- **A different user.** The deliverable stops being a spreadsheet a developer
  mails out and becomes a Windows application the business team installs: tick
  the registries, pick a folder, press a button.

The hard part is not the GUI. It is that the pipeline assumes a writable repo
checkout at a path derived from `__file__` — false under PyInstaller and under
any non-editable install — and that a full scrape of eight registries is a
working day of wall-clock time. Phase 0 fixes the first; the shipped-database
model in Phase 4 fixes the second.

**Settled decisions** (do not re-open without a reason): ship a prebuilt
database so export is instant and scraping is an explicit opt-in; Tkinter/ttk;
PyInstaller one-folder inside an Inno Setup installer.

---

## Where things stand — 2026-07-29

| Phase | | |
|---|---|---|
| 0 — Foundation | ✅ | path split, dispatch table, `pipeline.py`, cancellation |
| 1 — Cercarbono | ✅ | 231 projects, live and reconciled |
| 2 — Plan Vivo | ✅ | Verra adapter generalised to `platts/`; 2 projects, reconciled |
| 3 — Tkinter GUI | ✅ | `carbon-gui` — checkboxes, folder picker, two buttons, Cancel |
| 4 — Packaging | ✅ | 20.8 MB shipped DB, EXE, per-user installer — 16.7 MB, no admin |
| 4b — Distribution | 🔄 | portable ZIP + GitHub release; SmartScreen answered without signing |
| 4c — Docker | ✅ | the ~16,500-request scrape, headless, off the desktop |
| **5 — More registries** | **🔄** | **5e Plan Vivo V4 ✅, Verra JNR ✅**; 5d unblocked; 5a/5b/5c open |
| 6 — Hardening | | incremental sync, `verra doctor` |

**235 tests, green, offline.** Four registries live and **six standards across
three platforms**: Verra VCS 5,245 + JNR 5, Gold Standard 4,141, Cercarbono
231, Plan Vivo V5 2 + V4 30 — **9,649 projects in one database**, every one
carrying a derived Tipo Micro / Bioma / Durabilidade. Last delivery is
`out/carbon-projects_v2.xlsx`; a `_v3` has still not been cut, because nobody
has asked for one.

**The "0 projects retiring more than they issued" invariant no longer holds,
and that is a finding rather than a regression.** Six Plan Vivo V4 projects
retire more than the registry publishes as issued — Sofala most starkly at
**0 issued against 273,836 retired**, confirmed by querying the issuance feed
directly rather than inferred from a gap. Nothing is back-computed to make the
arithmetic close. See `docs/field-mapping.md`.

**There is now an installer.** `.\build.ps1` produces
`dist/installer/CarbonRegistryScraper-0.2.0-setup.exe`, which installs per-user
with no admin rights and carries a 20.8 MB database, so `Export Excel` works on
a machine that has never scraped anything and has no Python on it.

**And a way to hand it over.** Phase 4b adds
`dist/portable/CarbonRegistryScraper-0.2.0-portable.zip` and a GitHub release
to put it on, because an unsigned installer is a build problem only until it
meets someone else's machine — after that it is a trust problem, and that one
is not fixed by building harder.

What is left is registries. Phase 5 adds them to a tool the business can
already open — deliberately after, not before. It has grown by one: the Plan
Vivo adapter reaches the **V5 (PV Climate) registry only**, and its two
projects are not the whole of Plan Vivo. See 5e.

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

---

## Phase 4 — PyInstaller, Inno Setup, shipped database ✅

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

## Phase 4b — Getting it to the business team, unsigned

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
- [ ] Cut `v0.2.0` on GitHub with the ZIP, the installer and `carbon-seed.db`
- [ ] **Verify on a machine that is not this one** — download through a
      browser, which is the only way to get a real Mark of the Web, then
      unblock → extract → run and confirm no prompt appears. Once *without*
      unblocking too, so the READ-ME describes the real dialog

**The claim this cannot check from here is antivirus.** Corporate AV
heuristically quarantines unsigned PyInstaller executables often enough that it
has to be observed rather than assumed, and unblocking does nothing about it.
If it happens, the source route stops being the alternative and becomes the
plan. Same shape as every other trap in this repo: a build that finished clean
says nothing about what happens on the other machine.

**The installer stays.** It is built, it works, and someone will want a
Start-menu entry. It is the second link in the release, not the first.

---

## Phase 5 — Remaining registries

Same recipe each time, each independently droppable: discover or bundle-grep →
contract doc → settings entries → adapter → fixtures and tests → derivation
rules. The GUI checkbox appears on its own.

**5e is not like the others.** It is not a registry nobody has looked at — it
is a registry we are already half-scraping and did not know it.

**Before writing any new adapter, check the two platform tables.**
`verra standards -r all` names every S&P tenant in eight GETs, and
`docs/api-contract-markit.md` lists the 21 programmes on legacy Markit. That
is 29 registries reachable by subclassing something that already works, and it
is how 5e turned out to be an afternoon instead of a week.

- [ ] **5a BioCarbon** — ~~first check whether `BCCR` is BioCarbon~~ **it is
      not**: the S&P standards lookup names it "BC Carbon Registry", British
      Columbia (`140000000000001` / `BC`). Settled in Phase 2. Not on legacy
      Markit either — that view's 21 programmes do not include it. So this is
      a real adapter: grep `globalcarbontrace.io`'s Vite bundle the way
      Cercarbono's was cracked.
      **Watch for double counting**: Cercarbono re-issues projects migrated
      from BioCarbon and records the origin in
      `projects.extra.converted_from` — `CDC-106` and `CDC-107` are both
      ex-BioCarbon and link back to `biocarbonregistry.com`. The same project
      appearing under two registries must not be added up twice
- [ ] **5b Puro.earth** — server-rendered HTML; look for a JSON endpoint before
      writing a parser
- [ ] **5c ACR** — APX ASP platform, form posts and HTML tables. Highest effort
- [ ] **5d SocialCarbon** — ~~**blocked**: serves a parked CDN page~~
      **unblocked, 2026-07-29.** `registry.socialcarbon.org` is not parked: it
      is a **Bubble.io application with an open, unauthenticated Data API**,
      run by Wilder Earth.

      ```
      GET https://registry.socialcarbon.org/api/1.1/meta
      GET https://registry.socialcarbon.org/api/1.1/obj/project
      ```

      `meta` lists the readable types: `project`, `issuance`, `retirement`,
      `transaction`, `cancellations`, `transfer`, `asset`, `assetlisting`,
      `document`, `vvbs`. `obj/project` already returns real project records
      as JSON with no key. That is a better contract than three of the four
      registries already live.

      Two things to establish before writing the adapter, both the usual
      shape: Bubble's Data API pages with `cursor`/`limit` and states a
      `remaining` count — **prove it narrows and reconcile against it** — and
      SocialCarbon also appears on legacy Markit (`100000000000007`) as an
      **additional certification**, where its rows read "No Established
      Standard". Those are the same credits seen from another angle, so
      **ingesting both would double-count**. The Bubble registry is the
      current system and the one to use.
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
2. **SocialCarbon** — real registry URL needed.
2b. **Code signing** — Phase 4b routes around SmartScreen rather than removing
   the cause, and that works only as long as everyone does the unblock step.
   An Authenticode certificate ends the question: roughly USD 200-400/year for
   an OV certificate, which since June 2023 must live on a hardware token
   (so it also has to be plugged into whichever machine runs `build.ps1`). An
   EV certificate additionally buys immediate SmartScreen reputation rather
   than reputation earned over downloads. Worth it if this ever ships outside
   the team; a decision about money, not about code. **A self-signed
   certificate is not a substitute** — it has to be installed into Trusted
   Publishers on every machine, which needs admin rights and is exactly the
   IT ticket the per-user install was designed to avoid.
3. **Shipped-database freshness** — how stale may the bundled data be before a
   new installer is cut? Half-answered in Phase 3: the window now shows **"Data
   as of &lt;date&gt;"** at the top, deliberately the *oldest* registry rather
   than the newest, since a sheet is only as current as its stalest source. So
   the staleness is visible rather than assumed. What is still yours to decide
   is the threshold at which a new installer gets cut.

   Phase 4 narrowed it further rather than answering it: `build.ps1` rebuilds
   the shipped database from `data/verra.db` on every run, so an installer is
   never older than the build that produced it — but nothing forces that
   database to be fresh. **Run `verra status` before `build.ps1`**; the dates
   it prints are the dates the business will read.

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
