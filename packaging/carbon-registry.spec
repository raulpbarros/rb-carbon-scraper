# -*- mode: python ; coding: utf-8 -*-
"""One-folder PyInstaller build of the window.

    pyinstaller packaging/carbon-registry.spec --noconfirm

One folder, not one file: a `--onefile` build unpacks ~50 MB to a temp
directory on every launch, which is several seconds of nothing happening after
a double-click, and the Inno Setup installer makes the folder invisible to the
user anyway.

**What lands where is the thing to get right here**, because it is invisible in
a checkout and fatal once frozen. Everything bundled is read-only, and
`settings.RESOURCE_ROOT` resolves to `sys._MEIPASS` — a temp directory wiped
when the process exits. Nothing the program *writes* is here: the database, the
spreadsheets, the response cache and the logs all live under
`settings.USER_ROOT`, which is `%LOCALAPPDATA%\\CarbonRegistryScraper` for a
frozen build. See CLAUDE.md, "Where files live".

The layout inside the bundle must match what `settings` looks for:

    assets/fields-asked.txt      the Excel column list — the schema
    config/credits.yaml          how ledgers become the credit columns
    config/derivation/*.yaml     the classification rules
    seed/carbon-seed.db          the shipped database (see db.export_slim)

Both lists come from `packaging/bundle.py`, which derives them from
`settings.SEEDED_FILES` and `registries.ADAPTERS` rather than repeating them —
a spec that has to be edited every time a registry or a config file is added
is a spec that will eventually be forgotten.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH).resolve()))

import bundle  # noqa: E402

ROOT = bundle.ROOT

datas = bundle.datas()
if not bundle.SEED_SOURCE.exists():
    print(
        f"WARNING: no seed database at {bundle.SEED_SOURCE}. The app will open "
        f"on an empty database. Run `verra slim-db` first — build.ps1 does."
    )

a = Analysis(
    [str(ROOT / "packaging" / "carbon_gui.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=bundle.hidden_imports(),
    hookspath=[],
    runtime_hooks=[],
    excludes=bundle.EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CarbonRegistryScraper",
    debug=False,
    strip=False,
    upx=False,
    # No console. The window is the whole interface, and a business user has
    # no use for a black rectangle behind it — errors go to the log pane and
    # to LOG_DIR/gui.log instead.
    console=False,
    disable_windowed_traceback=False,
    # The taskbar and Explorer icon. Without it an unsigned EXE arrives
    # wearing PyInstaller's default, which is one more reason for the person
    # it was built for to distrust the download.
    icon=str(bundle.ICON) if bundle.ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="CarbonRegistryScraper",
)
