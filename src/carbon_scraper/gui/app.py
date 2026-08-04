"""The window.

One screen, top to bottom: which registries, where the spreadsheet goes, the
two buttons, progress, and the log.

**Two buttons, deliberately separate.**

* *Export Excel* re-applies the derivation rules and writes the next version of
  the spreadsheet. Seconds, no network. This is the button the business uses,
  and it works on a machine that has never scraped anything, because the
  installer ships a database.
* *Update registry data* is the scrape. Hours, opt-in, with the estimate shown
  before it starts. It deliberately does **not** export afterwards: writing a
  delivery is an act, and `_vN+1` should appear because someone asked for it.

The checkboxes mean the same thing for both buttons. A subset ticked exports a
subset — the pipeline's registry filter takes a sequence precisely so the
window does not have to lie about that.

Threading rules, and they are not negotiable: the worker thread only ever puts
messages on a queue, and every widget is touched here, on the Tk main loop,
from `_drain`. See `worker.py`.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import db, pipeline, settings
from .state import UiState
from .worker import Finished, LogLine, Note, Progress, Worker, install_logging

log = logging.getLogger(__name__)

POLL_MS = 150
MAX_PER_TICK = 400
MAX_LOG_LINES = 500


# --- formatting, kept free of Tk so it can be tested ----------------------


def format_age(stamp: str | None, *, now: datetime | None = None) -> str:
    """"2 days ago" for a stored ISO timestamp; "never" for nothing."""
    if not stamp:
        return "never synced"
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return "synced at an unreadable time"
    reference = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    days = (reference - when).days
    if days <= 0:
        return "synced today"
    if days == 1:
        return "synced yesterday"
    return f"synced {days} days ago"


def format_duration(minutes: float | None) -> str:
    if minutes is None:
        return "an unknown time"
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return f"about {round(minutes)} minutes"
    hours = minutes / 60
    return f"about {hours:.0f} hours" if hours >= 2 else "about an hour and a half"


def summary_line(entry: dict[str, Any]) -> str:
    """The grey text beside a checkbox: what is stored, and how it got there.

    A failed or cancelled attempt is reported even when the registry holds
    nothing, because those are the two cases that look identical to a registry
    nobody has ever scraped — and one of them means "try again", not "this is
    how it is".
    """
    projects = entry.get("projects") or 0
    line = (
        f"{projects:,} projects · {format_age(entry.get('last_sync'))}"
        if projects
        else "nothing stored yet"
    )
    if entry.get("last_sync_ok") is False:
        line += " · last attempt did not finish"
    return line


def estimate_minutes(names: list[str]) -> float | None:
    """How long scraping `names` should take, or None if any is unknown.

    Unknown for one registry makes the whole estimate unknown rather than an
    undercount: a total that quietly omits a registry is worse than admitting
    the number is not known, because the user believes it.
    """
    total = 0.0
    for name in names:
        minutes = settings.SYNC_ESTIMATE_MINUTES.get(name)
        if minutes is None:
            return None
        total += minutes
        if name == settings.VERRA:
            total += settings.VERRA_TOTALS_ESTIMATE_MINUTES
    return total


def registry_labels(names: list[str]) -> str:
    return ", ".join(settings.REGISTRY_LABELS.get(n, n) for n in names)


# --- what the two buttons actually do -------------------------------------
#
# Module-level rather than closures inside the button handlers, so that what
# each button does can be asserted without opening a window. The difference
# between them is the point of the design, and it is worth a test.


def build_export_task(names: list[str], out_dir: Path) -> Any:
    """Derive, then write the next version of the spreadsheet. No network."""

    def task(sink: Any, _cancel: Any) -> tuple[str, Any]:
        # Derive first: a classification rule edited in
        # config/derivation/*.yaml has to reach the sheet without a re-scrape,
        # which is the whole reason sync and derive are separate steps.
        written = pipeline.derive_all(names, sink=sink)
        sink.message(f"Applied derivation rules: {written:,} values.")
        path, rows, previous = pipeline.export(names, out_dir=out_dir, sink=sink)
        kept = f" Previous delivery kept as {previous.name}." if previous else ""
        return f"Wrote {rows:,} rows to {path.name}.{kept}", path

    return task


def build_update_task(names: list[str]) -> Any:
    """Scrape, fix Verra's totals, derive. **Deliberately does not export.**

    Writing a delivery is an act. If this exported, every refresh would burn a
    version number and the business would receive `_v9` without anyone having
    decided to send anything.
    """

    def task(sink: Any, cancel: Any) -> tuple[str, Any]:
        # The scrape/totals/derive chain — including the Verra-only guard on
        # the exact-totals pass — belongs to pipeline, so this button and
        # `verra update` cannot drift apart.
        written = pipeline.update_all(names, sink=sink, cancel=cancel)
        return (
            f"Updated {registry_labels(names)}. {written:,} derived values. "
            f"Press Export Excel to write a new spreadsheet.",
            None,
        )

    return task


def data_as_of(summary: dict[str, dict[str, Any]]) -> str:
    """The oldest sync among registries that hold data.

    Deliberately the oldest, not the newest: a sheet is only as current as its
    stalest registry, and the newest date would flatter it. Open question 3 in
    PLAN.md — how stale the shipped database may be — is answered by the user
    seeing this, rather than by a hidden assumption.
    """
    stamps = [
        entry["last_sync"]
        for entry in summary.values()
        if entry.get("projects") and entry.get("last_sync")
    ]
    if not stamps:
        return "Data as of: nothing stored yet"
    return f"Data as of: {min(stamps)[:10]} (oldest registry)"


# --- the window -----------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = UiState.load()
        self.queue: queue.Queue = queue.Queue()
        self.worker = Worker(self.queue)
        self.log_path = install_logging(self.queue)
        self.last_export: Path | None = None
        self.ticked: dict[str, tk.BooleanVar] = {}
        self.captions: dict[str, ttk.Label] = {}

        root.title("Carbon Registry Scraper")
        root.minsize(760, 620)
        self._build()
        self.refresh_summary()
        root.after(POLL_MS, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)

        self.as_of = ttk.Label(outer, text="", foreground="#555")
        self.as_of.grid(row=0, column=0, sticky="w", pady=(0, 8))

        # --- registries ---
        box = ttk.LabelFrame(outer, text="Registries", padding=10)
        box.grid(row=1, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        for index, (name, label) in enumerate(settings.REGISTRY_LABELS.items()):
            var = tk.BooleanVar(value=name in self.state.registries)
            var.trace_add("write", lambda *_: self._on_selection_change())
            self.ticked[name] = var
            ttk.Checkbutton(box, text=label, variable=var).grid(
                row=index, column=0, sticky="w", padx=(0, 12)
            )
            caption = ttk.Label(box, text="", foreground="#666")
            caption.grid(row=index, column=1, sticky="w")
            self.captions[name] = caption

        # --- output folder ---
        folder = ttk.LabelFrame(outer, text="Save the spreadsheet to", padding=10)
        folder.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        folder.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=str(self.state.out_path()))
        ttk.Entry(folder, textvariable=self.folder_var, state="readonly").grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(folder, text="Browse…", command=self._choose_folder).grid(
            row=0, column=1
        )
        ttk.Label(
            folder,
            text="Each export writes a new version. Previous deliveries are never "
            "overwritten.",
            foreground="#666",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- buttons ---
        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.export_button = ttk.Button(
            buttons, text="Export Excel", command=self._on_export
        )
        self.export_button.pack(side="left")
        self.update_button = ttk.Button(
            buttons, text="Update registry data…", command=self._on_update
        )
        self.update_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", command=self._on_cancel, state="disabled"
        )
        self.cancel_button.pack(side="left")
        self.open_button = ttk.Button(
            buttons, text="Open folder", command=self._open_folder, state="disabled"
        )
        self.open_button.pack(side="right")

        # --- progress ---
        progress = ttk.Frame(outer)
        progress.grid(row=4, column=0, sticky="ew", pady=(12, 4))
        progress.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(progress, mode="determinate", maximum=100)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.status = ttk.Label(progress, text="Ready.")
        self.status.grid(row=1, column=0, sticky="w", pady=(4, 0))

        # --- log ---
        pane = ttk.LabelFrame(outer, text="Log", padding=6)
        pane.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(0, weight=1)
        self.log_view = tk.Text(pane, height=12, wrap="word", state="disabled")
        self.log_view.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(pane, command=self.log_view.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_view.configure(yscrollcommand=scroll.set)
        self.log_view.tag_configure("warn", foreground="#b06000")
        self.log_view.tag_configure("error", foreground="#b00020")
        self._append(f"Log file: {self.log_path}")

    # -- reading the window ------------------------------------------------

    def selection(self) -> list[str]:
        """The ticked registries, in canonical order."""
        return [name for name in settings.REGISTRY_LABELS if self.ticked[name].get()]

    def _on_selection_change(self) -> None:
        self.state.registries = self.selection()
        self.state.save()
        self._set_running(self.worker.busy)

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="Where should the spreadsheet be saved?",
            initialdir=str(self.state.out_path()),
        )
        if not chosen:
            return
        self.state.out_dir = chosen
        self.state.save()
        self.folder_var.set(chosen)

    # -- database-backed captions ------------------------------------------

    def refresh_summary(self, *, force: bool = False) -> None:
        """Re-read the row counts. Skipped while a run is in flight.

        Not only to avoid two connections contending: during a sync the numbers
        change every second, and a caption that flickers upward is noise on top
        of a progress bar that already says the same thing.

        `force` is for the one call that happens *because* a run ended.
        `Finished` is the worker's last message but the thread is still alive
        while it unwinds, so `busy` usually still reads True there — and the
        refresh that exists to show the new numbers is the one that silently
        does not happen.
        """
        if self.worker.busy and not force:
            return
        try:
            with db.session() as conn:
                summary = db.registry_summary(conn)
        except Exception as exc:  # noqa: BLE001 - a missing database is not fatal here
            log.warning("Could not read the database: %s", exc)
            return
        for name, caption in self.captions.items():
            caption.configure(text=summary_line(summary.get(name, {})))
        self.as_of.configure(text=data_as_of(summary))

    # -- actions -----------------------------------------------------------

    def _on_export(self) -> None:
        names = self.selection()
        if not names:
            messagebox.showinfo("Nothing selected", "Tick at least one registry.")
            return
        self._begin(
            "export",
            "Building the spreadsheet…",
            build_export_task(names, self.state.out_path()),
        )

    def _on_update(self) -> None:
        names = self.selection()
        if not names:
            messagebox.showinfo("Nothing selected", "Tick at least one registry.")
            return

        labels = registry_labels(names)
        estimate = format_duration(estimate_minutes(names))
        if not messagebox.askokcancel(
            "Update registry data",
            f"About to re-scrape: {labels}.\n\n"
            f"This takes {estimate} and runs at about one request per second, "
            f"which is deliberate — the registries are public services and we do "
            f"not hammer them.\n\n"
            f"You can press Cancel at any point. Nothing is corrupted by "
            f"stopping: the next run picks up where this one left off.\n\n"
            f"No spreadsheet is written. Press Export Excel afterwards.",
        ):
            return

        self._begin("update", "Scraping…", build_update_task(names))

    def _on_cancel(self) -> None:
        self.worker.stop()
        self.status.configure(text="Stopping after the current request…")
        self.cancel_button.configure(state="disabled")

    def _open_folder(self) -> None:
        target = self.last_export.parent if self.last_export else self.state.out_path()
        try:
            if sys.platform == "win32":
                os.startfile(target)  # noqa: S606 - opening a folder the user chose
            else:  # pragma: no cover - the packaged target is Windows
                import subprocess

                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", str(target)]
                )
        except OSError as exc:
            messagebox.showerror("Could not open the folder", str(exc))

    def _begin(self, name: str, status: str, task: Any) -> None:
        if not self.worker.start(name, task):
            messagebox.showinfo("Busy", "Something is already running.")
            return
        self._set_running(True)
        self.bar.configure(mode="indeterminate")
        self.bar.start(60)
        self.status.configure(text=status)
        self._append(f"--- {status}")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.export_button.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")
        if running:
            self.update_button.configure(state="disabled")
        else:
            # Nothing ticked means neither button has anything to act on.
            self.update_button.configure(
                state="normal" if self.selection() else "disabled"
            )
            self.export_button.configure(
                state="normal" if self.selection() else "disabled"
            )

    # -- the queue ---------------------------------------------------------

    def _drain(self) -> None:
        """Move the worker's messages onto the screen. Main loop only.

        The reschedule is in a `finally` because it is the whole loop. One
        exception out of `_handle` — a `TclError` from a widget, a messagebox
        that fails — would otherwise stop the poll for the lifetime of the
        process: the log pane and the bar freeze, `Finished` never arrives,
        and Cancel and Export stay disabled forever, on the one front end
        whose user has no console to find the traceback in.
        """
        try:
            for _ in range(MAX_PER_TICK):
                try:
                    message = self.queue.get_nowait()
                except queue.Empty:
                    break
                self._handle(message)
        finally:
            self.root.after(POLL_MS, self._drain)

    def _handle(self, message: Any) -> None:
        if isinstance(message, Progress):
            self._show_progress(message)
        elif isinstance(message, Note):
            self._append(message.text)
        elif isinstance(message, LogLine):
            tag = (
                "error"
                if message.level >= logging.ERROR
                else "warn"
                if message.level >= logging.WARNING
                else None
            )
            self._append(message.text, tag)
        elif isinstance(message, Finished):
            self._finish(message)

    def _show_progress(self, message: Progress) -> None:
        name = settings.REGISTRY_LABELS.get(message.registry, message.registry)
        if message.total:
            if str(self.bar.cget("mode")) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.configure(value=min(100.0, message.done * 100.0 / message.total))
            of = f" of {message.total:,}"
        else:
            of = ""
        self.status.configure(
            text=f"{name} · {message.resource} · {message.done:,}{of}"
        )

    def _finish(self, message: Finished) -> None:
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100 if message.ok else 0)
        self._set_running(False)
        self.status.configure(text=message.summary)
        self._append(message.summary)
        self.refresh_summary(force=True)

        if message.ok:
            if isinstance(message.result, Path):
                self.last_export = message.result
                self.open_button.configure(state="normal")
            messagebox.showinfo("Done", message.summary)
        elif message.cancelled:
            messagebox.showinfo("Stopped", message.summary)
        else:
            detail = (
                f"\n\nThe full details were written to:\n{message.traceback_file}"
                if message.traceback_file
                else f"\n\nSee {self.log_path}"
            )
            messagebox.showerror("Something went wrong", message.summary + detail)

    def _append(self, text: str, tag: str | None = None) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n", tag or ())
        # Trim from the top: an overnight sync would otherwise grow the widget
        # without bound, and the file handler already keeps everything.
        excess = int(self.log_view.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log_view.delete("1.0", f"{excess + 1}.0")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    # -- closing -----------------------------------------------------------

    def _on_close(self) -> None:
        if not self.worker.busy:
            self.root.destroy()
            return
        if not messagebox.askokcancel(
            "Still running",
            "A run is still going. Stop it and close?\n\n"
            "Nothing is corrupted by stopping — every write is an upsert, so "
            "running it again picks up where it left off.",
        ):
            return
        self.worker.stop()
        self.status.configure(text="Stopping…")
        self._wait_then_destroy(deadline=40)

    def _wait_then_destroy(self, deadline: int) -> None:
        """Give the worker a moment to notice the cancel, then close anyway.

        It is a daemon thread, so the process would exit regardless; waiting
        just lets the in-flight request finish and its transaction commit.
        """
        if not self.worker.busy or deadline <= 0:
            self.root.destroy()
            return
        self.root.after(250, lambda: self._wait_then_destroy(deadline - 1))


def main() -> None:
    settings.ensure_dirs()
    root = tk.Tk()
    try:
        # A visible improvement on Windows' default Tk theme, and absent
        # elsewhere; not worth failing to open a window over.
        ttk.Style().theme_use("vista")
    except tk.TclError:  # pragma: no cover - non-Windows
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":  # pragma: no cover
    main()
