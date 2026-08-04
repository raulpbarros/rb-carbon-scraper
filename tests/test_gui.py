"""The desktop front end. No network, and no window.

Nothing here calls `tk.Tk()`. A test suite that opens a window cannot run on a
build agent, and the parts worth testing are the parts that are not widgets:
what the two buttons actually do, what the window remembers, what the worker
thread sends back, and the registry filter that lets a subset of checkboxes
mean the same thing to both buttons.

The widget layer that is left over is a wiring diagram, and is verified by
running it — see PLAN.md Phase 3.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from carbon_scraper import db, excel, pipeline, settings
from carbon_scraper.gui import app as gui_app
from carbon_scraper.gui import state as gui_state
from carbon_scraper.gui import worker as gui_worker
from carbon_scraper.http_client import Cancelled

VERRA = settings.VERRA
GS = settings.GOLD_STANDARD
CERC = settings.CERCARBONO


def add_project(conn, registry, project_id, name="A project"):
    return db.upsert_projects(
        conn,
        registry,
        [({"project_id": project_id, "project_name": name}, {"id": project_id})],
    )


# --- the registry filter --------------------------------------------------
#
# The GUI's checkboxes select registries for BOTH buttons. That only works if
# the filter below the pipeline takes a set, so these guard the widening.


def test_no_filter_returns_every_registry(conn):
    add_project(conn, VERRA, 1)
    add_project(conn, GS, 2)
    assert len(db.all_projects(conn, None)) == 2


def test_a_single_name_still_works(conn):
    """The CLI passes a string, and must be unaffected by the widening."""
    add_project(conn, VERRA, 1)
    add_project(conn, GS, 2)
    rows = db.all_projects(conn, VERRA)
    assert [r["registry"] for r in rows] == [VERRA]


def test_a_subset_returns_exactly_that_subset(conn):
    add_project(conn, VERRA, 1)
    add_project(conn, GS, 2)
    add_project(conn, CERC, 3)
    rows = db.all_projects(conn, [VERRA, CERC])
    assert sorted(r["registry"] for r in rows) == [CERC, VERRA]


def test_an_empty_selection_returns_nothing_not_everything(conn):
    """The trap this widening could easily have introduced.

    An empty list has to mean zero rows. Treating it as falsy and falling back
    to "every registry" would turn a window with no boxes ticked into a full
    export of the database: no error, plausible output, wrong scope — the same
    shape as an API filter that is silently ignored.
    """
    add_project(conn, VERRA, 1)
    add_project(conn, GS, 2)
    assert db.all_projects(conn, []) == []


def test_the_spreadsheet_honours_a_subset(conn):
    add_project(conn, VERRA, 1)
    add_project(conn, GS, 2)
    _, rows = excel.build_rows(conn, [GS])
    assert len(rows) == 1
    assert rows[0]["Registry"] == settings.REGISTRY_LABELS[GS]


def test_registry_clause_binds_parameters_rather_than_interpolating():
    """A registry name reaches SQL as a bound parameter, never as text."""
    where, params = db.registry_clause([VERRA, GS])
    assert where.count("?") == 2
    assert params == [VERRA, GS]


# --- pipeline selection ---------------------------------------------------


def test_all_means_every_registry():
    assert pipeline.selected("all") == list(settings.REGISTRY_LABELS)
    assert pipeline.only("all") is None


def test_a_sequence_is_resolved_deduplicated_and_ordered():
    picked = pipeline.selected(["gs", "verra", "GOLD_STANDARD"])
    assert picked == [VERRA, GS]


def test_only_keeps_an_empty_selection_empty():
    """`only([])` must not degrade into `None`, which means everything."""
    assert pipeline.only([]) == []
    assert pipeline.only([]) is not None


def test_a_selection_of_every_registry_is_not_silently_turned_into_none():
    """Distinct values, even where the result would be the same set.

    `all` is a statement about the future — whatever registries exist. A list
    is a statement about now. Collapsing one into the other would make a new
    registry appear in an export nobody asked to widen.
    """
    everything = list(settings.REGISTRY_LABELS)
    assert pipeline.only(everything) == everything


def test_derive_run_label_is_storable():
    """`runs.registry` takes text; a list would not bind to SQLite."""
    assert pipeline._run_label([VERRA, GS]) == f"{VERRA},{GS}"
    assert pipeline._run_label([]) == "none"
    assert pipeline._run_label("all") == "all"


# --- registry summary -----------------------------------------------------


def test_every_registry_appears_even_with_no_data(conn):
    """A registry that has never been scraped is a fact worth showing."""
    summary = db.registry_summary(conn)
    assert set(summary) == set(settings.REGISTRY_LABELS)
    assert summary[VERRA]["projects"] == 0
    assert summary[VERRA]["last_sync"] is None


def test_summary_reports_counts_and_the_last_finished_sync(conn):
    add_project(conn, VERRA, 1)
    add_project(conn, VERRA, 2)
    run_id = db.start_run(conn, "sync", VERRA)
    db.finish_run(conn, run_id, ok=True, counts={"projects": 2})

    entry = db.registry_summary(conn)[VERRA]
    assert entry["projects"] == 2
    assert entry["last_sync_ok"] is True
    assert entry["last_sync_seconds"] is not None


def test_an_unfinished_run_is_not_evidence_that_data_arrived(conn):
    """A killed process leaves finished_at NULL. That is not a sync."""
    add_project(conn, VERRA, 1)
    db.start_run(conn, "sync", VERRA)
    assert db.registry_summary(conn)[VERRA]["last_sync"] is None


def test_a_failed_sync_is_reported_as_failed(conn):
    run_id = db.start_run(conn, "sync", GS)
    db.finish_run(conn, run_id, ok=False, error="boom")
    assert db.registry_summary(conn)[GS]["last_sync_ok"] is False


def test_only_sync_runs_count_as_a_sync(conn):
    """`derive` and `totals` also write to `runs`, and are not scrapes."""
    run_id = db.start_run(conn, "derive", VERRA)
    db.finish_run(conn, run_id, ok=True)
    assert db.registry_summary(conn)[VERRA]["last_sync"] is None


# --- remembered state -----------------------------------------------------


def test_defaults_when_nothing_has_been_saved(tmp_path):
    loaded = gui_state.UiState.load(tmp_path / "absent.json")
    assert loaded.registries == list(settings.REGISTRY_LABELS)


def test_state_round_trips(tmp_path):
    path = tmp_path / "gui-state.json"
    gui_state.UiState(registries=[GS], out_dir=str(tmp_path)).save(path)
    loaded = gui_state.UiState.load(path)
    assert loaded.registries == [GS]
    assert loaded.out_path() == tmp_path


def test_a_corrupt_state_file_falls_back_instead_of_crashing(tmp_path):
    """A truncated JSON file must not stop the window from opening."""
    path = tmp_path / "gui-state.json"
    path.write_text('{"registries": [', encoding="utf-8")
    assert gui_state.UiState.load(path).registries == list(settings.REGISTRY_LABELS)


def test_a_registry_that_no_longer_exists_is_dropped(tmp_path):
    path = tmp_path / "gui-state.json"
    path.write_text(
        json.dumps({"registries": [VERRA, "RETIRED_REGISTRY"]}), encoding="utf-8"
    )
    assert gui_state.UiState.load(path).registries == [VERRA]


def test_a_selection_of_only_dead_registries_falls_back_to_all(tmp_path):
    """A window that opens with nothing ticked looks broken and does nothing."""
    path = tmp_path / "gui-state.json"
    path.write_text(json.dumps({"registries": ["GONE"]}), encoding="utf-8")
    assert gui_state.UiState.load(path).registries == list(settings.REGISTRY_LABELS)


def test_stored_registries_come_back_in_canonical_order(tmp_path):
    path = tmp_path / "gui-state.json"
    path.write_text(json.dumps({"registries": [GS, VERRA]}), encoding="utf-8")
    assert gui_state.UiState.load(path).registries == [VERRA, GS]


def test_the_default_folder_is_the_cli_folder_in_a_checkout():
    """Both front ends deliver to `out/` in development.

    Otherwise a version history would split in two, and `_v3` from the GUI
    would not know about `_v2` from the CLI.
    """
    assert gui_state.default_out_dir() == settings.OUT_DIR


# --- the worker thread ----------------------------------------------------


@pytest.fixture()
def sink_queue():
    return queue.Queue()


def drain(outbox, timeout=5.0):
    """Everything the worker sent, up to and including the Finished message."""
    messages = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = outbox.get(timeout=0.1)
        except queue.Empty:
            continue
        messages.append(message)
        if isinstance(message, gui_worker.Finished):
            return messages
    raise AssertionError(f"worker never finished; got {messages}")


def test_a_successful_task_reports_its_summary_and_result(sink_queue):
    worker = gui_worker.Worker(sink_queue)
    worker.start("t", lambda sink, cancel: ("all done", Path("sheet.xlsx")))
    finished = drain(sink_queue)[-1]
    assert finished.ok and not finished.cancelled
    assert finished.summary == "all done"
    assert finished.result == Path("sheet.xlsx")


def test_a_crash_is_captured_to_a_file_the_user_can_send(sink_queue, tmp_path, monkeypatch):
    """There is no console in a `--noconsole` build to copy a traceback from."""
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path)

    def explode(sink, cancel):
        raise RuntimeError("the registry moved")

    worker = gui_worker.Worker(sink_queue)
    worker.start("t", explode)
    finished = drain(sink_queue)[-1]

    assert not finished.ok and not finished.cancelled
    assert "the registry moved" in finished.summary
    assert finished.traceback_file is not None
    assert "RuntimeError" in finished.traceback_file.read_text(encoding="utf-8")


def test_cancelling_is_not_reported_as_a_crash(sink_queue, tmp_path, monkeypatch):
    """A user who pressed Cancel has not hit a bug."""
    monkeypatch.setattr(settings, "LOG_DIR", tmp_path)

    def stop(sink, cancel):
        raise Cancelled()

    worker = gui_worker.Worker(sink_queue)
    worker.start("t", stop)
    finished = drain(sink_queue)[-1]

    assert finished.cancelled and not finished.ok
    assert finished.traceback_file is None
    assert list(tmp_path.glob("error-*.log")) == []


def test_the_cancel_event_reaches_the_task(sink_queue):
    seen = threading.Event()

    def task(sink, cancel):
        while not cancel.is_set():
            time.sleep(0.01)
        seen.set()
        return "stopped", None

    worker = gui_worker.Worker(sink_queue)
    worker.start("t", task)
    worker.stop()
    worker.join(timeout=5)
    assert seen.is_set()


def test_only_one_task_runs_at_a_time(sink_queue):
    """Two runs would double the request rate at registries we are being polite to."""
    release = threading.Event()
    started = threading.Event()

    def first(sink, cancel):
        started.set()
        release.wait(5)
        return "first finished", None

    def second(sink, cancel):  # pragma: no cover - must never be reached
        raise AssertionError("a second task was allowed to start")

    worker = gui_worker.Worker(sink_queue)
    assert worker.start("first", first) is True
    assert started.wait(5)
    assert worker.start("second", second) is False

    release.set()
    worker.join(timeout=5)
    assert drain(sink_queue)[-1].summary == "first finished"


def test_the_sink_posts_progress_and_notes(sink_queue):
    sink = gui_worker.QueueSink(sink_queue)
    sink.message("hello")
    sink.stage(VERRA, "projects", 10, 100)
    note = sink_queue.get_nowait()
    progress = sink_queue.get_nowait()
    assert isinstance(note, gui_worker.Note) and note.text == "hello"
    assert isinstance(progress, gui_worker.Progress)
    assert (progress.done, progress.total) == (10, 100)


def test_reconciliation_warnings_reach_the_window(sink_queue):
    """`INCOMPLETE` is the difference between a short sync and a wrong one.

    A business user has no console to find it in, so it has to arrive in the
    log pane, tagged loudly enough to notice.
    """
    handler = gui_worker.QueueLogHandler(sink_queue)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(
        "carbon_scraper.registries.platts.api",
        logging.ERROR,
        __file__,
        1,
        "INCOMPLETE: expected 5,244, stored 3,973",
        None,
        None,
    )
    handler.emit(record)
    line = sink_queue.get_nowait()
    assert isinstance(line, gui_worker.LogLine)
    assert line.level >= logging.WARNING
    assert "INCOMPLETE" in line.text


# --- what the two buttons do ----------------------------------------------


class Recorder:
    """Stands in for `pipeline`, recording the calls each button makes."""

    def __init__(self):
        self.calls: list[str] = []

    def derive_all(self, names, **kwargs):
        self.calls.append("derive")
        return 7

    def export(self, names, **kwargs):
        self.calls.append("export")
        return Path("carbon-projects_v3.xlsx"), 9619, Path("carbon-projects_v2.xlsx")

    def sync(self, names, **kwargs):
        self.calls.append("sync")
        return {}

    def totals(self, **kwargs):
        self.calls.append("totals")
        return 0, 0.0, 0.0


@pytest.fixture()
def recorder(monkeypatch):
    fake = Recorder()
    for name in ("derive_all", "export", "sync", "totals"):
        monkeypatch.setattr(gui_app.pipeline, name, getattr(fake, name))
    return fake


def test_export_derives_first_so_a_rule_edit_reaches_the_sheet(recorder, tmp_path):
    task = gui_app.build_export_task([VERRA], tmp_path)
    summary, result = task(gui_worker.QueueSink(queue.Queue()), threading.Event())
    assert recorder.calls == ["derive", "export"]
    assert "9,619 rows" in summary
    assert "carbon-projects_v2.xlsx" in summary  # the previous delivery is named
    assert isinstance(result, Path)


def test_updating_the_data_never_writes_a_delivery(recorder):
    """The two buttons are separate on purpose.

    If a refresh exported, every update would burn a version number and the
    business would receive `_v9` without anyone having decided to send it.
    """
    task = gui_app.build_update_task([VERRA, GS])
    summary, result = task(gui_worker.QueueSink(queue.Queue()), threading.Event())
    assert "export" not in recorder.calls
    assert result is None
    assert "Export Excel" in summary


def test_updating_verra_runs_the_exact_totals_pass(recorder):
    """Without it Verra's credit columns silently undercount."""
    gui_app.build_update_task([VERRA])(
        gui_worker.QueueSink(queue.Queue()), threading.Event()
    )
    assert recorder.calls == ["sync", "totals", "derive"]


def test_updating_another_registry_does_not_run_verras_totals(recorder):
    gui_app.build_update_task([GS])(
        gui_worker.QueueSink(queue.Queue()), threading.Event()
    )
    assert recorder.calls == ["sync", "derive"]


# --- what the window says -------------------------------------------------


def test_age_wording():
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    assert gui_app.format_age(None) == "never synced"
    assert gui_app.format_age(now.isoformat(), now=now) == "synced today"
    assert (
        gui_app.format_age((now - timedelta(days=1)).isoformat(), now=now)
        == "synced yesterday"
    )
    assert "5 days ago" in gui_app.format_age(
        (now - timedelta(days=5)).isoformat(), now=now
    )


def test_a_registry_with_nothing_stored_says_so():
    assert gui_app.summary_line({"projects": 0}) == "nothing stored yet"


def test_a_failed_last_attempt_is_visible_beside_the_checkbox():
    line = gui_app.summary_line(
        {
            "projects": 231,
            "last_sync": "2026-07-28T09:00:00+00:00",
            "last_sync_ok": False,
        }
    )
    assert "231 projects" in line
    assert "did not finish" in line


def test_a_cancelled_first_attempt_does_not_look_like_a_fresh_install():
    """Found by running it: a cancelled sync stores no rows and records a
    failed run, and the caption said only "nothing stored yet" — indis-
    tinguishable from a registry nobody has ever tried. One of those means
    "press the button again"."""
    line = gui_app.summary_line(
        {"projects": 0, "last_sync": "2026-07-28T09:00:00+00:00", "last_sync_ok": False}
    )
    assert "nothing stored yet" in line
    assert "did not finish" in line


def test_the_estimate_includes_verras_exact_totals_pass():
    """Verra's sync is only half of what pressing Update actually does."""
    verra_only = gui_app.estimate_minutes([VERRA])
    assert verra_only == (
        settings.SYNC_ESTIMATE_MINUTES[VERRA] + settings.VERRA_TOTALS_ESTIMATE_MINUTES
    )
    assert verra_only > settings.SYNC_ESTIMATE_MINUTES[VERRA]


def test_an_unknown_registry_makes_the_estimate_unknown_not_short():
    """A total that quietly omits a registry is worse than admitting ignorance."""
    assert gui_app.estimate_minutes(["SOMETHING_NEW"]) is None
    assert gui_app.format_duration(None) == "an unknown time"


def test_every_registry_has_an_estimate():
    """A new registry with no entry would blank the estimate for every run."""
    assert set(settings.SYNC_ESTIMATE_MINUTES) >= set(settings.REGISTRY_LABELS)


def test_duration_wording():
    assert gui_app.format_duration(0.5) == "under a minute"
    assert gui_app.format_duration(4) == "about 4 minutes"
    assert gui_app.format_duration(325) == "about 5 hours"


def test_data_as_of_reports_the_oldest_registry_not_the_newest():
    """A sheet is only as current as its stalest registry."""
    summary = {
        VERRA: {"projects": 5245, "last_sync": "2026-01-05T09:00:00+00:00"},
        GS: {"projects": 4141, "last_sync": "2026-07-28T09:00:00+00:00"},
    }
    assert "2026-01-05" in gui_app.data_as_of(summary)


def test_data_as_of_ignores_registries_holding_nothing():
    summary = {
        VERRA: {"projects": 5245, "last_sync": "2026-07-28T09:00:00+00:00"},
        GS: {"projects": 0, "last_sync": "2020-01-01T00:00:00+00:00"},
    }
    assert "2026-07-28" in gui_app.data_as_of(summary)


# --- the poll loop --------------------------------------------------------
#
# `_drain` is the whole bridge between the worker thread and the screen. It is
# exercised here against a stand-in `self` rather than a window, because what
# matters about it is control flow, not widgets.


class _FakeRoot:
    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))


class _DrainHarness:
    """Enough of the window for `_drain` to run against."""

    def __init__(self, messages, explode=False):
        self.root = _FakeRoot()
        self.queue = queue.Queue()
        for message in messages:
            self.queue.put(message)
        self.handled = []
        self._explode = explode

    def _handle(self, message):
        self.handled.append(message)
        if self._explode:
            raise RuntimeError("a widget raised")

    def _drain(self):
        return gui_app.App._drain(self)


def test_the_poll_loop_reschedules_itself():
    harness = _DrainHarness([gui_worker.Note("hello")])
    gui_app.App._drain(harness)
    assert harness.handled == [gui_worker.Note("hello")]
    assert [delay for delay, _ in harness.root.scheduled] == [gui_app.POLL_MS]


def test_one_bad_message_does_not_stop_the_window_forever():
    """The reschedule is the loop.

    Unprotected, a single TclError out of `_handle` freezes the log pane and
    the bar for the lifetime of the process, `Finished` never arrives, and
    Cancel and Export stay disabled — on the one front end whose user has no
    console to find the traceback in.
    """
    harness = _DrainHarness([gui_worker.Note("boom")], explode=True)
    with pytest.raises(RuntimeError):
        gui_app.App._drain(harness)
    assert harness.root.scheduled, "the poll loop was not rearmed"


def test_the_end_of_a_run_refreshes_even_though_the_thread_is_still_alive(monkeypatch):
    """`Finished` is the worker's last message, sent before the thread unwinds.

    So `worker.busy` is usually still True when `_finish` runs, and the
    refresh that exists to show the new counts is the one that silently does
    not happen. Its caller passes `force`.
    """
    assert "self.refresh_summary(force=True)" in Path(gui_app.__file__).read_text(
        encoding="utf-8"
    )

    reads = []

    class _Caption:
        def configure(self, **kwargs):
            pass

    class _Window:
        worker = type("W", (), {"busy": True})()
        captions: dict = {}
        as_of = _Caption()

    @contextmanager
    def fake_session(path=None):
        reads.append(path)
        yield None

    monkeypatch.setattr(gui_app.db, "session", fake_session)
    monkeypatch.setattr(gui_app.db, "registry_summary", lambda conn: {})

    gui_app.App.refresh_summary(_Window())
    assert reads == [], "a refresh mid-run is still skipped"

    gui_app.App.refresh_summary(_Window(), force=True)
    assert len(reads) == 1


# --- structural rules -----------------------------------------------------


def test_the_gui_never_calls_the_typer_commands():
    """CLAUDE.md's rule, enforced rather than remembered.

    The command functions carry Typer `OptionInfo` defaults: calling them from
    anything but Typer works by accident and breaks the moment a signature
    changes.
    """
    forbidden = ("import cli", "from ..cli", "from carbon_scraper.cli", "cli.app")
    package = Path(gui_app.__file__).parent
    for module in package.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in source, f"{module.name} references {pattern}"


def test_the_worker_holds_no_widget():
    """Tkinter is not thread-safe; the worker must not be able to touch one."""
    source = Path(gui_worker.__file__).read_text(encoding="utf-8")
    assert "tkinter" not in source
    assert "import tk" not in source


def test_the_gui_builds_its_registries_from_the_label_table():
    """Adding a registry must add a checkbox with no GUI edit."""
    source = Path(gui_app.__file__).read_text(encoding="utf-8")
    assert "settings.REGISTRY_LABELS.items()" in source
    for name in settings.REGISTRY_LABELS:
        assert f'"{name}"' not in source, f"{name} is hard-coded in the window"
