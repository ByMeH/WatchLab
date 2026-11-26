"""Events page: paginated JSONL tables with filters (Tkinter-free loading)."""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from . import services, theme
from .services import EVENT_KIND_LABEL, EVENT_SOURCES, EventFilter

COLUMNS = ("type", "revision", "key", "sequence", "request_id", "timestamp", "run_id")
HEADINGS = ("type", "revision", "key", "sequence", "request_id", "timestamp", "run_id")
WIDTHS = (90, 80, 300, 70, 210, 190, 210)


class EventsPage:
    def __init__(self, app) -> None:
        self.app = app
        self.frame = tk.Frame(app.content, bg=theme.BG_ROOT)
        self.fonts = app.fonts
        self.suite_var = tk.StringVar()
        self.kind_var = tk.StringVar(value="producer")
        self.type_var = tk.StringVar(value="all")
        self.revision_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.run_var = tk.StringVar()
        self.size_var = tk.StringVar(value="100")
        self.page = 0
        self.total = 0
        self.pages = 1
        self._load_seq = 0

        self._build_controls()
        self._build_table()

    # ------------------------------------------------------------- controls
    def _build_controls(self) -> None:
        bar = tk.Frame(self.frame, bg=theme.BG_ROOT)
        bar.pack(fill="x", padx=24, pady=(20, 6))

        theme.label(bar, "suite", font_key="small", fg=theme.FG_DIM,
                    bg=theme.BG_ROOT).pack(side="left", padx=(0, 4))
        self.suite_box = ttk.Combobox(bar, textvariable=self.suite_var, width=22,
                                      state="readonly", font=self.fonts.small)
        self.suite_box.pack(side="left", padx=(0, 12))
        self.suite_box.bind("<<ComboboxSelected>>", lambda _e: self._on_suite_change())

        theme.label(bar, "records", font_key="small", fg=theme.FG_DIM,
                    bg=theme.BG_ROOT).pack(side="left", padx=(0, 4))
        self.kind_box = ttk.Combobox(bar, textvariable=self.kind_var, width=24,
                                     state="readonly", font=self.fonts.small,
                                     values=tuple(EVENT_KIND_LABEL.values()))
        self.kind_box.pack(side="left", padx=(0, 12))
        self.kind_box.current(0)
        self.kind_box.bind("<<ComboboxSelected>>", lambda _e: self._on_filter_change())

        theme.label(bar, "type", font_key="small", fg=theme.FG_DIM,
                    bg=theme.BG_ROOT).pack(side="left", padx=(0, 4))
        self.type_box = ttk.Combobox(bar, textvariable=self.type_var, width=14,
                                     state="readonly", font=self.fonts.small)
        self.type_box.pack(side="left", padx=(0, 12))
        self.type_box.bind("<<ComboboxSelected>>", lambda _e: self._on_filter_change())

        for caption, var, width in (("revision", self.revision_var, 7),
                                    ("key \u2282", self.key_var, 22),
                                    ("run_id \u2282", self.run_var, 22)):
            cell = tk.Frame(bar, bg=theme.BG_ROOT)
            cell.pack(side="left", padx=(0, 10))
            theme.label(cell, caption, font_key="small", fg=theme.FG_DIM,
                        bg=theme.BG_ROOT).pack(anchor="w")
            theme.entry(cell, self.fonts, textvariable=var, width=width).pack(fill="x")

        theme.button(bar, "Apply", self._on_filter_change, kind="secondary",
                     fonts=self.fonts).pack(side="left", padx=(0, 8))

        bar2 = tk.Frame(self.frame, bg=theme.BG_ROOT)
        bar2.pack(fill="x", padx=24, pady=(0, 6))
        theme.label(bar2, "page size", font_key="small", fg=theme.FG_DIM,
                    bg=theme.BG_ROOT).pack(side="left", padx=(0, 4))
        self.size_box = ttk.Combobox(bar2, textvariable=self.size_var, width=6,
                                     state="readonly", font=self.fonts.small,
                                     values=("50", "100", "250", "500"))
        self.size_box.pack(side="left", padx=(0, 12))
        self.size_box.current(1)
        self.size_box.bind("<<ComboboxSelected>>", lambda _e: self._on_filter_change())
        self.btn_prev = theme.button(bar2, "\u2190 prev", self._prev_page,
                                     kind="ghost", fonts=self.fonts)
        self.btn_prev.pack(side="left", padx=(0, 4))
        self.btn_next = theme.button(bar2, "next \u2192", self._next_page,
                                     kind="ghost", fonts=self.fonts)
        self.btn_next.pack(side="left", padx=(0, 10))
        self.status_label = theme.label(bar2, "", font_key="small", fg=theme.FG_DIM,
                                        bg=theme.BG_ROOT)
        self.status_label.pack(side="left")

    def _build_table(self) -> None:
        card = theme.card(self.frame, padding=0)
        card.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        inner = card.winfo_children()[0]
        tree = ttk.Treeview(inner, columns=COLUMNS, show="headings", selectmode="browse")
        for col, heading, width in zip(COLUMNS, HEADINGS, WIDTHS):
            tree.heading(col, text=heading)
            tree.column(col, width=width, anchor="w",
                        stretch=(col in ("key", "request_id", "run_id")))
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(inner, orient="horizontal", command=tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)
        tree.tag_configure("producer", foreground=theme.FG)
        tree.tag_configure("watch", foreground=theme.ACCENT_TEXT)
        tree.tag_configure("recover", foreground=theme.RUNNING)
        self.tree = tree

    # ------------------------------------------------------------- data
    def on_show(self) -> None:
        self._reload_suites()

    def _reload_suites(self) -> None:
        suites = services.completed_series(services.AUTOMATED_DIR)
        excluded = [s for s, _r in services.excluded_series(services.AUTOMATED_DIR)]
        all_suites = suites + excluded
        current = self.suite_var.get()
        self.suite_box.configure(values=all_suites)
        if current not in all_suites:
            if all_suites:
                self.suite_var.set(all_suites[-1] if "auto-20261119-233338" not in all_suites
                                   else "auto-20261119-233338")
            else:
                self.suite_var.set("")
        self._on_suite_change()

    def _on_suite_change(self) -> None:
        suite = self.suite_var.get()
        types = ["all"]
        if suite:
            for kind in EVENT_SOURCES:
                types.extend(t for t in services.event_types(services.AUTOMATED_DIR, suite, kind)
                             if t not in types)
        self.type_box.configure(values=tuple(types))
        if self.type_var.get() not in types:
            self.type_var.set("all")
        self._on_filter_change()

    def _on_filter_change(self) -> None:
        self.page = 0
        self._load()

    def _current_filter(self) -> EventFilter:
        return EventFilter(
            event_type="" if self.type_var.get() == "all" else self.type_var.get(),
            revision=self.revision_var.get().strip(),
            key_substring=self.key_var.get().strip(),
            run_id=self.run_var.get().strip(),
        )

    def _kind(self) -> str:
        for key, label in EVENT_KIND_LABEL.items():
            if label == self.kind_var.get():
                return key
        return "producer"

    def _load(self) -> None:
        suite = self.suite_var.get()
        if not suite:
            self.tree.delete(*self.tree.get_children())
            self.status_label.configure(text="no experiment series found")
            return
        self._load_seq += 1
        seq = self._load_seq
        kind = self._kind()
        page = self.page
        size = int(self.size_var.get())
        filter_ = self._current_filter()
        self.status_label.configure(text="loading\u2026")
        threading.Thread(
            target=self._worker, daemon=True,
            args=(seq, suite, kind, page, size, filter_)).start()

    def _worker(self, seq: int, suite: str, kind: str, page: int, size: int,
                filter_: EventFilter) -> None:
        records, total, pages = services.load_events_page(
            services.AUTOMATED_DIR, suite, kind, page, size, filter_)
        self.app.ui_call(lambda: self._apply_rows(seq, suite, kind, page, size,
                                                  filter_, records, total, pages))

    def _apply_rows(self, seq: int, suite: str, kind: str, page: int, size: int,
                    filter_: EventFilter, records, total: int, pages: int) -> None:
        if seq != self._load_seq:
            return  # a newer request superseded this one
        self.total = total
        self.pages = max(1, pages)
        self.tree.delete(*self.tree.get_children())
        for index, record in enumerate(records, start=page * size + 1):
            row = record.as_row()
            self.tree.insert("", "end", iid=str(index), values=row, tags=(kind,))
        source_files = ", ".join(EVENT_SOURCES[kind])
        shown = f"{page * size + 1}\u2013{page * size + len(records)}" if records else "0"
        self.status_label.configure(
            text=f"{suite} \u00b7 {EVENT_KIND_LABEL[kind]} \u00b7 matched {total:,} \u00b7 "
                 f"showing {shown} \u00b7 files: {source_files}")
        self.btn_prev.configure(state="normal" if page > 0 else "disabled")
        self.btn_next.configure(state="normal" if page + 1 < self.pages else "disabled")

    def _prev_page(self) -> None:
        if self.page > 0:
            self.page -= 1
            self._load()

    def _next_page(self) -> None:
        if self.page + 1 < self.pages:
            self.page += 1
            self._load()

    def on_hide(self) -> None:
        pass
