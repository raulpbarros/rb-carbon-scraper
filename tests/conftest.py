"""Shared test fixtures. No network anywhere in this suite.

The per-file copies of `conn`, `_load` and `FIXTURES` had drifted into six
near-identical definitions. They live here instead — not for speed (the suite
runs in seconds), but so a change to how a test database is opened reaches
every file at once.

`recording_progress` is the one that earns its keep: it asserts the contract
every adapter's `progress` callback has to honour, so a new adapter gets that
check for free instead of each file rediscovering it.
"""

from __future__ import annotations

import json

import pytest

from carbon_scraper import db, settings

FIXTURES = settings.FIXTURES_DIR


def load_fixture(name: str):
    """One captured API response, by file name."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def conn():
    """An in-memory database at the current schema."""
    connection = db.connect(":memory:")
    yield connection
    connection.close()


class RecordingProgress:
    """A progress callback that remembers what it was told.

    Both sinks read `done` as an **absolute position** against a total —
    `gui/app.py` computes `done * 100 / total`, the CLI prints
    `{done}/{total}`. An adapter reporting `1` per record therefore pins the
    bar at "1 of N" for a whole scrape and jumps to 100% at the end. One
    adapter did exactly that for its ~440-request half of Plan Vivo.
    """

    def __init__(self) -> None:
        self.seen: list[int] = []

    def __call__(self, done: int) -> None:
        self.seen.append(done)

    def assert_cumulative(self, expected: int | None = None) -> None:
        assert self.seen == list(range(1, len(self.seen) + 1)), (
            f"progress must count 1, 2, 3…; got {self.seen[:10]}"
        )
        if expected is not None:
            assert self.seen[-1] == expected


@pytest.fixture()
def progress() -> RecordingProgress:
    return RecordingProgress()
