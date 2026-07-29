"""What the window remembers between runs.

Deliberately small: the ticked registries and the folder deliveries are written
to. Anything else the user can see on screen is read from the database, which
is the source of truth — caching it here would let the window disagree with it.

Stored under `USER_ROOT`, so `CARBON_HOME` moves it with everything else and a
throwaway run cannot overwrite the real user's preferences.

**A corrupt or unreadable state file is not an error.** It falls back to the
defaults, because a business user whose JSON got truncated by a bad shutdown
still needs a working window, and the cost of guessing wrong here is one
re-ticked checkbox.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings

log = logging.getLogger(__name__)

STATE_FILE = settings.USER_ROOT / "gui-state.json"


def default_out_dir() -> Path:
    """Where the spreadsheet goes when the user has not chosen a folder.

    A development checkout keeps using `out/`, so the GUI and the CLI deliver
    to the same place and a version history is not split in two. An installed
    build defaults to `Documents\\Carbon Registry` instead: `%LOCALAPPDATA%` is
    the right place for a database and a wrong place for a file the user is
    meant to find, attach to an email and send.
    """
    if getattr(sys, "frozen", False):
        return Path.home() / "Documents" / "Carbon Registry"
    return settings.OUT_DIR


def default_registries() -> list[str]:
    return list(settings.REGISTRY_LABELS)


@dataclass
class UiState:
    """The remembered window state. Always valid by construction."""

    registries: list[str] = field(default_factory=default_registries)
    out_dir: str = ""

    def out_path(self) -> Path:
        return Path(self.out_dir) if self.out_dir else default_out_dir()

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> UiState:
        target = Path(path) if path else STATE_FILE
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable %s (%s); using defaults.", target, exc)
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls(
            registries=cls._clean_registries(raw.get("registries")),
            out_dir=str(raw.get("out_dir") or ""),
        )

    def save(self, path: Path | None = None) -> None:
        target = Path(path) if path else STATE_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {"registries": self.registries, "out_dir": self.out_dir},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - a read-only profile
            # Failing to remember a preference must never fail the run that
            # produced it. The spreadsheet is the deliverable; this is not.
            log.warning("Could not save %s (%s).", target, exc)

    # -- validation --------------------------------------------------------

    @staticmethod
    def _clean_registries(value: object) -> list[str]:
        """Keep only registries that still exist, in the canonical order.

        A stored name can outlive the registry it names — an identifier is
        renamed, or an adapter is dropped. Passing that on would ask the
        pipeline to scrape something that has no adapter. An empty result
        falls back to everything rather than to nothing: a window that opens
        with no boxes ticked looks broken, and both buttons would refuse to run.
        """
        if not isinstance(value, list):
            return default_registries()
        wanted = {str(name) for name in value}
        kept = [name for name in settings.REGISTRY_LABELS if name in wanted]
        return kept or default_registries()
