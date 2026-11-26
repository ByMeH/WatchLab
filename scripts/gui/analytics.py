"""Analytics page: reports/analysis tables, charts, metric definitions."""
from __future__ import annotations

import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

from . import services, theme

STAT_COLUMNS = ("events", "scenario", "metric", "n", "mean", "median", "stdev", "min", "max")


class AnalyticsPage:
    def __init__(self, app) -> None:
        self.app = app
        self.frame = tk.Frame(app.content, bg=theme.BG_ROOT)
        self.fonts = app.fonts
        self._build_header()
        self._build_body()
        self._build_definitions()
        self.refresh()

    def _build_header(self) -> None:
        bar = tk.Frame(self.frame, bg=theme.BG_ROOT)
        bar.pack(fill="x", padx=24, pady=(20, 8))
        theme.label(bar, "Analytics", fg=theme.FG, bg=theme.BG_ROOT,
                    font_key="heading").pack(side="left")
        self.manifest_label = theme.label(bar, "", font_key="small", fg=theme.FG_DIM,
                                          bg=theme.BG_ROOT)
        self.manifest_label.pack(side="left", padx=16)
        theme.button(bar, "\u21bb  Recalculate", self._recalculate, kind="secondary",
                     fonts=self.fonts).pack(side="right")
        theme.label(self.frame,
                    "Recalculate runs: python scripts/analyze_results.py "
                    "(aggregates existing summaries; never starts experiments).",
                    font_key="small", fg=theme.FG_FAINT, bg=theme.BG_ROOT).pack(
            fill="x", padx=24, pady=(0, 8))

    def _build_body(self) -> None:
        body = tk.Frame(self.frame, bg=theme.BG_ROOT)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 8))
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        # left: charts
        charts_card = theme.card(body, padding=10)
        charts_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        inner = charts_card.winfo_children()[0]
        theme.label(inner, "CHARTS (reports/analysis)", font_key="card_label",
                    fg=theme.FG_DIM).pack(anchor="w", pady=(0, 6))
        self.charts_holder = tk.Frame(inner, bg=theme.BG_CARD)
        self.charts_holder.pack(fill="both", expand=True)

        # right: stats table
        stats_card = theme.card(body, padding=0)
        stats_card.grid(row=0, column=1, sticky="nsew")
        inner = stats_card.winfo_children()[0]
        head = tk.Frame(inner, bg=theme.BG_CARD)
        head.pack(fill="x", padx=8, pady=(8, 2))
        theme.label(head, "GROUP STATISTICS", font_key="card_label",
                    fg=theme.FG_DIM).pack(side="left")
        self.metric_var = tk.StringVar(value="all")
        self.metric_box = ttk.Combobox(head, textvariable=self.metric_var, width=22,
                                       state="readonly", font=self.fonts.small)
        self.metric_box.pack(side="right", padx=6)
        self.metric_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        table = tk.Frame(inner, bg=theme.BG_CARD)
        table.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        tree = ttk.Treeview(table, columns=STAT_COLUMNS, show="headings",
                            selectmode="browse")
        for col, width in zip(STAT_COLUMNS, (60, 150, 170, 40, 80, 80, 80, 70, 70)):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=vsb.set)
        table.grid_rowconfigure(0, weight=1)
        table.grid_columnconfigure(0, weight=1)
        self.stats_tree = tree

    def _build_definitions(self) -> None:
        card = theme.card(self.frame, padding=0)
        card.pack(fill="both", expand=False, padx=24, pady=(0, 18))
        inner = card.winfo_children()[0]
        head = tk.Frame(inner, bg=theme.BG_CARD)
        head.pack(fill="x", padx=8, pady=(8, 2))
        theme.label(head, "METRIC DEFINITIONS (reports/analysis/README.md)",
                    font_key="card_label", fg=theme.FG_DIM).pack(side="left")
        self.definitions = tk.Text(inner, bg=theme.BG_INPUT, fg=theme.FG, bd=0,
                                   font=self.fonts.mono_small, height=9, wrap="word",
                                   state="disabled", padx=8, pady=6)
        self.definitions.pack(fill="x", expand=True, padx=8, pady=(0, 8))
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.definitions.yview)
        sb.pack(side="right", fill="y", padx=(0, 8))
        self.definitions.configure(yscrollcommand=sb.set)

    # ------------------------------------------------------------- data
    def refresh(self) -> None:
        self.manifest_label.configure(
            text=services.analysis_manifest_summary())
        self._load_charts()
        self._load_stats()
        self._load_definitions()

    def _load_charts(self) -> None:
        holder = self.charts_holder
        for child in holder.winfo_children():
            child.destroy()
        charts = services.analysis_charts()
        if not charts:
            theme.label(holder, "No charts found in reports/analysis/. "
                        "Run \u201cRecalculate\u201d to generate them.",
                        font_key="small", fg=theme.FG_FAINT, bg=theme.BG_CARD,
                        wraplength=380).pack(anchor="w", pady=8)
            return
        for path in charts:
            row = tk.Frame(holder, bg=theme.BG_CARD)
            row.pack(fill="x", pady=3)
            try:
                photo = tk.PhotoImage(file=str(path))
                factor = max(1, photo.width() // 330)
                if factor > 1:
                    photo = photo.subsample(factor, factor)
            except tk.TclError:
                photo = None
            name = tk.Label(row, text=path.name, fg=theme.ACCENT_TEXT,
                            bg=theme.BG_CARD, font=self.fonts.small, cursor="hand2")
            name.pack(side="left")
            name.bind("<Button-1>", lambda _e, p=path: self._open_chart(p))
            if photo is not None:
                thumb = tk.Label(row, image=photo, bg=theme.BG_CARD, cursor="hand2")
                thumb.image = photo  # keep a reference
                thumb.pack(side="right", padx=6)
                thumb.bind("<Button-1>", lambda _e, p=path: self._open_chart(p))

    def _open_chart(self, path) -> None:
        try:
            subprocess.Popen(["xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            self.app.set_status(f"cannot open {path.name}: no xdg-open")

    def _load_definitions(self) -> None:
        text = services.read_analysis_readme()
        self.definitions.configure(state="normal")
        self.definitions.delete("1.0", "end")
        if text.strip():
            self.definitions.insert("1.0", text)
        else:
            self.definitions.insert("1.0",
                                    "reports/analysis/README.md not found; run Recalculate.")
        self.definitions.configure(state="disabled")

    def _load_stats(self) -> None:
        rows = services.load_group_stats()
        metrics = ["all"]
        for row in rows:
            if row.get("metric") and row["metric"] not in metrics:
                metrics.append(row["metric"])
        self.metric_box.configure(values=tuple(metrics))
        if self.metric_var.get() not in metrics:
            self.metric_var.set("all")
        chosen = self.metric_var.get()
        self.stats_tree.delete(*self.stats_tree.get_children())
        for index, row in enumerate(rows):
            if chosen != "all" and row.get("metric") != chosen:
                continue
            self.stats_tree.insert("", "end", iid=str(index), values=tuple(
                row.get(key, "") for key in
                ("events_per_phase", "scenario", "metric", "n", "mean", "median",
                 "stdev_sample", "min", "max")))

    def _recalculate(self) -> None:
        self.app.run_process("analyze-results", [sys.executable, "scripts/analyze_results.py"])
        self.app.show_page("logs")

    def on_show(self) -> None:
        self.refresh()

    def on_hide(self) -> None:
        pass
