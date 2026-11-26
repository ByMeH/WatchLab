"""Experiments page: table of existing series; no experiments are started here."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import services, theme

COLUMNS = ("suite", "events", "scenario", "observed", "missing", "dup", "unexpected", "status")
HEADINGS = ("suite_id", "events/phase", "scenario", "observed", "missing",
            "duplicates", "unexpected", "validation status")
WIDTHS = (170, 90, 160, 90, 80, 90, 90, 200)
STATUS_COLORS = {
    "ok": theme.OK,
    "anomalies": theme.FAIL,
    "no validation": theme.WARN,
    "excluded (incomplete)": theme.WARN,
    "excluded (not in selected nine)": theme.FG_FAINT,
}


class ExperimentsPage:
    def __init__(self, app) -> None:
        self.app = app
        self.frame = tk.Frame(app.content, bg=theme.BG_ROOT)
        self.fonts = app.fonts
        self._build_header()
        self._build_table()

    def _build_header(self) -> None:
        bar = tk.Frame(self.frame, bg=theme.BG_ROOT)
        bar.pack(fill="x", padx=24, pady=(20, 8))
        theme.label(bar, "Experiment series", fg=theme.FG, bg=theme.BG_ROOT,
                    font_key="heading").pack(side="left")
        theme.button(bar, "\u21bb  Refresh", self.refresh, kind="secondary",
                     fonts=self.fonts).pack(side="right")
        note = tk.Frame(self.frame, bg=theme.BG_ROOT)
        note.pack(fill="x", padx=24, pady=(0, 8))
        theme.label(note,
                    "Main sample: nine verified series (scripts/analyze_results.py::DEFAULT_SUITES). "
                    "auto-20261119-230935 is incomplete and shown as excluded; it is not part of "
                    "the statistics. The GUI never starts benchmark series.",
                    font_key="small", fg=theme.FG_FAINT, bg=theme.BG_ROOT,
                    wraplength=980).pack(anchor="w")
        self.summary_label = theme.label(note, "", font_key="small", fg=theme.FG_DIM,
                                         bg=theme.BG_ROOT)
        self.summary_label.pack(anchor="w", pady=(2, 0))

    def _build_table(self) -> None:
        card = theme.card(self.frame, padding=0)
        card.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        inner = card.winfo_children()[0]
        tree = ttk.Treeview(inner, columns=COLUMNS, show="headings", selectmode="browse")
        for col, heading, width in zip(COLUMNS, HEADINGS, WIDTHS):
            tree.heading(col, text=heading)
            tree.column(col, width=width, anchor="w", stretch=(col == "suite"))
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=vsb.set)
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)
        for status, color in STATUS_COLORS.items():
            tree.tag_configure(status, foreground=color)
        tree.tag_configure("excluded", foreground=theme.FG_FAINT)
        self.tree = tree
        self.refresh()

    def refresh(self) -> None:
        rows = services.build_suite_rows()
        completed = services.completed_series()
        selected = services.selected_series()
        verified = services.verified_events()
        self.summary_label.configure(
            text=f"completed series: {len(completed)} \u00b7 selected: {selected} \u00b7 "
                 f"verified events: {verified if verified is not None else 'n/a'} \u00b7 "
                 f"rows: {len(rows)}")
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(rows):
            tag = row.status if row.status in STATUS_COLORS else "excluded"
            self.tree.insert("", "end", iid=str(index), tags=(tag,), values=(
                row.suite_id, row.events_per_phase, row.scenario,
                row.observed_events, row.missing_events, row.duplicates,
                row.unexpected_events, row.status))

    def on_show(self) -> None:
        self.refresh()

    def on_hide(self) -> None:
        pass
