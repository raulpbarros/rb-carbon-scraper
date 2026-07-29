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

# Kept for callers that predate the split; it means "the bundled side".
ROOT = RESOURCE_ROOT

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

REGISTRY_LABELS = {
    VERRA: "Verra VCS",
    GOLD_STANDARD: "Gold Standard",
    CERCARBONO: "Cercarbono",
    PLAN_VIVO: "Plan Vivo",
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
    VERRA: 150,
    GOLD_STANDARD: 120,
    CERCARBONO: 4,
    PLAN_VIVO: 1,
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

# --- Verra ----------------------------------------------------------------

SITE = "https://registry.verra.org"
ENVIRONMENT_CONFIG_URL = f"{SITE}/config/environment.config.json"
ENDPOINTS_CONFIG_URL = f"{SITE}/config/endpoints.json"

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

# --- public project pages -------------------------------------------------
# Used only as a fallback when an adapter did not store a `detail_url` on the
# project row. A registry missing from this map gets a blank cell, never
# another registry's URL — pointing a business user at the wrong registry's
# page is worse than pointing them nowhere.

PROJECT_DETAIL_URLS = {
    VERRA: PROJECT_DETAIL_URL,
    GOLD_STANDARD: GS_PROJECT_DETAIL_URL,
    CERCARBONO: CERCARBONO_PROJECT_DETAIL_URL,
    PLAN_VIVO: PV_PROJECT_DETAIL_URL,
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

REQUESTS_PER_SECOND = float(os.environ.get("VERRA_RPS", "1.0"))
MAX_CONCURRENCY = int(os.environ.get("VERRA_CONCURRENCY", "2"))
REQUEST_TIMEOUT = float(os.environ.get("VERRA_TIMEOUT", "60"))
MAX_RETRIES = int(os.environ.get("VERRA_RETRIES", "4"))
PAGE_SIZE = int(os.environ.get("VERRA_PAGE_SIZE", "100"))


def ensure_dirs() -> None:
    """Create the writable tree and seed the editable config into it.

    Only writable directories are created. `DOCS_DIR` and `FIXTURES_DIR` are
    repository artifacts, not runtime state, and under a frozen build they
    would land inside the temporary bundle.
    """
    for d in (DATA_DIR, OUT_DIR, CACHE_DIR, LOG_DIR, CONFIG_DIR, DERIVATION_DIR, ASSETS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    seed_user_files()
    seed_database()


def seed_user_files() -> None:
    """Copy bundled defaults into the user's config the first time only.

    Never overwrites: a user who edited `fields-asked.txt` keeps their edit
    across upgrades. A no-op in a development checkout, where the bundled and
    user paths are the same file.
    """
    for relative in SEEDED_FILES:
        source = RESOURCE_ROOT / relative
        target = USER_ROOT / relative
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
    """The user's copy if it exists, else the bundled original."""
    if user_path.exists():
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
