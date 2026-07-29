"""What the frozen build carries. No network, no PyInstaller needed.

Everything here is a structural guard rather than a behaviour check. The
failures it is aimed at share a shape: they cannot happen in a checkout, they
do not raise at build time, and they surface on a business user's machine as a
button that does nothing useful. `packaging/bundle.py` derives the lists from
the same tables the rest of the program reads, and these tests are what keeps
that derivation honest.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

from carbon_scraper import registries, settings

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


@pytest.fixture(scope="module")
def bundle():
    spec = importlib.util.spec_from_file_location(
        "carbon_packaging_bundle", PACKAGING / "bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


# -- the adapters ----------------------------------------------------------


def test_every_registry_adapter_is_bundled(bundle):
    """A lazily-imported adapter PyInstaller cannot see is not in the EXE.

    The checkbox appears regardless — the window builds those from
    REGISTRY_LABELS — so the registry looks supported right up until someone
    presses Update and gets a ModuleNotFoundError on a machine with no console
    to read it in.
    """
    hidden = bundle.hidden_imports()
    for name in registries.ALL:
        assert registries.adapter_class(name).__module__ in hidden


def test_the_adapter_list_is_derived_not_typed(bundle):
    """Adding a registry must not require editing the packaging."""
    assert len(bundle.hidden_imports()) == len(registries.ALL)


def test_the_diagnostic_is_not_shipped(bundle):
    """Playwright is a developer tool; `discover` is not part of the product."""
    assert "playwright" in bundle.EXCLUDES
    assert not any("discovery" in module for module in bundle.hidden_imports())


# -- the read-only files ---------------------------------------------------


def test_every_seeded_file_is_bundled(bundle, tmp_path):
    """First-run seeding copies out of the bundle; anything missing is a crash.

    `seed_user_files` skips a source that is not there, so a file left out of
    the build does not fail loudly — it fails when `read_requested_fields` or
    a derivation ruleset comes up empty, which reads as a data problem.
    """
    pairs = bundle.datas(seed=tmp_path / "absent.db")
    for relative in settings.SEEDED_FILES:
        source = str(ROOT / relative)
        assert (source, str(relative.parent)) in pairs
        assert Path(source).exists(), f"{relative} is listed but not in the checkout"


def test_the_bundle_layout_matches_what_settings_looks_for(bundle, tmp_path):
    """The destinations are relative to RESOURCE_ROOT; a wrong one is silent."""
    pairs = dict(bundle.datas(seed=tmp_path / "absent.db"))
    assert pairs[str(settings.BUNDLED_ASSETS_DIR / "fields-asked.txt")] == "assets"
    assert pairs[str(settings.BUNDLED_CONFIG_DIR / "credits.yaml")] == "config"


def test_the_seed_database_lands_where_settings_expects_it(bundle, tmp_path):
    seed = tmp_path / "carbon-seed.db"
    seed.write_bytes(b"")
    destination = dict(bundle.datas(seed=seed))[str(seed)]
    assert settings.SEED_DB == settings.RESOURCE_ROOT / destination / seed.name


def test_a_missing_seed_database_is_not_fatal(bundle, tmp_path):
    """No seed means an empty database, which the window can already show."""
    pairs = bundle.datas(seed=tmp_path / "absent.db")
    assert all("carbon-seed.db" not in source for source, _ in pairs)


# -- the entry point -------------------------------------------------------


def test_the_frozen_entry_point_is_the_one_that_was_tested():
    """The EXE and `carbon-gui` must run the same main(), not two copies."""
    text = (PACKAGING / "carbon_gui.py").read_text(encoding="utf-8")
    assert "from carbon_scraper.gui.app import main" in text

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "carbon-gui = \"carbon_scraper.gui.app:main\"" in pyproject


# -- what actually gets handed out -----------------------------------------


def test_the_installer_version_matches_the_project_version():
    """Two copies of the version string, pinned together.

    Inno needs its own at compile time and `build.ps1` reads pyproject's for the
    ZIP name, so a bumped release can ship `-0.3.0-portable.zip` containing an
    installer that calls itself 0.2.0 — and Windows decides upgrade-versus-second-
    copy from the Inno one. Nothing raises; the names simply stop meaning
    anything.
    """
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = version["project"]["version"]

    iss = (PACKAGING / "carbon-registry.iss").read_text(encoding="utf-8")
    match = re.search(r'#define\s+AppVersion\s+"([^"]+)"', iss)
    assert match, "carbon-registry.iss no longer defines AppVersion"
    assert match.group(1) == declared


def test_the_portable_zip_carries_the_readme():
    """The ZIP is handed to people who read nothing else.

    Step 1 of LEIA-ME.txt — unblock the archive before extracting — is what
    keeps SmartScreen out of a business user's way, and it is worthless if the
    file is not in the archive. `build.ps1` copies it in; this is the guard that
    the file exists and that the build still knows about it.
    """
    readme = PACKAGING / "LEIA-ME.txt"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "Desbloquear" in text, "the unblock step is the point of this file"

    assert "LEIA-ME.txt" in (ROOT / "build.ps1").read_text(encoding="utf-8")
