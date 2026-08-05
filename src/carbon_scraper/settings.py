"""Paths, endpoints and knobs — shared across every registry.

Registry-specific constants live in the clearly-labelled sections below and
are consumed only by that registry's adapter in `registries/`.

Verra's base URIs are NOT hardcoded: the app publishes its own routing table
at `/config/environment.config.json`, so we read it at runtime and follow S&P
wherever they move the backend. Gold Standard's API is a plain fixed host and
needs no such indirection. See CLAUDE.md for why this matters.

Paths come in two flavours and must never be confused, because getting this
wrong is invisible in a checkout and fatal in a packaged build:

* **`RESOURCE_ROOT`** — read-only files shipped with the program (`assets/`,
  default `config/`). Under PyInstaller this is `sys._MEIPASS`, a temporary
  directory that is wiped when the process exits. **Nothing may ever be
  written under it.**
* **`USER_ROOT`** — everything the program writes: the database, the Excel
  deliveries, the response cache, logs, and the user's own copies of the
  editable config. Under a frozen build this is `%LOCALAPPDATA%\\<app>`.

In a development checkout both are the repository root, so the layout on disk
is exactly what it has always been.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "CarbonRegistryScraper"


def _resource_root() -> Path:
    """Where read-only bundled files live."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parents[2]


def _user_root() -> Path:
    """Where everything we write lives."""
    override = os.environ.get("CARBON_HOME")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME
    return Path(__file__).resolve().parents[2]


def _dir(env_var: str, default: Path) -> Path:
    value = os.environ.get(env_var)
    return Path(value) if value else default


RESOURCE_ROOT = _resource_root()
USER_ROOT = _user_root()

# --- read-only, shipped ---------------------------------------------------

BUNDLED_ASSETS_DIR = RESOURCE_ROOT / "assets"
BUNDLED_CONFIG_DIR = RESOURCE_ROOT / "config"
DOCS_DIR = RESOURCE_ROOT / "docs"
FIXTURES_DIR = RESOURCE_ROOT / "tests" / "fixtures"

# The database the installer carries, so that `Export Excel` works on a machine
# that has never scraped anything. Written by `verra slim-db`; see
# db.export_slim. Absent in a development checkout, where it would be a stale
# copy of the real thing sitting next to it.
SEED_DB = RESOURCE_ROOT / "seed" / "carbon-seed.db"

# --- writable -------------------------------------------------------------

ASSETS_DIR = _dir("CARBON_ASSETS_DIR", USER_ROOT / "assets")
CONFIG_DIR = _dir("CARBON_CONFIG_DIR", USER_ROOT / "config")
DERIVATION_DIR = CONFIG_DIR / "derivation"
DATA_DIR = _dir("CARBON_DATA_DIR", USER_ROOT / "data")
OUT_DIR = _dir("CARBON_OUT_DIR", USER_ROOT / "out")
CACHE_DIR = _dir("CARBON_CACHE_DIR", USER_ROOT / ".cache")
LOG_DIR = _dir("CARBON_LOG_DIR", USER_ROOT / "logs")

FIELDS_FILE = ASSETS_DIR / "fields-asked.txt"
CREDITS_CONFIG = CONFIG_DIR / "credits.yaml"
API_CONTRACT_FILE = DOCS_DIR / "api-contract.md"
API_CONTRACT_JSON = CONFIG_DIR / "api-contract.json"

# Files seeded from the bundle into the user's config on first run, so an
# installed user can still follow CLAUDE.md's rule and edit the file rather
# than the code. Source is relative to RESOURCE_ROOT, target to USER_ROOT.
SEEDED_FILES = (
    Path("assets") / "fields-asked.txt",
    Path("config") / "credits.yaml",
    Path("config") / "derivation" / "biome.yaml",
    Path("config") / "derivation" / "continent.yaml",
    Path("config") / "derivation" / "durability.yaml",
    Path("config") / "derivation" / "project_type.yaml",
)

# `verra.db` kept as the default name so existing databases keep working; it
# now holds every registry, keyed by the `registry` column.
DB_PATH = Path(os.environ.get("CARBON_DB") or os.environ.get("VERRA_DB") or DATA_DIR / "verra.db")

# Base name for the deliverable. The file actually written is a new version
# every time — `carbon-projects_v1.xlsx`, `_v2`, ... — so a delivery already
# sent to the business is never overwritten. See excel.next_version_path().
XLSX_BASE = Path(
    os.environ.get("CARBON_XLSX") or os.environ.get("VERRA_XLSX") or OUT_DIR / "carbon-projects.xlsx"
)

# --- registry identifiers -------------------------------------------------
# Stored verbatim in `projects.registry`; also the accepted `--registry` values.

VERRA = "VERRA"
GOLD_STANDARD = "GOLD_STANDARD"
CERCARBONO = "CERCARBONO"
PLAN_VIVO = "PLAN_VIVO"
SOCIAL_CARBON = "SOCIAL_CARBON"
BIOCARBON = "BIOCARBON"
PURO = "PURO"
ACR = "ACR"

REGISTRY_LABELS = {
    VERRA: "Verra VCS",
    GOLD_STANDARD: "Gold Standard",
    CERCARBONO: "Cercarbono",
    PLAN_VIVO: "Plan Vivo",
    SOCIAL_CARBON: "SocialCarbon",
    BIOCARBON: "BioCarbon",
    PURO: "Puro.earth",
    ACR: "American Carbon Registry",
}

# Roughly how long a full sync of each registry takes, in minutes, measured on
# a cold cache at the default ~1 req/s. Shown before the GUI starts a scrape,
# so "this takes hours" is a number the user sees rather than a surprise.
#
# **Deliberately a static table rather than the durations in the `runs` table.**
# A repeat run reads most of its responses from the ~1 GB on-disk cache and
# finishes in minutes, so measured history predicts a cold scrape very badly —
# it would tell a user that Gold Standard takes four minutes and then take two
# hours. Past durations are reported as history, never as a forecast.
#
# A registry missing from this table makes the estimate unknown, and the GUI
# says so. It does not guess, and it does not silently omit the registry from
# the total.
SYNC_ESTIMATE_MINUTES = {
    # VCS, plus JNR on the same tenant.
    VERRA: 155,
    GOLD_STANDARD: 120,
    CERCARBONO: 4,
    # Two systems, and the second is the expensive one. V5 on S&P is 7
    # requests; V4 on the legacy Markit view is ~440, because that view pages
    # 15 rows at a time and ignores any `limit` asked of it — 5,034
    # retirements alone are 336 pages. Measured 2026-07-29 on a cold cache.
    PLAN_VIVO: 10,
    # Four requests for the whole registry — 19 projects and three ledgers,
    # each of which fits in one page. Measured 2026-08-04. Rounded up to a
    # minute because zero would read as "nothing will happen".
    SOCIAL_CARBON: 1,
    # ~14 for the four bulk feeds (the retirement ledger is 11,439 rows at 1000
    # a page) plus one detail and one cancellation request per project — 105
    # each. ~225 requests in total. Measured 2026-08-04.
    BIOCARBON: 5,
    # Three pages carry the entire registry — projects, issuances and
    # retirements — and then one detail page per project, 118 of them. ~121
    # requests, but the pages are large: 5.2 MB for the retirement feed and
    # ~900 KB per detail page, about 120 MB in all. Measured 2026-08-05.
    PURO: 4,
    # Four ledger requests carry every credit record (the retirement feed is
    # 10,724 rows at 2000 a page) and one request carries all 994 projects —
    # but the crediting period, the state and the ex-ante estimate are only on
    # the per-project detail, so that is 994 more. ~1,005 requests, and about
    # 100 MB: a retirement page is ~16 MB because every row carries its CORSIA
    # and SDG label objects.
    #
    # Two hours for that, not seventeen minutes, because this registry is
    # behind a Cloudflare rate-limiting rule and is scraped at one request per
    # seven seconds. See ACR_REQUESTS_PER_SECOND. Measured 2026-08-04.
    ACR: 120,
}

# The exact-totals pass, on top of Verra's sync. One request per project with
# credits, and the reason Verra's credit columns are right; see pipeline.totals.
VERRA_TOTALS_ESTIMATE_MINUTES = 175

# --- S&P Global "Carbon Registry" (Platts) --------------------------------
# A platform, not a registry. Verra and Plan Vivo are both served from it, as
# are UKLR, RAAS, OxCP, KRR, GCC and BCCR. Adding one of those is a change of
# three header values — see registries/platts/api.py.

# Only hosts we are willing to record or call.
API_HOST = "prod-us.api.platts.com"
PLATTS_BASE = f"https://{API_HOST}/ci-raas-prod"

# The tenants the multi-registry app declares in its own `app.js`. These are
# platform codes, not registries we support: several are almost certainly not
# carbon registries at all, and none of them has an adapter. They are listed
# so `verra standards -r all` can name each in one unauthenticated GET, which
# is the cheapest way to find out whether a registry we want is already on a
# platform we already speak.
#
# `BCCR` is the BC Carbon Registry (British Columbia), NOT BioCarbon —
# established by this lookup in Phase 2, after the plan assumed otherwise.
PLATTS_TENANT_CODES = ("VERRA", "UKLR", "RAAS", "PVCL", "OxCP", "KRR", "GCC", "BCCR")

# --- Verra ----------------------------------------------------------------

SITE = "https://registry.verra.org"
ENVIRONMENT_CONFIG_URL = f"{SITE}/config/environment.config.json"

# Page the `discover` command drives to observe real API traffic.
# Use the public program page: `/app/search/...` redirects anonymous visitors
# to the landing page and fires no search calls.
PUBLIC_SEARCH_URL = f"{SITE}/verra/public/program/VCS"
PROJECT_DETAIL_URL = f"{SITE}/app/projectDetail/VCS/{{project_id}}"

# The VCS standard's internal id, sent as the `standardid` header. Confirmed
# against {cmsResources}/public/standardsByRegistry/VERRA.
VERRA_STANDARD_ID = "150000000000001"

# Fallback if the live config fetch fails. Observed 2026-07-27.
FALLBACK_URIS = {
    "baseUri": "https://prod-us.api.platts.com/ci-raas-prod",
    "raasUri": "https://prod-us.api.platts.com/ci-raas-prod/br-reg/rest",
    "projectManagerUri": "https://prod-us.api.platts.com/ci-raas-prod/raas-project-api",
    "raasCreditUri": "https://prod-us.api.platts.com/ci-raas-prod/raas-credit-api",
    "raasReportPublicManager": "https://prod-us.api.platts.com/ci-raas-prod/raas-report-api/es/public",
}

# Sent by the site itself to every anonymous visitor. Not a secret, and not a
# credential we obtained — it ships in the public config above. We send exactly
# what the browser sends, and never touch authenticated endpoints.
FALLBACK_HEADERS = {
    "appkey": "wOKHFGuxKApQaujPSKgF",
    "X-XSRF-TOKEN": "t20",
    "application": "Markit",
    "Cookie": "XSRF-TOKEN=t20",
}

PROGRAM = "VCS"

# The VERRA tenant publishes six standards; two are ingested. See
# registries/verra/jnr.py for why the other four are not, which is a decision
# and not an omission. Read from the platform's own standards lookup
# (`verra standards -r verra`), never guessed.
VERRA_JNR_STANDARD_ID = "150000000000002"
VERRA_JNR_ACRONYM = "JNR"

# --- Plan Vivo ------------------------------------------------------------
# Same platform, same backend, same request shape as Verra. Only the three
# identity headers differ. Measured 2026-07-28; see
# docs/api-contract-planvivo.md.
#
# The `standardId` was read from the platform's own public standards lookup,
# not guessed: a wrong-but-plausible one answers HTTP 200 with `totalEntities:
# 0`, which is indistinguishable from an empty registry.

PV_SITE = "https://registry.spglobal.com"
PV_ENVIRONMENT_CONFIG_URL = f"{PV_SITE}/config/environment.config.json"

# The multi-registry app routes as /<context>/public/<standardParam>.
PV_CONTEXT = "pvclimate"
PV_STANDARD_PARAM = "pv"
PV_PUBLIC_SEARCH_URL = f"{PV_SITE}/{PV_CONTEXT}/public/{PV_STANDARD_PARAM}"
PV_PROJECT_DETAIL_URL = f"{PV_PUBLIC_SEARCH_URL}/projects/{{project_id}}"

PV_REGISTRY_CODE = "PVCL"
PV_STANDARD_ID = "671000000000001"
PV_STANDARD_ACRONYM = "PV"

# --- Plan Vivo V4, on the legacy Markit registry --------------------------
# The other half of Plan Vivo, and a different platform entirely. PV Climate
# on S&P is the V5 system launched in 2025; everything certified under V4 and
# earlier is still on the Markit Environmental Registry. Measured 2026-07-29;
# see docs/api-contract-markit.md and registries/planvivo/v4.py.
#
# Read from the view's own programme dropdown, not guessed. It is the same id
# an old Markit URL suggested during planning — which returned no rows against
# S&P, because it was never an S&P id.

PV4_STANDARD_ID = "100000000000004"

# Stored in `projects.standard_name`. Both eras sit under one PLAN_VIVO
# registry (user's decision, 2026-07-29), so this is the only thing that tells
# a V4 row from a V5 one — V5 rows read "PV Climate".
PV4_STANDARD_LABEL = "Plan Vivo Standard V4"

# --- Gold Standard --------------------------------------------------------
# Plain unauthenticated REST. Measured 2026-07-28; see docs/api-contract-gs.md.

GS_SITE = "https://registry.goldstandard.org"
GS_API = "https://public-api.goldstandard.org"
GS_PROJECT_DETAIL_URL = f"{GS_SITE}/projects/details/{{project_id}}"

# Hard caps, measured. A larger `size` is rejected at the edge with a 403 that
# looks like a block but is a response-size limit. Do not raise them.
GS_PROJECT_PAGE_SIZE = int(os.environ.get("VERRA_GS_PROJECT_PAGE_SIZE", "150"))
GS_CREDIT_PAGE_SIZE = int(os.environ.get("VERRA_GS_CREDIT_PAGE_SIZE", "25"))

# --- Cercarbono -----------------------------------------------------------
# Runs on the EcoRegistry platform. Plain unauthenticated REST, but two headers
# are mandatory: without them every call answers `ERROR_401 / No autorizado`,
# which reads like a credential wall and is not one. Measured 2026-07-28; see
# docs/api-contract-cercarbono.md.

CERCARBONO_SITE = "https://registry.cercarbono.com"
CERCARBONO_API = "https://api-front.ecoregistry.io/platform"
CERCARBONO_PROJECT_DETAIL_URL = f"{CERCARBONO_SITE}/projects/{{project_id}}"

# The EcoRegistry platform hosts several Cercarbono standards
# (`cercarbono-biodiversity`, `cercarbono-circular-economy`). Only the CO2
# standard is ingested: its credits are tCO2e, and the others are not, so
# mixing them would put non-comparable units in the credit columns.
# User's decision, 2026-07-28.
CERCARBONO_STANDARD = os.environ.get("CARBON_CERCARBONO_STANDARD", "cercarbono-co2")

# Sent by the site's own bundle on every request. `platform` identifies which
# registry of the shared EcoRegistry platform is being asked; `lng` picks the
# language of the descriptive fields.
CERCARBONO_HEADERS = {
    "platform": "ecoregistry",
    "lng": os.environ.get("CARBON_CERCARBONO_LANG", "en"),
}

# --- SocialCarbon ---------------------------------------------------------
# A Bubble.io application with an open, unauthenticated Data API — no key, no
# browser User-Agent needed, no Cloudflare in front of it. By some distance the
# smallest and cheapest registry here: four requests for all of it. Measured
# 2026-08-04; see docs/api-contract-socialcarbon.md.

SOCIALCARBON_SITE = "https://registry.socialcarbon.org"
SOCIALCARBON_API = f"{SOCIALCARBON_SITE}/api/1.1"

# The Bubble app routes a project page off its own opaque record id, not off
# the human `SOCIALCARBON-N` reference. The adapter always writes a
# `detail_url`, so this template is only ever the fallback — and see the note
# on PROJECT_DETAIL_URLS below for why that fallback cannot produce a working
# link for this registry.
SOCIALCARBON_PROJECT_DETAIL_URL = f"{SOCIALCARBON_SITE}/project_details/{{project_id}}"

# Bubble's Data API clamps `limit` to 100 and says nothing about it: asking for
# 200 against a 147-row type returns 100 with `remaining: 47` at HTTP 200.
# Third registry here to silently ignore a page size (Gold Standard clamps
# projects to 150; the Markit view ignores `limit` outright), so the pager
# reads `remaining` rather than trusting it got what it asked for.
SOCIALCARBON_PAGE_SIZE = 100

# --- BioCarbon, on Global CarbonTrace -------------------------------------
# BioCarbon Registry publishes through **Global CarbonTrace**, a Laravel API
# behind a Vue SPA. Like EcoRegistry it is a platform, not a registry: the app
# declares one programme (`biocarbon`) with three categories (`gei`,
# `biodiversity`, `water`), and only `gei` is tCO2e. Measured 2026-08-04; see
# docs/api-contract-biocarbon.md.

BIOCARBON_SITE = "https://globalcarbontrace.io"
BIOCARBON_API = "https://api.globalcarbontrace.io"

# Shipped in the site's own `assets/api-*.js` bundle and sent on every request
# the anonymous public registry makes — the same posture as Verra's `appkey`.
# Without it the API answers `{"status": 403, ...}` **at HTTP 200**, which is
# the EcoRegistry trap exactly: the status code cannot see the refusal.
# Overridable in case the site rotates it, so a rotation is a config change
# rather than a release.
BIOCARBON_API_KEY = os.environ.get(
    "CARBON_BIOCARBON_KEY", "SboCiHaHxtC2xRM92hpBjy1S2Y5La7IwjeB76z"
)

# The GHG programme. `/api/public/*` and `/api/ghg/*` are the same data —
# identical totals on all four resources — but the per-project routes exist
# only under `ghg`, so the adapter speaks `ghg` throughout and never has to
# explain why two prefixes appear.
BIOCARBON_PROGRAM = "ghg"

# `per_page` is honoured up to at least 5000 with no clamp and no marker: this
# is the first registry here that does *not* silently ignore a page size.
# Verified 100/200/500/1000/2000/5000 all return exactly what was asked for.
# 1000 is politeness, not a limit — 11,439 retirements in 12 requests.
BIOCARBON_PAGE_SIZE = 1000

# The SPA's own route: /registry/:program/:category/project/:id, keyed on the
# numeric initiative id, which is what `project_id` stores.
BIOCARBON_PROJECT_DETAIL_URL = (
    f"{BIOCARBON_SITE}/registry/biocarbon/gei/project/{{project_id}}"
)

# --- Puro.earth -----------------------------------------------------------
# A Next.js App Router site, and the first registry here whose data arrives
# **inside the HTML** rather than from an API the page calls. The server
# renders each route and streams the React Server Component payload to the
# browser in `self.__next_f.push([1, "<json string>"])` calls; the project and
# transaction lists are plain JSON objects inside it. There is no XHR to
# intercept and no JSON route to ask for — `RSC: 1`, `?_rsc=`, and a full
# router-state header were each tried and all three return the same prerendered
# HTML. Measured 2026-08-05; see docs/api-contract-puro.md.

PURO_SITE = "https://registry.puro.earth"

#: Route -> what it carries. Three requests are the whole registry: 118
#: projects, 583 issuance transactions and 1,519 retirement transactions, with
#: no paging anywhere. `/issuances` and `/retirements` are two tabs of one
#: view, and each returns only its own transaction type.
PURO_PROJECTS_PATH = "/projects"
PURO_ISSUANCES_PATH = "/issuances"
PURO_RETIREMENTS_PATH = "/retirements"

#: The public page for one project, and the only place three things are
#: published: the country **name** (the list carries an ISO code and nothing
#: else), and the registry's own `Issued credits` / `Retired credits` totals,
#: which are the only counts this registry states anywhere.
PURO_PROJECT_DETAIL_URL = f"{PURO_SITE}/projects/{{project_id}}"

#: Asserted by the adapter. The registry names its rule set per project
#: (`Puro Standard General Rules Version 3.1`, thirteen versions across 118
#: projects) and never names the standard itself; the version goes to `extra`.
PURO_STANDARD_NAME = "Puro Standard"

# --- ACR, on ICE GreenTrace -----------------------------------------------
# The American Carbon Registry left the APX ASP platform: `acr2.apx.com` now
# answers "You have reached an invalid page" at HTTP 200 for every path, and
# ACR's own site links to **ICE GreenTrace** instead. Measured 2026-08-04; see
# docs/api-contract-acr.md.
#
# **GreenTrace is a platform, not a registry**, the fourth one here. Its home
# page offers two: ACR and ART (Architecture for REDD+ Transactions, 30
# projects), served by the identical API with one path segment changed. That is
# why the code lives in `registries/greentrace/` and `registries/acr/` is
# identity only — the same shape as platts/verra and markit/planvivo.

GREENTRACE_SITE = "https://greentrace.ice.com"
GREENTRACE_API = f"{GREENTRACE_SITE}/api/greentraceservice/v1"

# The registry keys the platform publishes. Only ACR is ingested; ART is named
# because "check the platform table first" is the cheapest question to answer,
# and this is where the answer for GreenTrace lives.
ACR_REGISTRY_KEY = "ACR_REGISTRY"
ART_REGISTRY_KEY = "ART_REGISTRY"

# The site is an ICE CMS app whose report component is configured with a
# `reportUrl` and calls `{reportUrl}/results`. **The reportUrl itself is not an
# endpoint**: a GET on it returns HTTP 500 `No static resource …`, which reads
# like a server fault and is a path that was never mapped.
GREENTRACE_REPORT_RESULTS = "results"
GREENTRACE_REPORT_CRITERIA = "criteria"

# `max` is honoured up to 2000 and silently clamped above it: asking 20000 of a
# 3,358-row ledger returns 2000 rows with `totalCount: 3358` at HTTP 200. Fifth
# registry here to clamp a page size without saying so, so the pager advances on
# `offset` against `totalCount` and never on the row count it got back.
GREENTRACE_PAGE_SIZE = 2000

# **Keyed on the project *key* (`P2423FTH4Z22`), not on the reference id.**
# `…/project/ACR1275` returns HTTP 200 and a CMS shell that renders an error,
# while the API behind it 404s — so the wrong id here produces a link that
# looks fine in the sheet and is broken when a business user clicks it. The
# adapter writes `detail_url` on every row from the key, exactly as
# SocialCarbon's does; this template is only ever reached if it did not.
ACR_PROJECT_DETAIL_URL = (
    f"{GREENTRACE_SITE}/acr/projects/registry/{ACR_REGISTRY_KEY}/project/{{project_id}}"
)

# Asserted by the adapter. Every project here is an ACR project; what the
# registry publishes per project is a `creditingProgram` (ACR, California Air
# Resources Board, Washington Department of Ecology), which is the compliance
# programme the credits serve and not the standard they were certified under.
# That goes to `extra`.
ACR_STANDARD_NAME = "American Carbon Registry"

# --- public project pages -------------------------------------------------
# Used only as a fallback when an adapter did not store a `detail_url` on the
# project row. A registry missing from this map gets a blank cell, never
# another registry's URL — pointing a business user at the wrong registry's
# page is worse than pointing them nowhere.
#
# SocialCarbon is the one entry that cannot actually serve as a fallback: its
# `project_id` is a hash of Bubble's record id, because the registry publishes
# a human reference that is not unique, so formatting the template with it
# builds a URL that does not resolve. The adapter writes `detail_url` on every
# row and a test pins that, which is what keeps the fallback unreachable.

PROJECT_DETAIL_URLS = {
    VERRA: PROJECT_DETAIL_URL,
    GOLD_STANDARD: GS_PROJECT_DETAIL_URL,
    CERCARBONO: CERCARBONO_PROJECT_DETAIL_URL,
    PLAN_VIVO: PV_PROJECT_DETAIL_URL,
    SOCIAL_CARBON: SOCIALCARBON_PROJECT_DETAIL_URL,
    BIOCARBON: BIOCARBON_PROJECT_DETAIL_URL,
    PURO: PURO_PROJECT_DETAIL_URL,
    ACR: ACR_PROJECT_DETAIL_URL,
}

# --- shared HTTP ----------------------------------------------------------

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": SITE,
    "Referer": f"{SITE}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# --- politeness -----------------------------------------------------------
# Do not raise these to "make it faster". See CLAUDE.md.

# Politeness. There is deliberately no concurrency knob: every registry here
# is a public service being read for free, and each adapter's page size is a
# measured property of its API (`platts.MAX_LIMIT`, `goldstandard.PAGE_SIZES`,
# `markit.PAGE_SIZE`), not something a caller may raise.
REQUESTS_PER_SECOND = float(os.environ.get("VERRA_RPS", "1.0"))
REQUEST_TIMEOUT = float(os.environ.get("VERRA_TIMEOUT", "60"))
MAX_RETRIES = int(os.environ.get("VERRA_RETRIES", "4"))

# The longest a 429's own `Retry-After` will be honoured before the attempt is
# given up on. Cloudflare's rate-limit response asks for 3600 seconds, and an
# hour asleep inside a GUI run is indistinguishable from a hang — the run
# fails instead, and re-running it resumes from the response cache because
# every database write is an idempotent upsert.
MAX_RETRY_AFTER = float(os.environ.get("VERRA_MAX_RETRY_AFTER", "120"))

# Per-registry politeness, for the registries that ask for less than 1/s. An
# adapter may only ever go **slower** than REQUESTS_PER_SECOND: RegistryClient
# takes the minimum of the two, so this cannot be used to speed a scrape up.
#
# ACR is behind a Cloudflare rate-limiting rule, measured 2026-08-04: ~100
# requests inside ten minutes earn a 429 with `Retry-After: 3600`, and a sync
# needs ~1,005 because the crediting period is only on the per-project detail.
# 1 request per 7 seconds keeps a full sweep under that rule.
ACR_REQUESTS_PER_SECOND = float(os.environ.get("CARBON_ACR_RPS", "0.14"))


#: Set once `ensure_dirs()` has done its work. Three modules call it — `db`,
#: `http_client` and `excel` — so a read-only command (`export`, `coverage`,
#: `status`) was doing seven mkdirs, six `resolve()` pairs and a seed check
#: several times over. Reset by `reset_prepared()`.
_prepared = False


def ensure_dirs() -> None:
    """Create the writable tree and seed the editable config into it.

    Only writable directories are created. `DOCS_DIR` and `FIXTURES_DIR` are
    repository artifacts, not runtime state, and under a frozen build they
    would land inside the temporary bundle.

    Once per process: nothing here is idempotent-but-cheap, and every caller
    is on a hot path.
    """
    global _prepared
    if _prepared:
        return
    for d in (DATA_DIR, OUT_DIR, CACHE_DIR, LOG_DIR, CONFIG_DIR, DERIVATION_DIR, ASSETS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    seed_user_files()
    seed_database()
    _prepared = True


def reset_prepared() -> None:
    """Make the next `ensure_dirs()` do its work again.

    For tests that repoint the directory constants after something has
    already prepared the old ones.
    """
    global _prepared
    _prepared = False


def _seed_target(relative: Path) -> Path:
    """Where a bundled default is seeded to, honouring the directory overrides.

    `USER_ROOT / relative` is wrong whenever `CARBON_CONFIG_DIR` or
    `CARBON_ASSETS_DIR` is set: the file lands in the default tree while
    every reader looks in the overridden one, which then stays empty.
    """
    head, *rest = relative.parts
    root = {"assets": ASSETS_DIR, "config": CONFIG_DIR}.get(head)
    if root is None:
        return USER_ROOT.joinpath(*relative.parts)
    return root.joinpath(*rest)


def seed_user_files() -> None:
    """Copy bundled defaults into the user's config the first time only.

    Never overwrites: a user who edited `fields-asked.txt` keeps their edit
    across upgrades. A no-op in a development checkout, where the bundled and
    user paths are the same file.
    """
    for relative in SEEDED_FILES:
        source = RESOURCE_ROOT / relative
        target = _seed_target(relative)
        if target.exists() or not source.exists():
            continue
        try:
            if source.resolve() == target.resolve():
                continue
        except OSError:  # pragma: no cover - resolve() on a missing parent
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def seed_database() -> None:
    """Copy the shipped database into place, the first time only.

    This is what makes `Export Excel` work on a machine that has never
    scraped anything: the installer carries a slimmed copy of the real
    database and the first run adopts it.

    **Only ever when there is no database at all.** Not "if it is empty", not
    "if it is older" — the user's file is the source of truth from the moment
    it exists, and an upgrade that quietly restored the installer's data over
    a scrape the business had just waited two hours for would be indetectable:
    the sheet would still build, with numbers from whenever the installer was
    cut.
    """
    if DB_PATH.exists() or not SEED_DB.exists():
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SEED_DB, DB_PATH)


def _editable(user_path: Path, relative: Path) -> Path:
    """The user's copy if it has content, else the bundled original.

    "Has content" rather than "exists", because a *directory* answers
    `exists()` the moment `ensure_dirs()` creates it — before anything has
    been seeded into it. An empty `config/derivation` winning over the
    bundled one makes `derive.load_rulesets()` return no rules at all, and
    every classified column (Tipo Micro, Bioma, Durabilidade, Continent)
    goes blank in the delivered sheet with nothing raised.
    """
    if user_path.is_dir():
        populated = any(user_path.glob("*.yaml"))
    else:
        populated = user_path.is_file()
    if populated:
        return user_path
    bundled = RESOURCE_ROOT / relative
    return bundled if bundled.exists() else user_path


def fields_file() -> Path:
    return _editable(FIELDS_FILE, Path("assets") / "fields-asked.txt")


def credits_config() -> Path:
    return _editable(CREDITS_CONFIG, Path("config") / "credits.yaml")


def derivation_dir() -> Path:
    return _editable(DERIVATION_DIR, Path("config") / "derivation")


def api_contract_paths(slug: str = "verra") -> tuple[Path, Path]:
    """(markdown, json) that `verra discover` writes for one registry.

    Verra keeps the original filenames so nothing that references them breaks.
    Every other registry gets a `-capture` suffix, which keeps a generated
    capture from ever landing on top of the hand-written contract doc
    (`docs/api-contract-planvivo.md`) — that one is what a human reads, and it
    carries the measured constraints a capture cannot see.
    """
    if slug == "verra":
        return API_CONTRACT_FILE, API_CONTRACT_JSON
    return (
        DOCS_DIR / f"api-contract-{slug}-capture.md",
        CONFIG_DIR / f"api-contract-{slug}.json",
    )


def read_requested_fields() -> list[str]:
    """Column order for the Excel export.

    `assets/fields-asked.txt` is the schema — one column per line, in order.
    Edit that file, not the code.
    """
    lines = fields_file().read_text(encoding="utf-8-sig").splitlines()
    return [line.strip() for line in lines if line.strip()]
