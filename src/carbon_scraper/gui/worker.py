"""The background thread, and the one-way queue everything travels back on.

Tkinter is not thread-safe: a widget touched from anywhere but the main loop
corrupts the display or crashes the interpreter, usually not immediately and
usually not on the machine it was developed on. **This module therefore
imports no Tk and holds no widget.** It can only put messages on a queue; the
window drains that queue from `root.after`, on the main loop, where touching a
widget is legal.

The same queue carries three kinds of traffic — pipeline progress, pipeline
messages, and log records — so the log pane and the progress bar cannot get out
of order with each other.

Cancellation is a `threading.Event` handed to `pipeline`, which passes it to
`http_client`; it is checked between requests and slept on during retry
backoff, so Cancel takes effect within about a second instead of up to the 45 s
a backoff can otherwise block for. Stopping is always safe: every database
write is an idempotent upsert, so a cancelled sync is repaired by running it
again, not by starting over.
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import settings
from ..http_client import Cancelled

log = logging.getLogger(__name__)

#: What a task is handed and what it must return: it is given the sink to
#: report through and the event to stop on, and returns (summary, result).
Task = Callable[[Any, threading.Event], "tuple[str, Any]"]


# --- messages -------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    """Position within one resource of one registry."""

    registry: str
    resource: str
    done: int
    total: int | None


@dataclass(frozen=True)
class Note:
    """A completed step, worth a line in the log pane."""

    text: str


@dataclass(frozen=True)
class LogLine:
    level: int
    text: str


@dataclass(frozen=True)
class Finished:
    """The end of a task, however it ended.

    `cancelled` is separate from `ok` on purpose: a user who pressed Cancel
    has not hit a bug and must not be shown a crash dialog.
    """

    ok: bool
    cancelled: bool
    summary: str
    result: Any = None
    traceback_file: Path | None = None


# --- the sink and the logging bridge --------------------------------------


class QueueSink:
    """A `pipeline.ProgressSink` that posts to the window's queue.

    `pipeline._Throttle` already rate-limits `stage` centrally, so this stays a
    plain put with no throttling of its own — one place to get that right.
    """

    def __init__(self, outbox: queue.Queue) -> None:
        self._outbox = outbox

    def message(self, text: str) -> None:
        self._outbox.put(Note(text))

    def stage(self, registry: str, resource: str, done: int, total: int | None) -> None:
        self._outbox.put(Progress(registry, resource, done, total))


class QueueLogHandler(logging.Handler):
    """Mirrors log records into the window.

    Installed on the root logger before anything calls `logging.basicConfig`,
    which is a no-op once a handler exists — so the GUI's handler wins and the
    CLI's stdout config never takes effect in a windowed process.

    This is how `INCOMPLETE` reconciliation warnings reach the user. They are
    the difference between a short sync and a wrong one, and a business user
    has no console to find them in.
    """

    def __init__(self, outbox: queue.Queue) -> None:
        super().__init__()
        self._outbox = outbox

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._outbox.put(LogLine(record.levelno, self.format(record)))
        except Exception:  # pragma: no cover - never let logging break a run
            self.handleError(record)


def install_logging(outbox: queue.Queue, log_dir: Path | None = None) -> Path:
    """Route logging to the window and to a file. Returns the file's path.

    The file matters as much as the pane: a user reporting "it did not work"
    can attach it, and it survives the window being closed.
    """
    directory = Path(log_dir) if log_dir else settings.LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gui.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    handler = QueueLogHandler(outbox)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    # One INFO line per HTTP request would be ~7,300 lines for a Gold Standard
    # sync, and says nothing a business user can act on.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path


# --- the worker -----------------------------------------------------------


class Worker:
    """Runs one task at a time on a background thread.

    One at a time is a deliberate limit, not a simplification: two concurrent
    tasks would share a SQLite connection factory and double the request rate
    at the registries, and the politeness limit is per-process.
    """

    def __init__(self, outbox: queue.Queue | None = None) -> None:
        self.outbox: queue.Queue = outbox or queue.Queue()
        self.cancel = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, name: str, task: Task) -> bool:
        """Begin `task`. Returns False if something is already running."""
        if self.busy:
            return False
        self.cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(name, task), name=f"carbon-{name}", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self.cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- the thread body ---------------------------------------------------

    def _run(self, name: str, task: Task) -> None:
        sink = QueueSink(self.outbox)
        try:
            summary, result = task(sink, self.cancel)
        except Cancelled:
            self.outbox.put(
                Finished(
                    ok=False,
                    cancelled=True,
                    summary="Stopped. Nothing was corrupted — every write is an "
                    "upsert, so running this again picks up where it left off.",
                )
            )
        except Exception as exc:  # noqa: BLE001 - the top of a thread; nothing above catches
            path = self._write_traceback(name, exc)
            self.outbox.put(
                Finished(
                    ok=False,
                    cancelled=False,
                    summary=f"{type(exc).__name__}: {exc}",
                    traceback_file=path,
                )
            )
        else:
            self.outbox.put(
                Finished(ok=True, cancelled=False, summary=summary, result=result)
            )

    @staticmethod
    def _write_traceback(name: str, exc: BaseException) -> Path | None:
        """Put the traceback somewhere the user can send it from.

        A non-technical user cannot copy a console, and there is no console
        here to copy: the packaged build is `--noconsole`. The dialog shows
        this path.
        """
        try:
            settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = settings.LOG_DIR / f"error-{name}-{stamp}.log"
            path.write_text(
                "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
                encoding="utf-8",
            )
            return path
        except OSError:  # pragma: no cover - disk full, read-only profile
            log.exception("Could not write the traceback file.")
            return None
