"""Dark theme for WatchLab Desktop: palette, fonts, ttk styles.

Graphite background, violet accents, minimalistic flat panels. All UI code
imports from here so pages stay consistent and easy to adjust.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

# --------------------------------------------------------------- palette
BG_ROOT = "#0e1013"        # window background (graphite)
BG_SIDEBAR = "#13151b"     # navigation panel
BG_CARD = "#181b22"        # cards / panels
BG_CARD_ALT = "#1d212a"    # hover / headers
BG_INPUT = "#12141a"       # entry / log background
BORDER = "#262c38"         # hairline borders
FG = "#e8eaf0"             # primary text
FG_DIM = "#8b93a7"         # secondary text
FG_FAINT = "#5b6272"       # disabled text

ACCENT = "#8b5cf6"         # violet accent
ACCENT_BRIGHT = "#a78bfa"  # hover / highlight
ACCENT_DIM = "#2a2440"     # dark violet fill
ACCENT_TEXT = "#c4b5fd"

OK = "#4ade80"             # success green
OK_DIM = "#153224"
WARN = "#fbbf24"           # warning amber
WARN_DIM = "#332a12"
FAIL = "#f87171"           # failure red
FAIL_DIM = "#33191b"
RUNNING = "#60a5fa"        # running blue

MONO_FALLBACKS = ("JetBrains Mono", "Cascadia Mono", "Consolas",
                  "DejaVu Sans Mono", "Courier New", "TkFixedFont")
SANS_FALLBACKS = ("Inter", "Segoe UI", "SF Pro Text", "DejaVu Sans",
                  "Helvetica", "TkDefaultFont")


class Fonts:
    """Resolved font families and sizes."""

    def __init__(self, root: tk.Misc) -> None:
        families = set(tkfont.families(root))
        sans = next((f for f in SANS_FALLBACKS if f in families), "TkDefaultFont")
        mono = next((f for f in MONO_FALLBACKS if f in families), "TkFixedFont")
        self.sans = sans
        self.mono = mono
        self.base = (sans, 10)
        self.small = (sans, 9)
        self.heading = (sans, 16, "bold")
        self.title = (sans, 20, "bold")
        self.card_value = (sans, 18, "bold")
        self.card_label = (sans, 9)
        self.nav = (sans, 11)
        self.mono = (mono, 10)
        self.mono_small = (mono, 9)
        self.table = (sans, 10)
        self.table_mono = (mono, 9)


def apply_theme(root: tk.Tk) -> Fonts:
    """Configure the root window, ttk styles and fonts. Returns resolved fonts."""
    root.configure(bg=BG_ROOT)
    fonts = Fonts(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - theme always present in Tk 8.6
        pass

    root.option_add("*Font", fonts.base)

    # ---------------------------------------------------------- treeview
    style.configure("Treeview", background=BG_CARD, fieldbackground=BG_CARD,
                    foreground=FG, rowheight=26, borderwidth=0,
                    font=fonts.table, relief="flat")
    style.configure("Treeview.Heading", background=BG_CARD_ALT,
                    foreground=FG_DIM, borderwidth=0, relief="flat",
                    font=fonts.small, padding=(8, 5))
    style.map("Treeview.Heading", background=[("active", BG_CARD_ALT)])
    style.map("Treeview", background=[("selected", ACCENT_DIM)],
              foreground=[("selected", FG)])
    style.configure("Treeview", arrowcolor=FG_DIM)

    # -------------------------------------------------------- scrollbars
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=BG_CARD_ALT,
                        troughcolor=BG_ROOT, bordercolor=BG_ROOT,
                        arrowcolor=FG_DIM, relief="flat", borderwidth=0)
        style.map(f"{orient}.TScrollbar",
                  background=[("active", ACCENT_DIM), ("pressed", ACCENT)])

    # ---------------------------------------------------------- combobox
    style.configure("TCombobox", fieldbackground=BG_INPUT, background=BG_CARD_ALT,
                    foreground=FG, arrowcolor=ACCENT_BRIGHT, borderwidth=0,
                    padding=(8, 5), selectbackground=ACCENT_DIM,
                    selectforeground=FG, relief="flat")
    style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)],
              foreground=[("readonly", FG)],
              selectbackground=[("readonly", ACCENT_DIM)],
              selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", BG_CARD_ALT)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DIM)
    root.option_add("*TCombobox*Listbox.selectForeground", FG)
    root.option_add("*TCombobox*Listbox.font", fonts.small)

    # ---------------------------------------------------------- progress
    style.configure("Accent.Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=BG_INPUT, borderwidth=0, thickness=4)
    style.configure("Slim.Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=BG_ROOT, borderwidth=0, thickness=2)

    return fonts


# --------------------------------------------------------------- widgets
def card(parent: tk.Misc, padding: int = 14) -> tk.Frame:
    """Flat dark card with a hairline border."""
    outer = tk.Frame(parent, bg=BORDER)
    inner = tk.Frame(outer, bg=BG_CARD, padx=padding, pady=padding)
    inner.pack(fill="both", expand=True)
    return outer


def label(parent: tk.Misc, text: str, *, font_key: str = "base",
          fg: str = FG, bg: str = BG_CARD, anchor: str = "w",
          justify: str = "left", wraplength: int = 0) -> tk.Label:
    fonts: Fonts = parent.winfo_toplevel().fonts  # type: ignore[attr-defined]
    return tk.Label(parent, text=text, fg=fg, bg=bg, font=getattr(fonts, font_key),
                    anchor=anchor, justify=justify,
                    wraplength=wraplength or 0, bd=0, highlightthickness=0)


def button(parent: tk.Misc, text: str, command, *, kind: str = "secondary",
           fonts: Fonts | None = None, padx: int = 14, pady: int = 7,
           state: str = "normal") -> tk.Button:
    """Flat modern button. kinds: primary | secondary | danger | ghost."""
    fonts = fonts or parent.winfo_toplevel().fonts  # type: ignore[attr-defined]
    variants = {
        "primary": (ACCENT, "#ffffff", ACCENT_BRIGHT, ACCENT_TEXT),
        "secondary": (ACCENT_DIM, ACCENT_TEXT, "#37305a", ACCENT_BRIGHT),
        "danger": (FAIL_DIM, FAIL, "#4b2226", FAIL),
        "ghost": (BG_CARD, FG_DIM, BG_CARD_ALT, FG),
    }
    bg, fg, hover, hoverfg = variants[kind]
    disabled_fg = FG_FAINT if kind != "ghost" else "#3a3f4c"
    btn = tk.Button(parent, text=text, command=command, font=fonts.nav,
                    bg=bg, fg=fg, activebackground=hover, activeforeground=hoverfg,
                    relief="flat", bd=0, padx=padx, pady=pady, cursor="hand2",
                    state=state, disabledforeground=disabled_fg,
                    highlightthickness=0, takefocus=0)
    def _enter(_event: object) -> None:
        if str(btn.cget("state")) == "normal":
            btn.configure(bg=hover)

    def _leave(_event: object) -> None:
        if str(btn.cget("state")) == "normal":
            btn.configure(bg=bg)

    btn.bind("<Enter>", _enter)
    btn.bind("<Leave>", _leave)
    return btn


def entry(parent: tk.Misc, fonts: Fonts, textvariable=None, width: int = 0,
          placeholder: str = "") -> tk.Entry:
    """Dark flat entry with violet focus ring."""
    ent = tk.Entry(parent, bg=BG_INPUT, fg=FG, insertbackground=ACCENT,
                   relief="flat", bd=0, font=fonts.base,
                   highlightthickness=1, highlightbackground=BORDER,
                   highlightcolor=ACCENT, textvariable=textvariable,
                   width=width, disabledforeground=FG_FAINT)
    if placeholder:
        _attach_placeholder(ent, placeholder)
    return ent


def _attach_placeholder(ent: tk.Entry, placeholder: str) -> None:
    ph_fg = FG_FAINT
    normal_fg = FG

    def show_ph() -> None:
        if not ent.get():
            ent.insert(0, placeholder)
            ent.configure(fg=ph_fg)

    def clear_ph(*_) -> None:
        if ent.get() == placeholder:
            ent.delete(0, "end")
            ent.configure(fg=normal_fg)

    ent.bind("<FocusIn>", clear_ph)
    ent.bind("<FocusOut>", show_ph)
    show_ph()


def status_dot(parent: tk.Misc, fonts: Fonts, color: str, text: str,
               bg: str = BG_CARD) -> tk.Frame:
    """Small colored dot + label, used for state indicators."""
    frame = tk.Frame(parent, bg=bg)
    tk.Label(frame, text="\u25cf", fg=color, bg=bg, font=fonts.base).pack(side="left")
    tk.Label(frame, text=" " + text, fg=FG, bg=bg, font=fonts.small).pack(side="left")
    return frame


def separator(parent: tk.Misc, bg: str = BORDER) -> tk.Frame:
    return tk.Frame(parent, bg=bg, height=1)
