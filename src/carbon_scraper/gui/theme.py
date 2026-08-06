"""How the window looks, and the two calls that have to happen before it opens.

Split out of `app.py` because it is the only part of the front end with no
decisions in it: nothing here reads the database, drives the pipeline or knows
what a button does. It picks colours and fonts and hands back a `Theme`.

**Every single thing in here is optional at runtime.** A missing font family, a
Tk build without the `clam` theme, a Windows without `shcore` — each falls back
and the window still opens. A business user losing an accent colour is a
nuisance; a business user losing the window is a support call.

Three things are worth the explanation:

* **DPI awareness must be set before `tk.Tk()` exists**, from the process, not
  from a widget. Without it Windows renders the whole app at 96 DPI and scales
  the bitmap up, which on a 4K laptop is a permanently blurry window — the
  single most visible defect in the packaged build, and invisible on a
  development machine at 100%.
* **The palette needs the `clam` theme.** `vista` draws buttons, checkboxes
  and progress bars as operating-system bitmaps that ignore any colour asked
  of them, so a themed window is `clam` or it is nothing.
* **`AMBER` and `RED` are the log pane's warning and error colours**, reused
  rather than re-picked. The freshness marker beside a registry and the
  `INCOMPLETE` line in the log then mean the same thing in the same colour.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

from .. import settings

log = logging.getLogger(__name__)

# --- the palette ----------------------------------------------------------
#
# Six colours. The subject is a ledger of what is stored and how old it is, so
# the window is paper, hairlines and ink, and the only saturated colour is on
# the one control the business presses every day.

INK = "#14181C"
PAPER = "#F5F6F7"
CARD = "#FFFFFF"
HAIRLINE = "#D7DBDE"
MUTED = "#6B7378"
FAINT = "#9AA2A7"
TROUGH = "#E7EAEC"

PETROL = "#0F5257"
PETROL_LIGHT = "#166B72"
PETROL_DARK = "#0A3B3F"
ON_INK = "#E8EDEE"
ON_INK_MUTED = "#93A0A3"

#: Already the log pane's tags. Reused so one colour means one thing.
AMBER = "#B06000"
RED = "#B00020"

# --- fonts ----------------------------------------------------------------
#
# System families only. A bundled typeface would be weight in an installer that
# already fights SmartScreen, and Windows ships everything needed here.

DISPLAY_FAMILIES = ("Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI")
BODY_FAMILIES = ("Segoe UI Variable Text", "Segoe UI", "Helvetica")
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "Courier New")


@dataclass(frozen=True)
class Fonts:
    display: tuple
    eyebrow: tuple
    body: tuple
    strong: tuple
    small: tuple
    mono: tuple
    mono_small: tuple


@dataclass(frozen=True)
class Theme:
    """What `app.py` needs to lay a window out. Colours are module constants."""

    fonts: Fonts
    scale: float

    def px(self, value: int) -> int:
        """A pixel measurement at the screen's scale.

        Fonts are in points and Tk scales those itself; anything given in
        pixels — a pane's height, a pad — does not scale unless it is asked to,
        and a 12 px gutter on a 4K screen is a hairline.
        """
        return max(1, int(round(value * self.scale)))


# --- the calls that come before the window --------------------------------


def enable_dpi_awareness() -> None:
    """Tell Windows this process draws at the screen's real resolution.

    System-aware rather than per-monitor: per-monitor also requires handling
    `WM_DPICHANGED`, which Tk does not, so dragging between two screens of
    different scaling would leave a window sized for the wrong one. System
    awareness is correct on the machine's primary screen and merely imperfect
    elsewhere, which is the better failure.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:  # noqa: BLE001 - a blurry window still works
        log.debug("Could not set DPI awareness: %s", exc)


def set_icon(root: tk.Tk) -> None:
    """The window's icon, if it is in the bundle."""
    try:
        icon = settings.APP_ICON
        if icon.exists():
            root.iconbitmap(default=str(icon))
    except Exception as exc:  # noqa: BLE001 - the default icon is not fatal
        log.debug("Could not set the window icon: %s", exc)


# --- building the theme ---------------------------------------------------


def _family(candidates: tuple[str, ...], fallback: str) -> str:
    try:
        available = {name.lower() for name in tkfont.families()}
    except tk.TclError:  # pragma: no cover - needs a broken Tk
        return fallback
    for name in candidates:
        if name.lower() in available:
            return name
    return fallback


def _scale(root: tk.Tk) -> float:
    """Screen DPI as a multiplier on the 96-DPI design, and Tk told to match."""
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except tk.TclError:  # pragma: no cover - needs a broken Tk
        return 1.0
    if dpi <= 0:
        return 1.0
    root.tk.call("tk", "scaling", dpi / 72.0)
    return dpi / 96.0


def apply(root: tk.Tk) -> Theme:
    """Set the theme up on `root` and return what the layout needs."""
    scale = _scale(root)
    display = _family(DISPLAY_FAMILIES, "TkDefaultFont")
    body = _family(BODY_FAMILIES, "TkDefaultFont")
    mono = _family(MONO_FAMILIES, "TkFixedFont")

    fonts = Fonts(
        display=(display, 15, "bold"),
        eyebrow=(body, 8, "bold"),
        body=(body, 10),
        strong=(body, 10, "bold"),
        small=(body, 9),
        mono=(mono, 10),
        mono_small=(mono, 9),
    )

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - needs a Tk without clam
        log.debug("clam is unavailable; keeping %s", style.theme_use())
    _configure(style, fonts, scale)
    root.configure(background=PAPER)
    return Theme(fonts=fonts, scale=scale)


def _configure(style: ttk.Style, fonts: Fonts, scale: float) -> None:
    def px(value: int) -> int:
        return max(1, int(round(value * scale)))

    style.configure(
        ".", background=PAPER, foreground=INK, font=fonts.body, focuscolor=PETROL
    )
    style.configure("TFrame", background=PAPER)
    style.configure("TLabel", background=PAPER, foreground=INK)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Eyebrow.TLabel", foreground=MUTED, font=fonts.eyebrow)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=INK)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED,
                    font=fonts.small)
    style.configure("CardFaint.TLabel", background=CARD, foreground=FAINT,
                    font=fonts.small)
    # Mono, and a size below the labels beside it: figures in a column want to
    # line up, not to shout over the names they belong to.
    style.configure("Count.TLabel", background=CARD, foreground=INK,
                    font=fonts.mono_small)
    style.configure("CountFaint.TLabel", background=CARD, foreground=FAINT,
                    font=fonts.mono_small)
    style.configure("Hairline.TFrame", background=HAIRLINE)
    style.configure("Header.TFrame", background=INK)
    style.configure("Header.TLabel", background=INK, foreground=ON_INK,
                    font=fonts.display)
    style.configure("HeaderMuted.TLabel", background=INK, foreground=ON_INK_MUTED,
                    font=fonts.small)

    # The one saturated control. Everything else is quiet so this is the thing
    # the eye lands on, which is correct: it is the button the business presses.
    style.configure(
        "Accent.TButton",
        background=PETROL,
        foreground="#FFFFFF",
        bordercolor=PETROL,
        lightcolor=PETROL,
        darkcolor=PETROL,
        focuscolor="#FFFFFF",
        borderwidth=0,
        padding=(px(18), px(9)),
        font=fonts.strong,
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", TROUGH), ("pressed", PETROL_DARK),
                    ("active", PETROL_LIGHT)],
        foreground=[("disabled", FAINT)],
        lightcolor=[("pressed", PETROL_DARK), ("active", PETROL_LIGHT)],
        darkcolor=[("pressed", PETROL_DARK), ("active", PETROL_LIGHT)],
    )

    style.configure(
        "Secondary.TButton",
        background=CARD,
        foreground=INK,
        bordercolor=HAIRLINE,
        lightcolor=CARD,
        darkcolor=CARD,
        borderwidth=1,
        padding=(px(14), px(9)),
        font=fonts.body,
    )
    style.map(
        "Secondary.TButton",
        background=[("disabled", PAPER), ("pressed", TROUGH), ("active", TROUGH)],
        foreground=[("disabled", FAINT)],
        bordercolor=[("active", MUTED)],
    )

    style.configure(
        "Link.TButton",
        background=PAPER,
        foreground=MUTED,
        borderwidth=0,
        padding=(px(4), px(2)),
        font=fonts.small,
        relief="flat",
    )
    style.map(
        "Link.TButton",
        background=[("active", PAPER), ("pressed", PAPER)],
        foreground=[("disabled", FAINT), ("active", PETROL)],
    )

    # The tick is a glyph in the label, and clam's own indicator is taken out
    # of the layout, for two reasons. `indicatorsize` is a fixed pixel count
    # that does not follow the screen, so at 150% the box is a third of the
    # size it looks in a mockup; and clam draws a *cross* in a ticked box,
    # which is the wrong word for "selected" in the one list whose whole job
    # is to say what is selected. A glyph scales with the font and says it.
    try:
        style.layout(
            "Ledger.TCheckbutton",
            [(
                "Checkbutton.padding",
                {"sticky": "nswe", "children": [(
                    "Checkbutton.focus",
                    {"side": "left", "sticky": "w", "children": [
                        ("Checkbutton.label", {"sticky": "nswe"})
                    ]},
                )]},
            )],
        )
    except tk.TclError:  # pragma: no cover - a theme without those elements
        log.debug("Keeping the theme's own checkbox indicator")
    style.configure(
        "Ledger.TCheckbutton",
        background=CARD,
        foreground=INK,
        padding=(0, px(4)),
        font=fonts.body,
    )
    style.map(
        "Ledger.TCheckbutton",
        background=[("active", CARD), ("disabled", CARD)],
        foreground=[("!selected", MUTED), ("disabled", FAINT)],
    )

    for name, thickness, colour in (
        ("Overall.Horizontal.TProgressbar", px(3), PETROL),
        ("Row.Horizontal.TProgressbar", px(5), PETROL),
    ):
        style.configure(
            name,
            background=colour,
            troughcolor=TROUGH,
            bordercolor=TROUGH,
            lightcolor=colour,
            darkcolor=colour,
            borderwidth=0,
            thickness=thickness,
        )
    style.configure("Overall.Horizontal.TProgressbar", troughcolor=INK,
                    bordercolor=INK)

    style.configure(
        "TEntry", fieldbackground=CARD, bordercolor=HAIRLINE, lightcolor=CARD,
        darkcolor=CARD, foreground=INK, padding=px(6),
    )
    style.configure("Vertical.TScrollbar", background=TROUGH, troughcolor=PAPER,
                    bordercolor=PAPER, arrowcolor=MUTED)
