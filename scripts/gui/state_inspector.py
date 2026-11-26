"""Recovery State Inspector: read-only view of the recovered local KV state.

Opened from the Recovery page after demo.py produced a state file. The dialog
reads only the state.json saved by the Go recover command — it never queries
etcd. Parsing runs in the service layer on a worker thread; the result is
applied on the UI thread via app.ui_call, so Tkinter is never touched from a
background thread.

Semantics kept explicit (see docs/GUI_GUIDE.md):
  snapshot_revision (R) - revision of the last Range snapshot;
  last_revision (L)     - checkpoint of the last fully applied revision;
  mod_revision          - last-change revision of one key.
The key count is the size of the current state map, not a historical event
count; snapshot_revision is never shown as the current state revision.
"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import ttk

from . import services, theme
from .services import (SCOPE_ALL, SCOPE_CHANGED_AFTER_SNAPSHOT,
                       SCOPE_FROM_SNAPSHOT, SCOPE_LABELS,
                       RecoveryState, RecoveryStateError, StateKV)

VALUE_PREVIEW_LIMIT = 96  # table shows at most this many characters per value
MAX_ROWS = 500            # hard cap; the counter always shows the true total
METRICS = (
    ("snapshot", "SNAPSHOT REVISION (R)"),
    ("last", "LAST APPLIED REVISION (L)"),
    ("keys", "CURRENT KEYS"),
    ("bytes", "DATA, KEY+VALUE BYTES"),
)


def _shorten(text: str, limit: int) -> str:
    """One-line truncated preview for table cells."""
    text = text.replace("\n", "\\n").replace("\r", "")
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class StateInspectorDialog:
    """Toplevel window: state.json metrics plus a filterable key table."""

    def __init__(self, app, path: str) -> None:
        self.app = app
        self.fonts = app.fonts
        self.path = path
        self.state: RecoveryState | None = None
        self._load_seq = 0
        self._row_kv: dict[str, StateKV] = {}

        self.window = tk.Toplevel(app)
        self.window.title("Recovery State Inspector \u2014 WatchLab")
        self.window.configure(bg=theme.BG_ROOT)
        self.window.geometry("1060x660")
        self.window.minsize(880, 500)
        self.window.transient(app)
        self.window.fonts = app.fonts  # theme.label resolves fonts via winfo_toplevel
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self.refresh()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        window = self.window

        head = tk.Frame(window, bg=theme.BG_ROOT)
        head.pack(fill="x", padx=24, pady=(16, 2))
        head_left = tk.Frame(head, bg=theme.BG_ROOT)
        head_left.pack(side="left", fill="x", expand=True)
        theme.label(head_left, "RECOVERY STATE INSPECTOR", font_key="card_label",
                    fg=theme.FG_DIM, bg=theme.BG_ROOT).pack(anchor="w")
        self.path_label = theme.label(head_left, self.path, font_key="small",
                                      fg=theme.FG_FAINT, bg=theme.BG_ROOT,
                                      wraplength=800)
        self.path_label.pack(anchor="w")
        self.btn_refresh = theme.button(head, "\u21bb  Refresh", self.refresh,
                                        kind="secondary", fonts=self.fonts)
        self.btn_refresh.pack(side="right")

        # error / notice banner (hidden unless something is wrong)
        self.banner = tk.Label(window, text="", fg=theme.WARN, bg=theme.WARN_DIM,
                               font=self.fonts.small, justify="left", anchor="w",
                               wraplength=960, padx=12, pady=8, bd=0,
                               highlightthickness=0)

        self.metrics_card = theme.card(window, padding=12)
        metrics = self.metrics_card
        metrics.pack(fill="x", padx=24, pady=(6, 4))
        inner = metrics.winfo_children()[0]
        self.metric_cells: dict[str, tk.Label] = {}
        for column, (key, caption) in enumerate(METRICS):
            inner.grid_columnconfigure(column, weight=1)
            cell = tk.Frame(inner, bg=theme.BG_CARD)
            cell.grid(row=0, column=column, sticky="ew", padx=(0, 0 if column == 3 else 20))
            theme.label(cell, caption, font_key="card_label",
                        fg=theme.FG_DIM).pack(anchor="w")
            value = theme.label(cell, "\u2014", font_key="card_value", fg=theme.FG)
            value.pack(anchor="w")
            self.metric_cells[key] = value
        self.delta_label = theme.label(inner, "", font_key="small", fg=theme.FG_DIM,
                                       wraplength=940, justify="left")
        self.delta_label.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        filter_bar = tk.Frame(window, bg=theme.BG_ROOT)
        filter_bar.pack(fill="x", padx=24, pady=(0, 6))
        theme.label(filter_bar, "key \u2282", font_key="small", fg=theme.FG_DIM,
                    bg=theme.BG_ROOT).pack(side="left", padx=(0, 4))
        self.search_var = tk.StringVar()
        self.search_entry = theme.entry(filter_bar, self.fonts,
                                        textvariable=self.search_var, width=30)
        self.search_entry.pack(side="left", padx=(0, 10))
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.scope_var = tk.StringVar(value=SCOPE_LABELS[SCOPE_ALL])
        self.scope_box = ttk.Combobox(filter_bar, textvariable=self.scope_var, width=38,
                                      state="readonly", font=self.fonts.small,
                                      values=tuple(SCOPE_LABELS[s] for s in (
                                          SCOPE_ALL, SCOPE_CHANGED_AFTER_SNAPSHOT,
                                          SCOPE_FROM_SNAPSHOT)))
        self.scope_box.pack(side="left", padx=(0, 10))
        self.scope_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())
        theme.button(filter_bar, "Apply", self._apply_filter, kind="secondary",
                     fonts=self.fonts).pack(side="left", padx=(0, 10))
        theme.button(filter_bar, "View full value", self._show_selected_value,
                     kind="ghost", fonts=self.fonts).pack(side="left")
        self.count_label = theme.label(filter_bar, "", font_key="small",
                                       fg=theme.FG_DIM, bg=theme.BG_ROOT)
        self.count_label.pack(side="right")

        table_card = theme.card(window, padding=0)
        table_card.pack(fill="both", expand=True, padx=24, pady=(0, 6))
        inner = table_card.winfo_children()[0]
        columns = ("key", "mod", "value")
        tree = ttk.Treeview(inner, columns=columns, show="headings",
                            selectmode="browse")
        tree.heading("key", text="key")
        tree.heading("mod", text="mod_revision")
        tree.heading("value", text="value (truncated)")
        tree.column("key", width=430, anchor="w", stretch=True)
        tree.column("mod", width=110, anchor="e", stretch=False)
        tree.column("value", width=420, anchor="w", stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(inner, orient="horizontal", command=tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)
        tree.tag_configure("changed", foreground=theme.ACCENT_TEXT)
        tree.tag_configure("snapshot", foreground=theme.FG)
        tree.bind("<Double-1>", lambda _e: self._show_selected_value())
        self.tree = tree

        foot = tk.Frame(window, bg=theme.BG_ROOT)
        foot.pack(fill="x", padx=24, pady=(0, 12))
        theme.label(foot,
                    "snapshot_revision (R) \u2014 revision of the last Range snapshot \u00b7 "
                    "last_revision (L) \u2014 checkpoint of the last applied revision \u00b7 "
                    "mod_revision \u2014 last-change revision of this key. The key count is "
                    "the size of the current state map, not the number of historical "
                    "events. data_bytes is the sum of key+value UTF-8 lengths, not the "
                    "on-disk JSON size. The saved state file is read as is; etcd is not "
                    "queried.",
                    font_key="small", fg=theme.FG_FAINT, bg=theme.BG_ROOT,
                    wraplength=980, justify="left").pack(anchor="w")

    # ------------------------------------------------------------- loading
    def refresh(self) -> None:
        """Re-read the state file on a worker thread; UI updated via ui_call."""
        self._load_seq += 1
        seq = self._load_seq
        path = self.path
        self.btn_refresh.configure(state="disabled")
        self.count_label.configure(text="loading\u2026")
        self.banner.pack_forget()
        threading.Thread(target=self._worker, daemon=True, args=(seq, path)).start()

    def _worker(self, seq: int, path: str) -> None:
        try:
            state = services.load_recovery_state(path)
        except RecoveryStateError as exc:
            error = exc  # `as exc` is deleted after the except block; keep it
            self.app.ui_call(lambda: self._apply_error(seq, error))
            return
        self.app.ui_call(lambda: self._apply_state(seq, state))

    def _alive(self, seq: int) -> bool:
        """The load result still matters: same request and window not closed."""
        if seq != self._load_seq:
            return False
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def _apply_state(self, seq: int, state: RecoveryState) -> None:
        if not self._alive(seq):
            return
        self.state = state
        self.btn_refresh.configure(state="normal")
        self.banner.pack_forget()
        file_note = f"  \u00b7  {state.file_size_bytes:,} bytes on disk"
        if state.updated_at:
            file_note += f"  \u00b7  updated_at {state.updated_at}"
        self.path_label.configure(text=state.path + file_note)
        self.metric_cells["snapshot"].configure(
            text="\u2014" if state.snapshot_revision is None else str(state.snapshot_revision))
        self.metric_cells["last"].configure(
            text="\u2014" if state.last_revision is None else str(state.last_revision))
        self.metric_cells["keys"].configure(text=f"{state.key_count:,}")
        self.metric_cells["bytes"].configure(
            text="\u2014 (not recorded)" if state.data_bytes is None
            else f"{state.data_bytes:,}")
        self._show_delta(state)
        self._apply_filter()

    @staticmethod
    def _delta_text(state: RecoveryState) -> tuple[str, str]:
        """(text, color) explaining the snapshot/checkpoint relation."""
        delta = state.checkpoint_delta
        if delta is None or state.snapshot_revision is None or state.last_revision is None:
            return ("Revisions are not recorded in this state file; the "
                    "snapshot/checkpoint relation is unknown.", theme.WARN)
        changed = len(state.changed_after_snapshot)
        if delta > 0:
            return (f"Watch events were applied after the snapshot: the checkpoint "
                    f"advanced by {delta} revision(s) "
                    f"(L {state.last_revision} \u2212 R {state.snapshot_revision}); "
                    f"{changed} of {state.key_count:,} key(s) have mod_revision > R.",
                    theme.ACCENT_TEXT)
        if delta == 0:
            return (f"No watch events applied after the snapshot (L = R = "
                    f"{state.snapshot_revision}); the state map is the snapshot "
                    f"as saved.", theme.FG_DIM)
        return (f"L < R ({state.last_revision} < {state.snapshot_revision}): "
                f"unexpected checkpoint relation; the file may be from another run.",
                theme.WARN)

    def _show_delta(self, state: RecoveryState) -> None:
        text, color = self._delta_text(state)
        self.delta_label.configure(text=text, fg=color)

    def _apply_error(self, seq: int, exc: RecoveryStateError) -> None:
        if not self._alive(seq):
            return
        self.state = None
        self._row_kv = {}
        self.btn_refresh.configure(state="normal")
        self.path_label.configure(text=self.path)
        for cell in self.metric_cells.values():
            cell.configure(text="\u2014")
        self.delta_label.configure(text="", fg=theme.FG_DIM)
        self.count_label.configure(text="")
        self.tree.delete(*self.tree.get_children())
        if exc.reason == "missing":
            text = (f"state.json not found: {self.path}\n"
                    f"Run the demo first (Recovery page, \u25b6 Run demo) \u2014 after a "
                    f"finished run its recovered state becomes inspectable here.")
            color = theme.WARN
        elif exc.reason == "corrupted":
            text = f"state.json is corrupted and cannot be parsed:\n{exc}"
            color = theme.FAIL
        elif exc.reason == "unreadable":
            text = f"state.json cannot be read:\n{exc}"
            color = theme.FAIL
        else:
            text = f"unexpected state.json structure:\n{exc}"
            color = theme.FAIL
        self.banner.configure(text=text, fg=color)
        self.banner.pack(fill="x", padx=24, pady=(2, 6), before=self.metrics_card)
        self._apply_filter()

    # ------------------------------------------------------------- filtering
    def _scope_key(self) -> str:
        for key, label in SCOPE_LABELS.items():
            if label == self.scope_var.get():
                return key
        return SCOPE_ALL

    def _apply_filter(self) -> None:
        state = self.state
        if state is None:
            return
        rows = services.filter_state_keys(state, self.search_var.get(),
                                          self._scope_key())
        self.tree.delete(*self.tree.get_children())
        self._row_kv = {}
        snapshot = state.snapshot_revision
        for index, kv in enumerate(rows):
            if index >= MAX_ROWS:
                break
            changed = (kv.mod_revision is not None and snapshot is not None
                       and kv.mod_revision > snapshot)
            iid = f"kv{index}"
            self.tree.insert("", "end", iid=iid,
                             values=(kv.key,
                                     "" if kv.mod_revision is None else str(kv.mod_revision),
                                     _shorten(kv.value, VALUE_PREVIEW_LIMIT)),
                             tags=("changed" if changed else "snapshot",))
            self._row_kv[iid] = kv
        total = len(rows)
        if total == 0:
            text = ("recovered state has no keys (empty state map)"
                    if state.key_count == 0 else "no keys match the filter")
        elif total > MAX_ROWS:
            text = f"showing first {MAX_ROWS} of {total:,} matching keys"
        else:
            text = f"{total:,} key(s)"
        self.count_label.configure(text=text)

    # ------------------------------------------------------------- values
    def _show_selected_value(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        kv = self._row_kv.get(selection[0])
        if kv is not None:
            ValueDialog(self.window, kv, self.fonts)

    # ------------------------------------------------------------- lifecycle
    def _on_close(self) -> None:
        self._load_seq += 1  # discard any load result still in flight
        self.window.destroy()


class ValueDialog:
    """Full value of one selected key, with an optional pretty-printed view."""

    def __init__(self, master, kv: StateKV, fonts) -> None:
        self.kv = kv
        self.fonts = fonts
        self.window = tk.Toplevel(master)
        self.window.title(f"Value \u00b7 \u2026{kv.key[-64:]}")
        self.window.configure(bg=theme.BG_ROOT)
        self.window.minsize(560, 380)  # auto-sized: never clip the buttons row
        self.window.transient(master)
        self.window.fonts = fonts
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

        head = tk.Frame(self.window, bg=theme.BG_ROOT)
        head.pack(fill="x", padx=16, pady=(14, 4))
        theme.label(head, "SELECTED KEY", font_key="card_label",
                    fg=theme.FG_DIM, bg=theme.BG_ROOT).pack(anchor="w")
        theme.label(head, kv.key, font_key="small", fg=theme.FG, bg=theme.BG_ROOT,
                    wraplength=700, justify="left").pack(anchor="w")
        theme.label(head, f"mod_revision: "
                          f"{'?' if kv.mod_revision is None else kv.mod_revision}"
                          f"   \u00b7   {len(kv.value):,} characters",
                    font_key="small", fg=theme.FG_DIM, bg=theme.BG_ROOT).pack(anchor="w")

        pane = tk.Frame(self.window, bg=theme.BG_ROOT)
        pane.pack(fill="both", expand=True, padx=16, pady=(6, 4))
        self.text = tk.Text(pane, bg=theme.BG_INPUT, fg=theme.FG, bd=0,
                            highlightthickness=0, font=fonts.mono_small,
                            state="disabled", wrap="none", height=14, width=84)
        self.text.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(pane, orient="vertical", command=self.text.yview)
        vsb.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=vsb.set)

        pretty, payload = self._render_value(kv.value)
        self.text.configure(state="normal")
        self.text.insert("1.0", payload)
        self.text.configure(state="disabled")
        if pretty:
            theme.label(self.window, "value is pretty-printed JSON; "
                                     "\u201ccopy value\u201d copies the raw string",
                        font_key="small", fg=theme.FG_FAINT,
                        bg=theme.BG_ROOT).pack(anchor="w", padx=16)

        buttons = tk.Frame(self.window, bg=theme.BG_ROOT)
        buttons.pack(fill="x", padx=16, pady=(4, 14))
        self.btn_copy = theme.button(buttons, "Copy value", self._copy, kind="secondary",
                                     fonts=fonts)
        self.btn_copy.pack(side="left")
        theme.button(buttons, "Close", self.window.destroy, kind="ghost",
                     fonts=fonts).pack(side="right")

    @staticmethod
    def _render_value(raw: str) -> tuple[bool, str]:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return False, raw
        if isinstance(parsed, (dict, list)):
            try:
                return True, json.dumps(parsed, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                return False, raw
        return False, raw

    def _copy(self) -> None:
        self.window.clipboard_clear()
        self.window.clipboard_append(self.kv.value)
        self.btn_copy.configure(text="copied \u2713")
        self.window.after(1200, lambda: self._reset_copy_button())

    def _reset_copy_button(self) -> None:
        try:
            if self.window.winfo_exists():
                self.btn_copy.configure(text="Copy value")
        except tk.TclError:
            pass
