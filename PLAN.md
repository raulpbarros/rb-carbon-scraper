# PLAN — from developer CLI to an installable app across 8 registries

Two things are changing at once:

- **More registries.** Verra, Gold Standard, Cercarbono and Plan Vivo are live.
  ACR, Puro.earth, BioCarbon and SocialCarbon were added during scoping. Eight
  in total. Phase 5 keeps the last four independently droppable.
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

## Where things stand — 2026-07-28

| Phase | | |
|---|---|---|
| 0 — Foundation | ✅ | path split, dispatch table, `pipeline.py`, cancellation |
| 1 — Cercarbono | ✅ | 231 projects, live and reconciled |
| 2 — Plan Vivo | ✅ | Verra adapter generalised to `platts/`; 2 projects, reconciled |
| 3 — Tkinter GUI | ✅ | `carbon-gui` — checkboxes, folder picker, two buttons, Cancel |
| **4 — Packaging** | **next** | slim DB, PyInstaller, Inno Setup — puts it on their machine |
| 5 — Four more registries | | BioCarbon, Puro.earth, ACR, SocialCarbon |
| 6 — Hardening | | incremental sync, `verra doctor` |

**164 tests, green, offline.** Four registries live: Verra 5,245 projects, Gold
Standard 4,141, Cercarbono 231, Plan Vivo 2 — **9,619 in one database**, every
one carrying a derived Tipo Micro / Bioma / Durabilidade and 0 projects
reporting more credits retired than issued. Last delivery is
`out/carbon-projects_v2.xlsx`; a `_v3` covering all four registries has not
been cut yet, because nobody has asked for one.

There is now a window, and it runs from a checkout. What is left before the
business team can use it is Phase 4: a database small enough to ship, an EXE,
and an installer that needs no admin rights. Phase 5 adds registries to a tool
they can already open — deliberately after, not before.

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

## Phase 4 — PyInstaller, Inno Setup, shipped database

- [ ] `db.export_slim()` + `verra slim-db`: `projects`, `credit_totals`,
      `project_derived`, `runs` only. The dev database is 225 MB, almost all of
      it `raw_snapshots` and per-record `credit_events`, neither of which the
      spreadsheet needs
- [ ] PyInstaller one-folder spec: `--add-data` for `assets/` and `config/`,
      slim DB as a seed, `--noconsole`, excluding playwright and pytest
- [ ] `installer/carbon-registry.iss`: per-user install (no admin rights),
      shortcuts, uninstaller that leaves `%LOCALAPPDATA%` data alone
- [ ] `build.ps1`: clean → pytest → slim-db → PyInstaller → Inno
- [ ] Clean Windows VM with no Python: install, export, update, uninstall

**SmartScreen:** an unsigned installer shows "Windows protected your PC" and
hides Run anyway behind *More info*. Not fixable in code — it needs an
Authenticode certificate. Document the click-through; raise signing separately.

---

## Phase 5 — Remaining registries

Same recipe each time, each independently droppable: discover or bundle-grep →
contract doc → settings entries → adapter → fixtures and tests → derivation
rules. The GUI checkbox appears on its own.

- [ ] **5a BioCarbon** — ~~first check whether `BCCR` is BioCarbon~~ **it is
      not**: the S&P standards lookup names it "BC Carbon Registry", British
      Columbia (`140000000000001` / `BC`). Settled in Phase 2. So this is a
      real adapter: grep `globalcarbontrace.io`'s Vite bundle the way
      Cercarbono's was cracked.
      **Watch for double counting**: Cercarbono re-issues projects migrated
      from BioCarbon and records the origin in
      `projects.extra.converted_from` — `CDC-106` and `CDC-107` are both
      ex-BioCarbon and link back to `biocarbonregistry.com`. The same project
      appearing under two registries must not be added up twice
- [ ] **5b Puro.earth** — server-rendered HTML; look for a JSON endpoint before
      writing a parser
- [ ] **5c ACR** — APX ASP platform, form posts and HTML tables. Highest effort
- [ ] **5d SocialCarbon** — **blocked**: `registry.socialcarbon.org` serves a
      parked CDN page, not a registry. Need the real URL

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
3. **Shipped-database freshness** — how stale may the bundled data be before a
   new installer is cut? Half-answered in Phase 3: the window now shows **"Data
   as of &lt;date&gt;"** at the top, deliberately the *oldest* registry rather
   than the newest, since a sheet is only as current as its stalest source. So
   the staleness is visible rather than assumed. What is still yours to decide
   is the threshold at which a new installer gets cut.
4. **Plan Vivo forward credits (fPVC)** — 103,246 of Plan Vivo's 213,145 issued
   units are forward credits, issued against future sequestration and flagged
   `isVerified: false` by the platform itself. They are currently counted in
   `Total Credits Issued`, because the registry's own published figure counts
   them. If the business wants them reported separately, the class is already
   stored in `credit_events.unit_type` — a `config/credits.yaml` change, not a
   re-scrape. Raised 2026-07-28; see `docs/field-mapping.md`.
