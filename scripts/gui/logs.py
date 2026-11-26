"""Logs page: streamed output of GUI-started processes, exit codes, stop."""
from __future__ import annotations

import subprocess
import time
import tkinter as tk
from tkinter import ttk

from . import services, theme
from .process import ProcessRunner, format_duration, format_ts


class LogsPage:
    def __init__(self, app) -> None:
        self.app = app
        self.frame = tk.Frame(app.content, bg=theme.BG_ROOT)
        self.fonts = app.fonts
        self._selected_runner: ProcessRunner | None = None
        self._tick_job: str | None = None
        self._last_status: dict[int, str] = {}
        self._build_header()
        self._build_body()

    def _build_header(self) -> None:
        bar = tk.Frame(self.frame, bg=theme.BG_ROOT)
        bar.pack(fill="x", padx=24, pady=(20, 8))
        theme.label(bar, "Process logs", fg=theme.FG, bg=theme.BG_ROOT,
                    font_key="heading").pack(side="left")
        theme.label(bar, "GUI-started processes only; logs are saved under logs/gui/",
                    font_key="small", fg=theme.FG_DIM, bg=theme.BG_ROOT).pack(
            side="left", padx=16)
        theme.button(bar, "Stop selected", self._stop_selected, kind="danger",
                     fonts=self.fonts).pack(side="right", padx=(8, 0))
        theme.button(bar, "\u21bb  Refresh", self.refresh, kind="secondary",
                     fonts=self.fonts).pack(side="right")
        theme.button(bar, "Open log dir", self._open_log_dir, kind="ghost",
                     fonts=self.fonts).pack(side="right")

    def _build_body(self) -> None:
        body = tk.Frame(self.frame, bg=theme.BG_ROOT)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        body.grid_columnconfigure(0, weight=2)
        body.grid_columnconfigure(1, weight=3)
        body.grid_rowconfigure(0, weight=1)

        list_card = theme.card(body, padding=0)
        list_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        inner = list_card.winfo_children()[0]
        cols = ("name", "status", "exit", "duration", "log")
        tree = ttk.Treeview(inner, columns=cols, show="headings", selectmode="browse")
        for col, heading, width in zip(cols, ("name", "status", "exit code", "duration", "log file"),
                                       (150, 90, 70, 80, 240)):
            tree.heading(col, text=heading)
            tree.column(col, width=width, anchor="w", stretch=(col == "log"))
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=vsb.set)
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)
        tree.tag_configure("running", foreground=theme.RUNNING)
        tree.tag_configure("ok", foreground=theme.OK)
        tree.tag_configure("fail", foreground=theme.FAIL)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree = tree

        out_card = theme.card(body, padding=0)
        out_card.grid(row=0, column=1, sticky="nsew")
        inner = out_card.winfo_children()[0]
        head = tk.Frame(inner, bg=theme.BG_CARD)
        head.pack(fill="x", padx=8, pady=(8, 2))
        theme.label(head, "OUTPUT", font_key="card_label", fg=theme.FG_DIM).pack(side="left")
        self.timestamps = tk.BooleanVar(value=True)
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(head, text="timestamps", variable=self.timestamps).pack(
            side="right", padx=6)
        ttk.Checkbutton(head, text="auto-scroll", variable=self.autoscroll).pack(side="right")
        self.output = tk.Text(inner, bg=theme.BG_INPUT, fg=theme.FG, bd=0,
                              font=self.fonts.mono_small, state="disabled", wrap="none")
        self.output.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.output.yview)
        sb.pack(side="right", fill="y", padx=(0, 2))
        self.output.configure(yscrollcommand=sb.set)

    # ------------------------------------------------------------- data
    def refresh(self) -> None:
        runners = list(self.app.runners)
        statuses = {}
        for index, runner in enumerate(runners):
            running = runner.is_running()
            status = "running" if running else (
                "finished" if runner.exit_code is not None else "not started")
            statuses[index] = status
            tag = "running" if running else ("ok" if runner.exit_code == 0 else "fail")
            values = (runner.name, status,
                      runner.exit_code if runner.exit_code is not None else "-",
                      format_duration(runner.started_at,
                                      runner.finished_at or (time.time() if running else None)),
                      runner.log_path.name if runner.log_path else "-")
            if self.tree.exists(str(index)):
                self.tree.item(str(index), values=values, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=str(index), tags=(tag,), values=values)
        for iid in list(self.tree.get_children()):
            if int(iid) >= len(runners):
                self.tree.delete(iid)
        self._last_status = statuses
        if self._selected_runner is None and runners:
            self.tree.selection_set("0")
        if self._selected_runner is not None and self._selected_runner.is_running():
            self._show_selected()

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if 0 <= index < len(self.app.runners):
            self._selected_runner = self.app.runners[index]
            self._show_selected()

    def _show_selected(self) -> None:
        runner = self._selected_runner
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        if runner is None:
            self.output.configure(state="disabled")
            return
        if runner.is_running():
            lines = runner.tail(400)
        else:
            lines = runner.read_log(400)
        for ts, line in lines:
            self.output.insert("end", self._format_line(ts, line))
        if self.autoscroll.get():
            self.output.see("end")
        self.output.configure(state="disabled")

    def _format_line(self, ts: float, line: str) -> str:
        prefix = f"[{format_ts(ts)}] " if self.timestamps.get() else ""
        return prefix + (line if line.endswith("\n") else line + "\n")

    def _stop_selected(self) -> None:
        runner = self._selected_runner
        if runner is not None and runner.is_running():
            self.app.stop_runner(runner)
            self.refresh()

    def _open_log_dir(self) -> None:
        try:
            subprocess.Popen(["xdg-open", str(services.GUI_LOG_DIR)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            self.app.set_status("cannot open log directory")

    # ------------------------------------------------------------- lifecycle
    def _tick(self) -> None:
        if self._tick_job is None:
            return
        changed = self._last_status != {
            index: ("running" if r.is_running() else
                    "finished" if r.exit_code is not None else "not started")
            for index, r in enumerate(self.app.runners)}
        if changed:
            self.refresh()
        elif self._selected_runner is not None and self._selected_runner.is_running():
            self._show_selected()
        self._tick_job = self.frame.after(400, self._tick)

    def on_show(self) -> None:
        self.refresh()
        self._tick_job = self.frame.after(400, self._tick)

    def on_hide(self) -> None:
        if self._tick_job is not None:
            self.frame.after_cancel(self._tick_job)
            self._tick_job = None
