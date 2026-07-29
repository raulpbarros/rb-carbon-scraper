"""What goes into the frozen bundle, derived rather than listed.

Imported by `carbon-registry.spec`. Separate from it so it can be tested: a
`.spec` only runs inside PyInstaller, and the two things below are exactly the
kind that fail silently.

* A **missing data file** is invisible until a business user presses a button.
  `settings.SEEDED_FILES` already names every editable file the program copies
  into `USER_ROOT` on first run, so the bundle is built from that list instead
  of a second copy of it that can drift.
* A **missing adapter** is worse. The adapters are imported lazily by
  `registries.ADAPTERS`, so PyInstaller's static analysis has to be told about
  them. Left out, the registry's checkbox still appears — the window builds
  those from `REGISTRY_LABELS` — and pressing Update raises ModuleNotFoundError
  on a machine with no console to read it. Asking `registries` which modules
  those are means adding a registry cannot forget this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The spec runs outside an installed environment; make the source importable.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from carbon_scraper import registries, settings  # noqa: E402

#: Where `verra slim-db` writes the database the installer carries, and where
#: it lands inside the bundle. Must match `settings.SEED_DB`.
SEED_SOURCE = ROOT / "dist" / "seed" / "carbon-seed.db"
SEED_TARGET = "seed"


def datas(*, seed: Path | None = None) -> list[tuple[str, str]]:
    """`(source, destination_folder)` pairs for PyInstaller's `datas`.

    The editable config, plus the shipped database when one has been built.
    The database is optional: without it the EXE opens on an empty database,
    which is a state the window already knows how to show. Silently shipping a
    *stale* one would be the worse failure, so `build.ps1` rebuilds it every
    time rather than reusing whatever is on disk.
    """
    seed = SEED_SOURCE if seed is None else seed
    pairs = [
        (str(ROOT / relative), str(relative.parent))
        for relative in settings.SEEDED_FILES
    ]
    if seed.exists():
        pairs.append((str(seed), SEED_TARGET))
    return pairs


def hidden_imports() -> list[str]:
    """Every registry adapter module, asked of the dispatch table."""
    return sorted(
        registries.adapter_class(name).__module__ for name in registries.ALL
    )


# Weight, and nothing else. Playwright is a developer diagnostic and
# `discover` is deliberately not shipped; the rest are pulled into the dev
# environment by other things and imported nowhere here.
EXCLUDES = [
    "playwright",
    "pytest",
    "_pytest",
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PIL",
    "IPython",
    "setuptools",
    "pip",
]
