# syntax=docker/dockerfile:1
#
# Two images from one file:
#
#   runtime    the scraper. No Playwright, exactly like the packaged EXE.
#   discovery  the diagnostic. Playwright plus one Chromium.
#
# Neither replaces `build.ps1`. PyInstaller needs a Windows host, so the EXE,
# the portable ZIP and the installer are still built on Windows; this only
# moves the long, headless half of the work — the scrape — off the desktop.
#
# ---------------------------------------------------------------------------
# The one thing to get right: the install has to stay EDITABLE.
#
# `settings.RESOURCE_ROOT` is `Path(__file__).resolve().parents[2]`, so the
# package has to keep sitting next to `assets/`, `config/`, `docs/` and
# `tests/`. Those directories are not package data — `[tool.setuptools.
# packages.find]` collects `src/` only — so a plain `pip install .` puts
# carbon_scraper in site-packages, where RESOURCE_ROOT resolves to the Python
# install and none of them exist. Nothing raises at build time; it surfaces as
# an empty field list or a derivation ruleset that quietly matches nothing.
# Same class of failure as a frozen build writing under `_MEIPASS`.
#
# `tests/test_docker.py` pins this.
# ---------------------------------------------------------------------------

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    # Everything the program writes goes here — database, ~1 GB response
    # cache, logs, and the user's editable copies of assets/ and config/,
    # which `seed_user_files()` lays down on first run.
    CARBON_HOME=/data

WORKDIR /app

# Dependencies first, so editing a YAML rule or a doc does not reinstall httpx.
COPY pyproject.toml /app/
COPY src /app/src
RUN --mount=type=cache,target=/root/.cache/pip pip install -e /app

COPY . /app

# Declared so a `docker run` without `-v` still gets a real filesystem rather
# than the container layer. compose mounts the named volume here.
#
# It must NOT be a bind mount from Windows: db.py opens SQLite with
# `PRAGMA journal_mode=WAL`, which needs POSIX advisory locks and a
# shared-memory `-shm` mapping, and Docker Desktop's Windows bind mounts
# provide neither reliably. See docker/README.md.
VOLUME ["/data"]

# `verra` and `carbon` are the same Typer app; `verra` is what the docs use.
ENTRYPOINT ["verra"]


# --- runtime ---------------------------------------------------------------
# The scraper. Deliberately has no browser: `discover` is a diagnostic, not
# the data path, and every registry is reached over plain httpx.

FROM base AS runtime
CMD ["status"]


# --- discovery -------------------------------------------------------------
# Re-derives an S&P contract when the operator moves something.
#
# Chromium is installed here rather than inheriting a
# mcr.microsoft.com/playwright/python image on purpose: `playwright install`
# fetches the browser build that matches the playwright wheel pip just
# resolved, so the pair cannot drift. Pinning an image tag instead means a
# `pip install` that outruns the tag fails at run time with
# "Executable doesn't exist", after the download.

FROM base AS discovery
RUN --mount=type=cache,target=/root/.cache/pip pip install -e "/app[dev]" \
 && playwright install --with-deps chromium
CMD ["standards", "-r", "verra"]
