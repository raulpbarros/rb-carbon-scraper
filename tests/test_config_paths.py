"""Editable config resolution and seeding. No network.

The resource/user split is invisible in a development checkout, which is
exactly what makes it easy to break. These pin the two ways an installed or
overridden layout could end up reading an empty ruleset directory and
delivering a sheet with the right shape and four blank columns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carbon_scraper import derive, settings


# -- an empty directory is not "the user's copy" ---------------------------


def test_an_empty_derivation_dir_loses_to_the_bundled_one(tmp_path, monkeypatch):
    """`ensure_dirs()` creates the directory before anything is seeded into it.

    A bare `exists()` check therefore hands `load_rulesets` an empty
    directory: no rules, no error, and Tipo Micro / Bioma / Durabilidade /
    Continent all blank in the delivered sheet.
    """
    empty = tmp_path / "derivation"
    empty.mkdir()
    monkeypatch.setattr(settings, "DERIVATION_DIR", empty)

    assert settings.derivation_dir() == settings.RESOURCE_ROOT / "config" / "derivation"
    assert derive.load_rulesets(settings.derivation_dir())


def test_a_populated_derivation_dir_still_wins(tmp_path, monkeypatch):
    """"Edit the file, not the code" has to keep working for an installed user."""
    mine = tmp_path / "derivation"
    mine.mkdir()
    (mine / "biome.yaml").write_text(
        "column: Bioma\nrules:\n  - name: t\n    value: Cerrado\n"
        "    match:\n      - field: country_name\n        equals: Brazil\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DERIVATION_DIR", mine)

    assert settings.derivation_dir() == mine
    rulesets = derive.load_rulesets()
    assert [r.column for r in rulesets] == ["Bioma"]


def test_a_missing_file_falls_back_to_the_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FIELDS_FILE", tmp_path / "fields-asked.txt")
    assert settings.fields_file() == settings.RESOURCE_ROOT / "assets" / "fields-asked.txt"


# -- no rules at all is an error, not a quiet run --------------------------


def test_load_rulesets_refuses_an_empty_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="No derivation rulesets"):
        derive.load_rulesets(tmp_path)


# -- seeding follows the overrides -----------------------------------------


def test_seeding_targets_the_overridden_config_dir(tmp_path, monkeypatch):
    """Seeding into `USER_ROOT` while every reader looks at `CARBON_CONFIG_DIR`
    leaves the directory the readers use permanently empty."""
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(settings, "ASSETS_DIR", tmp_path / "assets")

    assert settings._seed_target(Path("config") / "derivation" / "biome.yaml") == (
        tmp_path / "cfg" / "derivation" / "biome.yaml"
    )
    assert settings._seed_target(Path("config") / "credits.yaml") == (
        tmp_path / "cfg" / "credits.yaml"
    )
    assert settings._seed_target(Path("assets") / "fields-asked.txt") == (
        tmp_path / "assets" / "fields-asked.txt"
    )


def test_seeding_writes_where_the_readers_look(tmp_path, monkeypatch):
    config = tmp_path / "cfg"
    assets = tmp_path / "assets"
    monkeypatch.setattr(settings, "CONFIG_DIR", config)
    monkeypatch.setattr(settings, "ASSETS_DIR", assets)
    monkeypatch.setattr(settings, "DERIVATION_DIR", config / "derivation")
    monkeypatch.setattr(settings, "FIELDS_FILE", assets / "fields-asked.txt")
    monkeypatch.setattr(settings, "CREDITS_CONFIG", config / "credits.yaml")

    settings.seed_user_files()

    assert (config / "derivation" / "biome.yaml").is_file()
    assert (assets / "fields-asked.txt").is_file()
    assert settings.derivation_dir() == config / "derivation"
    assert derive.load_rulesets()
