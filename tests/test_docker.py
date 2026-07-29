"""What the container build has to keep true. No Docker needed to run these.

Same shape as `test_packaging.py`: every failure guarded here is one that does
not raise, does not appear in a checkout, and produces a plausible-looking
result. A container that installs the package non-editably still starts and
still scrapes — it just cannot find `assets/fields-asked.txt`. A container
that bind-mounts the database still writes rows — into a file that WAL cannot
lock properly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from carbon_scraper import cli, pipeline

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    """The Dockerfile's instructions, with the comments stripped out.

    The comments explain these very traps by name, so a test that greps the
    raw text matches the warning rather than the mistake.
    """
    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((DOCKER / "compose.yaml").read_text(encoding="utf-8"))


# --- the install ----------------------------------------------------------


def test_the_package_is_installed_editable(dockerfile):
    """`RESOURCE_ROOT` is `__file__.parents[2]` — the package's neighbours.

    `assets/`, `config/`, `docs/` and `tests/fixtures/` are not package data;
    setuptools collects `src/` only. A plain `pip install .` lands the package
    in site-packages, where RESOURCE_ROOT resolves to the Python install and
    none of those exist. Nothing raises: the requested-field list comes back
    empty and the sheet is built with no columns.
    """
    installs = [line for line in dockerfile.splitlines() if "pip install" in line]
    assert installs, "the Dockerfile no longer installs the project"
    assert all("-e" in line for line in installs), installs


def test_the_diagnostic_is_not_in_the_scraping_image(dockerfile):
    """Playwright is a developer tool, exactly as in the frozen build.

    The runtime stage is what runs unattended for hours; a browser in it is
    ~400 MB of attack surface for a code path that never executes.
    """
    runtime = dockerfile.split("AS runtime", 1)[1].split("AS discovery", 1)[0]
    assert "playwright" not in runtime.lower()
    assert '[dev]' not in runtime


# --- the database ---------------------------------------------------------


def test_the_database_lives_on_a_named_volume(compose):
    """WAL over a Docker Desktop bind mount is the trap this design avoids.

    `db.py` sets `PRAGMA journal_mode=WAL`, which needs POSIX advisory locks
    and a shared-memory `-shm` mapping. Windows bind mounts (virtiofs /
    gRPC-FUSE) provide neither reliably, and the failure is not a clean error:
    it is a database that still opens.
    """
    assert "carbon-data" in compose["volumes"]
    for name, service in compose["services"].items():
        mounts = service.get("volumes", [])
        assert "carbon-data:/data" in mounts, f"{name} does not mount the volume"
        assert not any(m.startswith("../data:/data") for m in mounts), name


def test_only_the_deliverable_folders_are_bind_mounted(compose):
    """Bind mounts are fine for plain files and wrong for SQLite.

    `export` writes an .xlsx and `publish` writes a fresh file via
    `VACUUM INTO`; both are sequential writes with no locking. `discover`
    mounts the checkout because its captures belong in git. Nothing else may
    reach the host filesystem.
    """
    allowed = {"export", "publish", "discover"}
    for name, service in compose["services"].items():
        binds = [m for m in service.get("volumes", []) if m.startswith("..")]
        assert not binds or name in allowed, f"{name} bind-mounts {binds}"


# --- a refresh is not a delivery ------------------------------------------


def test_the_sync_service_does_not_write_a_spreadsheet(compose):
    """The container refreshes data. Sending a delivery stays a human act.

    If the scheduled-refresh path exported, every run would burn a version
    number and the business would receive `_v9` without anyone having decided
    to send anything — the same reason the window's Update button does not
    export.
    """
    command = compose["services"]["sync"]["command"]
    assert command[0] == "update"
    assert "export" not in command
    assert "run" not in command


def test_update_is_a_real_command_that_never_exports(monkeypatch):
    """`update` is what the container runs; `run` would have exported."""
    names = {c.name or c.callback.__name__ for c in cli.app.registered_commands}
    assert "update" in names

    calls: list[str] = []
    monkeypatch.setattr(pipeline, "sync", lambda *a, **k: calls.append("sync") or {})
    monkeypatch.setattr(pipeline, "totals", lambda *a, **k: calls.append("totals"))
    monkeypatch.setattr(pipeline, "derive_all", lambda *a, **k: calls.append("derive") or 7)
    monkeypatch.setattr(
        pipeline, "export", lambda *a, **k: pytest.fail("a refresh must not export")
    )

    assert pipeline.update_all("gs") == 7
    assert calls == ["sync", "derive"], "totals is Verra-only; see pipeline.update_all"


def test_the_exact_totals_pass_still_runs_for_verra(monkeypatch):
    """Skipping it makes Verra's credit columns silently undercount."""
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "sync", lambda *a, **k: calls.append("sync") or {})
    monkeypatch.setattr(pipeline, "totals", lambda *a, **k: calls.append("totals"))
    monkeypatch.setattr(pipeline, "derive_all", lambda *a, **k: calls.append("derive") or 0)

    pipeline.update_all("all")
    assert calls == ["sync", "totals", "derive"]


# --- the build context ----------------------------------------------------


def test_the_writable_tree_is_out_of_the_build_context():
    """A full sync leaves ~1 GB of cache and a ~215 MB database in the checkout.

    Beyond the weight, a baked copy at /app/data would be actively misleading:
    the container reads CARBON_HOME (/data), so the image's copy is never the
    one in use.
    """
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for writable in ("data/", "out/", ".cache/", "logs/", "dist/", ".git"):
        assert writable in ignored, f"{writable} is not excluded from the build context"
