"""The window.

One screen, top to bottom: what is held and how old it is, which registries,
where the spreadsheet goes, the two buttons, and — folded away — the log.

**Two buttons, deliberately separate.**

* *Export Excel* re-applies the derivation rules and writes the next version of
  the spreadsheet. Seconds, no network. This is the button the business uses,
  and it works on a machine that has never scraped anything, because the
  installer ships a database. It is the only control given the accent colour.
* *Update registry data* is the scrape. Hours, opt-in, with the estimate shown
  before it starts. It deliberately does **not** export afterwards: writing a
  delivery is an act, and `_vN+1` should appear because someone asked for it.

The checkboxes mean the same thing for both buttons. A subset ticked exports a
subset — the pipeline's registry filter takes a sequence precisely so the
window does not have to lie about that.

**The registry list is also the progress display.** `Progress` arrives naming
the registry it belongs to, so the row for that registry shows the resource and
the position, and the rest of the list stays still. There is no single 0-100%
bar across a run, because there is no honest one: eight registries over four
hours through one bar means either eight resets or an invented weighting. What
sits under the header instead advances a step per registry and never goes
backwards.

Threading rules, and they are not negotiable: the worker thread only ever puts
messages on a queue, and every widget is touched here, on the Tk main loop,
from `_drain`. See `worker.py`.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import db, pipeline, settings
from . import theme
from .state import UiState
from .worker import Finished, LogLine, Note, Progress, Worker, install_logging

log = logging.getLogger(__name__)

POLL_MS = 150
TICK_MS = 1000
MAX_PER_TICK = 400
MAX_LOG_LINES = 500

FRESH_DAYS = 30

# The freshness marker beside a registry. A filled mark is data that is here
# and recent, amber is here and old, and an outline is nothing at all — three
# states rather than a colour scale, because "how stale is too stale" is a
# business question nobody has answered yet.
MARK_FILLED = "●"
MARK_HOLLOW = "○"

# The tick itself, drawn as text rather than by the theme: see theme.py for
# why. Both glyphs are in Segoe UI, checked on the target platform.
TICKED = "☑"
UNTICKED = "☐"


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


def format_clock(seconds: float) -> str:
    """Elapsed time, in the units someone watching a long run reads."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return f"{int(seconds)} sec"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def remaining_text(estimate: float | None, elapsed_seconds: float) -> str:
    """What is left of a run, against the *static* estimate.

    Deliberately not measured from this run's own rate: most of a repeat run
    comes off the ~1 GB response cache in seconds, so an extrapolated rate
    would promise four minutes and then take two hours. Overrunning the
    estimate is said plainly rather than by counting down to zero and stopping.
    """
    if estimate is None:
        return "time to finish unknown"
    remaining = estimate * 60 - elapsed_seconds
    if remaining <= 0:
        return "longer than estimated"
    return f"about {format_clock(remaining)} left"


def summary_line(entry: dict[str, Any]) -> str:
    """The grey text on a registry's row: what is stored, and how it got there.

    A failed or cancelled attempt is reported even when the registry holds
    nothing, because those are the two cases that look identical to a registry
    nobody has ever scraped — and one of them means "try again", not "this is
    how it is".
    """
    projects = entry.get("projects") or 0
    line = format_age(entry.get("last_sync")) if projects else "nothing stored yet"
    if entry.get("last_sync_ok") is False:
        line += " · last attempt did not finish"
    return line


def count_text(entry: dict[str, Any]) -> str:
    """The number in a registry's own column.

    An em dash rather than `0`: a registry nobody has scraped holds nothing,
    which is not the same claim as a registry that was read and found empty.
    """
    projects = entry.get("projects") or 0
    return f"{projects:,}" if projects else "—"


def freshness(entry: dict[str, Any], *, now: datetime | None = None) -> tuple[str, str]:
    """`(marker, colour)` for one registry's row."""
    if entry.get("last_sync_ok") is False:
        return MARK_FILLED, theme.RED
    stamp = entry.get("last_sync")
    if not (entry.get("projects") and stamp):
        return MARK_HOLLOW, theme.FAINT
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return MARK_FILLED, theme.AMBER
    reference = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    days = (reference - when).days
    return MARK_FILLED, (theme.AMBER if days > FRESH_DAYS else theme.PETROL)


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


def headline(summary: dict[str, dict[str, Any]]) -> str:
    """The one line above everything: how much is held, across how many."""
    holding = {
        name: entry for name, entry in summary.items() if entry.get("projects")
    }
    if not holding:
        return "Nothing stored yet — press Update registry data to fetch it"
    projects = sum(entry["projects"] for entry in holding.values())
    plural = "registry" if len(holding) == 1 else "registries"
    return f"{projects:,} projects across {len(holding)} {plural}"


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
        return "no data yet"
    return f"data as of {min(stamps)[:10]}, the oldest registry"


# --- one line of the ledger -----------------------------------------------


class RegistryRow:
    """A registry's tick box, what is stored in it, and where a scrape is.

    The same row does both jobs on purpose. The alternative — a list up here
    and a progress panel down there — makes the user hold "which registry is
    this bar about" in their head, when the message already says.
    """

    def __init__(
        self,
        parent: ttk.Frame,
        index: int,
        label: str,
        variable: tk.BooleanVar,
        ui: theme.Theme,
    ) -> None:
        self.ui = ui
        self.label = label
        self.variable = variable
        pad = ui.px(10)

        self.frame = ttk.Frame(parent, style="Card.TFrame")
        self.frame.grid(row=index * 2, column=0, sticky="ew")
        # The stretch belongs to the name, so the counts stay in a column of
        # their own and read down the page as a column of figures.
        self.frame.columnconfigure(0, weight=1)

        self.tick = ttk.Checkbutton(
            self.frame, variable=variable, style="Ledger.TCheckbutton"
        )
        self.tick.grid(row=0, column=0, sticky="w", padx=(pad, pad))
        # Bound to the variable rather than redrawn by whoever changed it:
        # "select all", a click, and a restored selection all go through here,
        # and a glyph that disagrees with the box is worse than no glyph.
        variable.trace_add("write", lambda *_: self._sync_tick())
        self._sync_tick()

        # Wide enough for the widest thing it ever holds — a mid-scrape
        # "305,144 / 305,144" — because a clipped figure reads as a smaller
        # one rather than as a truncated one.
        self.count = ttk.Label(
            self.frame, text="", style="Count.TLabel", anchor="e", width=18
        )
        self.count.grid(row=0, column=1, sticky="e", padx=(0, pad))

        self.mark = ttk.Label(self.frame, text=MARK_HOLLOW, style="Card.TLabel")
        self.mark.grid(row=0, column=2, sticky="e")

        # A fixed width so a row switching between "synced today" and
        # "reading retirements" does not shuffle the column beside it.
        self.state = ttk.Label(
            self.frame, text="", style="CardMuted.TLabel", anchor="w", width=26
        )
        self.state.grid(row=0, column=3, sticky="w", padx=(ui.px(6), pad))

        # Sits in the row rather than beside it, hidden until this registry is
        # the one being read.
        self.bar = ttk.Progressbar(
            self.frame,
            style="Row.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
        )
        self.bar.grid(row=1, column=0, columnspan=4, sticky="ew", padx=pad,
                      pady=(0, ui.px(6)))
        self.bar.grid_remove()

        # A rule between rows, not around them: the list reads as a ledger,
        # and eight boxed cards would read as eight unrelated things.
        rule = ttk.Frame(parent, style="Hairline.TFrame", height=1)
        rule.grid(row=index * 2 + 1, column=0, sticky="ew")
        self.rule = rule

    def _sync_tick(self) -> None:
        glyph = TICKED if self.variable.get() else UNTICKED
        self.tick.configure(text=f"{glyph}  {self.label}")

    def show_stored(self, entry: dict[str, Any]) -> None:
        self.count.configure(
            text=count_text(entry),
            style="Count.TLabel" if entry.get("projects") else "CountFaint.TLabel",
        )
        mark, colour = freshness(entry)
        self.mark.configure(text=mark, foreground=colour)
        self.state.configure(text=summary_line(entry), style="CardMuted.TLabel")
        self.bar.grid_remove()

    def show_progress(self, resource: str, done: int, total: int | None) -> None:
        self.count.configure(
            text=f"{done:,} / {total:,}" if total else f"{done:,}",
            style="Count.TLabel",
        )
        self.mark.configure(text=MARK_FILLED, foreground=theme.PETROL)
        self.state.configure(text=f"reading {resource}", style="CardMuted.TLabel")
        self.bar.grid()
        self.bar.configure(value=min(100.0, done * 100.0 / total) if total else 0)

    def set_active(self, ticked: bool) -> None:
        """Dim a row nobody has ticked. Neither button will act on it."""
        self.count.configure(style="Count.TLabel" if ticked else "CountFaint.TLabel")
        self.state.configure(
            style="CardMuted.TLabel" if ticked else "CardFaint.TLabel"
        )


# --- the window -----------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui = theme.apply(root)
        self.state = UiState.load()
        self.queue: queue.Queue = queue.Queue()
        self.worker = Worker(self.queue)
        self.log_path = install_logging(self.queue)
        self.last_export: Path | None = None
        self.ticked: dict[str, tk.BooleanVar] = {}
        self.rows: dict[str, RegistryRow] = {}
        self.details_open = False
        self.started_at: float | None = None
        self.estimate: float | None = None
        self.timed = False
        self.running_label = ""
        self.overall_value = 0.0
        self.seen: list[str] = []
        self._ticking = False
        self._batching = False

        root.title("Carbon Registry Scraper")
        theme.set_icon(root)
        self._build()
        self.refresh_summary()
        self._set_running(False)
        root.after(POLL_MS, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Return>", self._on_return)
        root.bind("<Escape>", lambda _event: self._on_cancel())
        self._fit(set_minimum=True)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        ui = self.ui
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self._build_header()

        body = ttk.Frame(self.root, padding=(ui.px(18), ui.px(10)))
        body.grid(row=1, column=0, sticky="ew")
        body.columnconfigure(0, weight=1)

        self._build_ledger(body)
        self._build_folder(body)
        self._build_actions(body)
        self._build_details(body)

    def _build_header(self) -> None:
        ui = self.ui
        # The bottom padding leaves room for the overall bar, which is drawn
        # on the band's own bottom edge: without it the bar sits on the
        # descenders of the line above.
        header = ttk.Frame(self.root, style="Header.TFrame",
                           padding=(ui.px(18), ui.px(10), ui.px(18), ui.px(12)))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        self.headline = ttk.Label(header, text="", style="Header.TLabel")
        self.headline.grid(row=0, column=0, sticky="w")

        # The trust line. It says how stale the sheet about to be written is,
        # which is the one thing a business user cannot find out any other way.
        self.as_of = ttk.Label(header, text="", style="HeaderMuted.TLabel")
        self.as_of.grid(row=1, column=0, sticky="w", pady=(ui.px(2), 0))

        self.overall = ttk.Progressbar(
            self.root, style="Overall.Horizontal.TProgressbar",
            mode="determinate", maximum=100,
        )
        self.overall.grid(row=0, column=0, sticky="sew")

    def _build_ledger(self, parent: ttk.Frame) -> None:
        ui = self.ui
        eyebrow = ttk.Frame(parent)
        eyebrow.grid(row=0, column=0, sticky="ew")
        eyebrow.columnconfigure(0, weight=1)
        ttk.Label(eyebrow, text="REGISTRIES", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            eyebrow, text="select all", style="Link.TButton",
            command=lambda: self._select_every(True),
        ).grid(row=0, column=1)
        ttk.Label(eyebrow, text="·", style="Muted.TLabel", font=ui.fonts.small).grid(
            row=0, column=2, padx=ui.px(2)
        )
        ttk.Button(
            eyebrow, text="none", style="Link.TButton",
            command=lambda: self._select_every(False),
        ).grid(row=0, column=3)

        # A hairline frame with one pixel of padding, rather than a `solid`
        # relief: ttk draws that in its own near-black and the list would sit
        # in a box heavier than anything else on screen.
        border = ttk.Frame(parent, style="Hairline.TFrame", padding=1)
        border.grid(row=1, column=0, sticky="ew", pady=(ui.px(6), 0))
        border.columnconfigure(0, weight=1)
        ledger = ttk.Frame(border, style="Card.TFrame")
        ledger.grid(row=0, column=0, sticky="ew")
        ledger.columnconfigure(0, weight=1)

        for index, (name, label) in enumerate(settings.REGISTRY_LABELS.items()):
            var = tk.BooleanVar(value=name in self.state.registries)
            var.trace_add("write", lambda *_: self._on_selection_change())
            self.ticked[name] = var
            self.rows[name] = RegistryRow(ledger, index, label, var, ui)
        # The last rule would draw on top of the container's own border.
        self.rows[list(self.rows)[-1]].rule.grid_remove()

    def _build_folder(self, parent: ttk.Frame) -> None:
        ui = self.ui
        # The eyebrow sits on the same line as the field rather than above it.
        # Four stacked lines here is most of the reason the window did not fit
        # a 1080-line screen at 150%, and the folder is not the main event.
        folder = ttk.Frame(parent)
        folder.grid(row=3, column=0, sticky="ew", pady=(ui.px(14), 0))
        folder.columnconfigure(1, weight=1)
        ttk.Label(folder, text="SPREADSHEET FOLDER", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, ui.px(10))
        )
        self.folder_var = tk.StringVar(value=str(self.state.out_path()))
        self.folder_entry = ttk.Entry(
            folder, textvariable=self.folder_var, state="readonly",
            font=ui.fonts.mono_small,
        )
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=(0, ui.px(8)))
        self.folder_entry.bind("<Configure>", lambda _event: self._show_folder_tail())
        ttk.Button(
            folder, text="Change…", style="Secondary.TButton",
            command=self._choose_folder,
        ).grid(row=0, column=2)
        ttk.Label(
            parent,
            text="Every export writes a new version. Previous deliveries are never "
            "overwritten.",
            style="Muted.TLabel",
            font=ui.fonts.small,
        ).grid(row=4, column=0, sticky="w", pady=(ui.px(6), 0))

    def _build_actions(self, parent: ttk.Frame) -> None:
        ui = self.ui
        buttons = ttk.Frame(parent)
        buttons.grid(row=5, column=0, sticky="ew", pady=(ui.px(14), 0))
        buttons.columnconfigure(3, weight=1)

        self.export_button = ttk.Button(
            buttons, text="Export Excel", style="Accent.TButton",
            command=self._on_export,
        )
        self.export_button.grid(row=0, column=0)
        self.update_button = ttk.Button(
            buttons, text="Update registry data…", style="Secondary.TButton",
            command=self._on_update,
        )
        self.update_button.grid(row=0, column=1, padx=(ui.px(8), 0))
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", style="Secondary.TButton",
            command=self._on_cancel, state="disabled",
        )
        self.cancel_button.grid(row=0, column=2, padx=(ui.px(8), 0))
        self.open_button = ttk.Button(
            buttons, text="Open folder", style="Link.TButton",
            command=self._open_folder, state="disabled",
        )
        self.open_button.grid(row=0, column=3, sticky="e")

        self.status = ttk.Label(parent, text="Ready.", style="Muted.TLabel")
        self.status.grid(row=6, column=0, sticky="w", pady=(ui.px(10), 0))

    def _build_details(self, parent: ttk.Frame) -> None:
        """The log, and the one control that reveals it.

        The button lives with the rest of the body and the pane below it in
        the row that carries the window's weight. Together in one weighted
        row, a window clamped to the screen height shrinks *both* — and the
        first thing to disappear off the bottom is the control that would put
        the log away again.
        """
        ui = self.ui
        self.details_button = ttk.Button(
            parent, text="▸ Details", style="Link.TButton",
            command=self._toggle_details,
        )
        self.details_button.grid(row=7, column=0, sticky="w", pady=(ui.px(8), 0))

        self.details = ttk.Frame(
            self.root, padding=(ui.px(18), 0, ui.px(18), ui.px(14))
        )
        self.details.grid(row=2, column=0, sticky="nsew")
        self.details.columnconfigure(0, weight=1)
        self.details.rowconfigure(0, weight=1)
        self.log_view = tk.Text(
            self.details, height=5, wrap="word", state="disabled",
            font=ui.fonts.mono_small, background=theme.CARD, foreground=theme.INK,
            borderwidth=0, highlightthickness=1,
            highlightbackground=theme.HAIRLINE, highlightcolor=theme.HAIRLINE,
            padx=ui.px(8), pady=ui.px(6),
        )
        self.log_view.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.details, command=self.log_view.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_view.configure(yscrollcommand=scroll.set)
        self.log_view.tag_configure("warn", foreground=theme.AMBER)
        self.log_view.tag_configure("error", foreground=theme.RED)
        self._append(f"Log file: {self.log_path}")

        # Collapsed to start with: most sessions are one press of Export Excel,
        # and a wall of log text is noise for that user. It opens itself on the
        # first warning, so `INCOMPLETE` is never hidden.
        self.details.grid_remove()
        self.root.rowconfigure(2, weight=0)

    def _toggle_details(self, *, open_it: bool | None = None) -> None:
        wanted = (not self.details_open) if open_it is None else open_it
        if wanted == self.details_open:
            return
        self.details_open = wanted
        self.details_button.configure(text="▾ Details" if wanted else "▸ Details")
        if wanted:
            self.details.grid()
            self.root.rowconfigure(2, weight=1)
        else:
            self.details.grid_remove()
            self.root.rowconfigure(2, weight=0)
        self._fit()

    def _fit(self, *, set_minimum: bool = False) -> None:
        """Size the window to the layout, but never past the screen.

        Two failures this avoids, both of them silent. Tk will open a window
        *smaller* than its own layout asks for, which clips the right-hand
        columns off the ledger and the folder section off the bottom; and
        opening the log pane adds enough height to push the buttons off a
        1080-line screen, on a window with no scroll of its own. The log pane
        carries the row weight, so what gets shortened is the log.
        """
        self.root.update_idletasks()
        margin = self.ui.px(64)
        width = min(
            max(self.ui.px(720), self.root.winfo_reqwidth()),
            self.root.winfo_screenwidth() - margin,
        )
        height = min(
            self.root.winfo_reqheight(), self.root.winfo_screenheight() - margin
        )
        if set_minimum:
            # The minimum is the window without its log: a collapsed window
            # that cannot be shrunk to its own content is the same bug again.
            self.root.minsize(width, height)
        self.root.geometry(f"{width}x{height}")
        # Only now does the field know how wide it is, and therefore how much
        # of the path it is cutting.
        self._show_folder_tail()

    # -- reading the window ------------------------------------------------

    def selection(self) -> list[str]:
        """The ticked registries, in canonical order."""
        return [name for name in settings.REGISTRY_LABELS if self.ticked[name].get()]

    def _on_selection_change(self) -> None:
        if self._batching:
            return
        self.state.registries = self.selection()
        self.state.save()
        self._set_running(self.worker.busy)

    def _select_every(self, wanted: bool) -> None:
        """Tick or untick the lot as one change, not eight.

        Without the flag each box writes the state file and re-styles every
        row, so pressing "all" does that eight times over.
        """
        self._batching = True
        try:
            for var in self.ticked.values():
                var.set(wanted)
        finally:
            self._batching = False
        self._on_selection_change()

    def _on_return(self, event: Any) -> None:
        # A focused button handles its own Return; taking it here as well
        # starts the task twice and the second one is told it is busy.
        if isinstance(getattr(event, "widget", None), ttk.Button):
            return
        if str(self.export_button.cget("state")) != "disabled":
            self._on_export()

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
        self._show_folder_tail()

    def _show_folder_tail(self) -> None:
        """Scroll the path to its end.

        A path too long for the field is cut somewhere, and the end is the
        half worth keeping: every candidate folder starts `C:\\Users\\<name>\\`
        and it is the last segment that says which folder this is.

        Re-applied on every resize rather than once at startup, because at
        startup the field does not yet have its final width: scrolling to the
        end of a box that is about to change size lands back at the beginning.
        """
        self.folder_entry.xview_moveto(1.0)

    # -- database-backed captions ------------------------------------------

    def refresh_summary(self, *, force: bool = False) -> None:
        """Re-read the row counts. Skipped while a run is in flight.

        Not only to avoid two connections contending: during a sync the numbers
        change every second, and a row that flickers upward is noise on top of
        the progress the same row is already showing.

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
        for name, row in self.rows.items():
            row.show_stored(summary.get(name, {}))
            row.set_active(self.ticked[name].get())
        self.headline.configure(text=headline(summary))
        self.as_of.configure(text=data_as_of(summary))

    # -- actions -----------------------------------------------------------

    def _on_export(self) -> None:
        names = self.selection()
        if not names:
            messagebox.showinfo("Nothing selected", "Tick at least one registry.")
            return
        self._begin(
            "export",
            "Building the spreadsheet",
            build_export_task(names, self.state.out_path()),
            label="Building the spreadsheet",
            estimate=None,
            timed=False,
        )

    def _on_update(self) -> None:
        names = self.selection()
        if not names:
            messagebox.showinfo("Nothing selected", "Tick at least one registry.")
            return

        labels = registry_labels(names)
        estimate = estimate_minutes(names)
        if not messagebox.askokcancel(
            "Update registry data",
            f"About to re-scrape: {labels}.\n\n"
            f"This takes {format_duration(estimate)} and runs at about one request "
            f"per second, which is deliberate — the registries are public services "
            f"and we do not hammer them.\n\n"
            f"You can press Cancel at any point. Nothing is corrupted by "
            f"stopping: the next run picks up where this one left off.\n\n"
            f"No spreadsheet is written. Press Export Excel afterwards.",
        ):
            return

        self._begin(
            "update",
            "Starting…",
            build_update_task(names),
            label=labels,
            estimate=estimate,
            timed=True,
        )

    def _on_cancel(self) -> None:
        if not self.worker.busy:
            return
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

    def _begin(
        self,
        name: str,
        status: str,
        task: Any,
        *,
        label: str,
        estimate: float | None,
        timed: bool,
    ) -> None:
        if not self.worker.start(name, task):
            messagebox.showinfo("Busy", "Something is already running.")
            return
        self.started_at = time.monotonic()
        self.estimate = estimate
        self.timed = timed
        self.running_label = label
        self.overall_value = 0.0
        self.seen = []
        self.overall.configure(value=0)
        self._set_running(True)
        self.status.configure(text=status)
        self._append(f"--- {status}")
        if not self._ticking:
            self._ticking = True
            self.root.after(TICK_MS, self._tick)

    def _tick(self) -> None:
        """Keep elapsed and remaining honest while a long run is going."""
        if not self.worker.busy or self.started_at is None:
            self._ticking = False
            return
        self.status.configure(text=self._status_text())
        self.root.after(TICK_MS, self._tick)

    def _status_text(self) -> str:
        if self.started_at is None:
            return "Ready."
        elapsed = time.monotonic() - self.started_at
        line = f"{self.running_label} · {format_clock(elapsed)} elapsed"
        # Only a scrape gets a countdown. An export finishes in seconds, and
        # "about 0 min left" beside it is noise pretending to be information.
        return f"{line} · {remaining_text(self.estimate, elapsed)}" if self.timed else line

    def _set_running(self, running: bool) -> None:
        self.cancel_button.configure(state="normal" if running else "disabled")
        if running:
            self.export_button.configure(state="disabled")
            self.update_button.configure(state="disabled")
            return
        # Nothing ticked means neither button has anything to act on.
        state = "normal" if self.selection() else "disabled"
        self.export_button.configure(state=state)
        self.update_button.configure(state=state)
        for name, row in self.rows.items():
            row.set_active(self.ticked[name].get())
        if not self.selection():
            self.status.configure(text="Tick a registry to export or update it.")

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
            if tag:
                # `INCOMPLETE` is the difference between a short sync and a
                # wrong one, and it must not arrive in a pane nobody opened.
                self._toggle_details(open_it=True)
        elif isinstance(message, Finished):
            self._finish(message)

    def _show_progress(self, message: Progress) -> None:
        row = self.rows.get(message.registry)
        if row is not None:
            row.show_progress(message.resource, message.done, message.total)
            self._advance_overall(message.registry)
        self.status.configure(text=self._status_text())

    def _advance_overall(self, registry: str) -> None:
        """One step per registry, and never backwards.

        A registry is several resources and each one counts from zero, so a
        bar driven by the current resource's fraction would walk back several
        times per registry. Steps are coarse and true; a fraction would be
        smooth and invented.
        """
        if registry not in self.seen:
            self.seen.append(registry)
        selected = self.selection() or self.seen
        value = min(100.0, (len(self.seen) - 0.5) * 100.0 / max(1, len(selected)))
        self.overall_value = max(self.overall_value, value)
        self.overall.configure(value=self.overall_value)

    def _finish(self, message: Finished) -> None:
        self.started_at = None
        self.overall.configure(value=100 if message.ok else self.overall_value)
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
            self._toggle_details(open_it=True)
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
    # Before tk.Tk() exists, and from the process rather than a widget:
    # otherwise Windows renders the whole app at 96 DPI and scales the result,
    # which on a 4K screen is a permanently blurry window.
    theme.enable_dpi_awareness()
    settings.ensure_dirs()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":  # pragma: no cover
    main()
