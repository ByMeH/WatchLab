"""Recovery page: run scripts/demo.py, show stage progress and live log.

The GUI does not re-implement the demo: it starts `python scripts/demo.py`
and maps its stdout markers onto the ten recovery stages. Compaction is only
ever executed by demo.py against its own isolated etcd.
"""
from __future__ import annotations

import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from . import services, theme
from .process import ProcessRunner, format_ts
from .state_inspector import StateInspectorDialog

STAGES: list[tuple[str, str, str, str]] = [
    # (short title, activate pattern, done pattern, description)
    ("Starting isolated etcd", "=== WatchLab demonstration ===",
     "isolated etcd healthy",
     "demo.py starts a private single-member etcd on free localhost ports"),
    ("Watch created", "$ [watch-initial]",
     "reconnect: phase 1 delivered",
     "watch reports WATCH_CREATED in its console; phase 1 is fully delivered"),
    ("Producer started", "$ [producer-recon-p1]",
     "reconnect: phase 1 delivered",
     "Producer writes phase 1 events; the watcher consumes them"),
    ("Client disconnected", "reconnect: phase 1 delivered",
     "reconnect: phase 2 written while Watch was down",
     "only our own Watch process is stopped; phase 2 is written while nobody watches"),
    ("Reconnect with L+1", "$ [watch-resumed]",
     "--- reconnect: PASS",
     "Watch resumes from the last fully processed revision L+1; no gaps"),
    ("Compaction executed", "=== SCENARIO: compaction-recovery ===",
     "$ [recover-after-compaction]",
     "demo.py compacts its own isolated etcd at the last phase-2 revision"),
    ("Compaction detected", "$ [recover-after-compaction]",
     "compaction: COMPACTED detected",
     "recover reports RECOVER_COMPACTED: required revision has been compacted"),
    ("Range snapshot created", "$ [recover-after-compaction]",
     "compaction: COMPACTED detected",
     "fresh Range snapshot at revision R (current key-value map, not history)"),
    ("Watch restarted with R+1", "compaction: COMPACTED detected",
     "compaction: phase 3 delivered",
     "replacement Watch starts at R+1 and receives phase 3 events"),
    ("Validation completed", "compaction: phase 3 delivered",
     "--- compaction-recovery: PASS",
     "event-level validation: request_id, revision, sequence, key, SHA-256"),
]

STAGE_PENDING = 0
STAGE_ACTIVE = 1
STAGE_DONE = 2
STAGE_FAIL = 3
STAGE_COLORS = {
    STAGE_PENDING: theme.FG_FAINT,
    STAGE_ACTIVE: theme.ACCENT,
    STAGE_DONE: theme.OK,
    STAGE_FAIL: theme.FAIL,
}
STAGE_WORDS = {STAGE_PENDING: "pending", STAGE_ACTIVE: "running",
               STAGE_DONE: "done", STAGE_FAIL: "failed"}


class RecoveryPage:
    def __init__(self, app) -> None:
        self.app = app
        self.frame = tk.Frame(app.content, bg=theme.BG_ROOT)
        self.fonts = app.fonts
        self.runner: ProcessRunner | None = None
        self.stage_states = [STAGE_PENDING] * len(STAGES)
        self._pulse_job: str | None = None
        self._pulse_on = False
        self._summary_path: str | None = None
        self._state_path: str | None = None
        self._inspector: StateInspectorDialog | None = None
        self._lines: list[tuple[float, str]] = []

        self._build_controls()
        self._build_path()
        self._build_body()
        self._build_result()

    # ------------------------------------------------------------- layout
    def _build_controls(self) -> None:
        bar = tk.Frame(self.frame, bg=theme.BG_ROOT)
        bar.pack(fill="x", padx=24, pady=(20, 8))

        self.btn_run = theme.button(bar, "\u25b6  Run demo", self.run_demo,
                                    kind="primary", fonts=self.fonts)
        self.btn_run.pack(side="left")
        self.btn_stop = theme.button(bar, "\u25a0  Stop", self.stop_demo,
                                     kind="danger", fonts=self.fonts, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 16))

        self._vars = {"events": tk.StringVar(value="5"),
                      "interval": tk.StringVar(value="100ms"),
                      "timeout": tk.StringVar(value="30")}
        for caption, var, width in (("events", self._vars["events"], 5),
                                    ("interval", self._vars["interval"], 8),
                                    ("timeout, s", self._vars["timeout"], 5)):
            cell = tk.Frame(bar, bg=theme.BG_ROOT)
            cell.pack(side="left", padx=(0, 12))
            theme.label(cell, caption, font_key="small", fg=theme.FG_DIM,
                        bg=theme.BG_ROOT).pack(anchor="w")
            theme.entry(cell, self.fonts, textvariable=var, width=width).pack(fill="x")

        self.progress_label = theme.label(bar, "stages: 0/10", font_key="small",
                                          fg=theme.FG_DIM, bg=theme.BG_ROOT)
        self.progress_label.pack(side="left", padx=16)
        self.progress = ttk.Progressbar(bar, style="Slim.Horizontal.TProgressbar",
                                        maximum=10, value=0, length=180)
        self.progress.pack(side="left")

    def _build_path(self) -> None:
        holder = tk.Frame(self.frame, bg=theme.BG_ROOT)
        holder.pack(fill="x", padx=24)
        self.path_canvas = tk.Canvas(holder, bg=theme.BG_ROOT, height=96,
                                     highlightthickness=0, bd=0)
        self.path_canvas.pack(fill="x", expand=True)
        self.path_canvas.bind("<Configure>", lambda _e: self._draw_path())
        self.path_canvas.bind("<Button-1>", self._on_path_click)

    def _build_body(self) -> None:
        body = tk.Frame(self.frame, bg=theme.BG_ROOT)
        body.pack(fill="both", expand=True, padx=24, pady=(6, 0))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # stage list
        stage_panel = theme.card(body, padding=10)
        stage_panel.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        theme.label(stage_panel.winfo_children()[0], "RECOVERY PATH",
                    font_key="card_label", fg=theme.FG_DIM).pack(anchor="w", pady=(0, 6))
        self.stage_rows: list[tuple[tk.Label, tk.Label]] = []
        for index, (title, _act, _done, _desc) in enumerate(STAGES):
            row = tk.Frame(stage_panel.winfo_children()[0], bg=theme.BG_CARD)
            row.pack(fill="x", pady=1)
            dot = tk.Label(row, text="\u25cb", fg=theme.FG_FAINT, bg=theme.BG_CARD,
                           font=self.fonts.base, width=2)
            dot.pack(side="left")
            num = tk.Label(row, text=f"{index + 1}.", fg=theme.FG_DIM,
                           bg=theme.BG_CARD, font=self.fonts.small, width=3)
            num.pack(side="left")
            lbl = tk.Label(row, text=title, fg=theme.FG_DIM, bg=theme.BG_CARD,
                           font=self.fonts.small, anchor="w")
            lbl.pack(side="left")
            state = tk.Label(row, text=STAGE_WORDS[STAGE_PENDING], fg=theme.FG_FAINT,
                             bg=theme.BG_CARD, font=self.fonts.small)
            state.pack(side="right")
            self.stage_rows.append((dot, state))

        # live log
        log_card = theme.card(body, padding=0)
        log_card.grid(row=0, column=1, sticky="nsew")
        inner = log_card.winfo_children()[0]
        head = tk.Frame(inner, bg=theme.BG_CARD)
        head.pack(fill="x", padx=10, pady=(8, 4))
        theme.label(head, "DEMO OUTPUT \u00b7 python scripts/demo.py",
                    font_key="card_label", fg=theme.FG_DIM).pack(side="left")
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(head, text="auto-scroll", variable=self.autoscroll,
                        style="Switch.TCheckbutton").pack(side="right")
        self.log = tk.Text(inner, bg=theme.BG_INPUT, fg=theme.FG, bd=0,
                           highlightthickness=0, font=self.fonts.mono_small,
                           state="disabled", wrap="none", height=12)
        self.log.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for kind, color in (("info", theme.FG), ("mark", theme.ACCENT_BRIGHT),
                            ("error", theme.FAIL), ("spawn", theme.RUNNING)):
            self.log.tag_configure(kind, foreground=color)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)

    def _build_result(self) -> None:
        self.result_card = theme.card(self.frame, padding=12)
        self.result_card.pack(fill="x", padx=24, pady=(8, 18))
        inner = self.result_card.winfo_children()[0]
        head = tk.Frame(inner, bg=theme.BG_CARD)
        head.pack(fill="x")
        self.result_title = theme.label(head, "LAST DEMO RESULT", font_key="card_label",
                                        fg=theme.FG_DIM)
        self.result_title.pack(side="left")
        self.btn_inspect = theme.button(head, "\u25c9  Inspect recovered state",
                                        self._open_inspector, kind="secondary",
                                        fonts=self.fonts, padx=10, pady=4,
                                        state="disabled")
        self.btn_inspect.pack(side="right")
        self.result_body = tk.Label(inner, text="No demo has been started from the GUI yet.",
                                    fg=theme.FG_DIM, bg=theme.BG_CARD,
                                    font=self.fonts.small, justify="left", anchor="w")
        self.result_body.pack(anchor="w", pady=(4, 0))
        self._refresh_result_from_files()

    # ------------------------------------------------------------- actions
    def run_demo(self) -> None:
        if self.runner is not None and self.runner.is_running():
            return
        try:
            events = int(self._vars["events"].get())
            timeout = float(self._vars["timeout"].get())
            interval = self._vars["interval"].get().strip() or "100ms"
        except ValueError:
            self._append_line("GUI", "events/timeout must be numeric", "error")
            return
        command = [sys.executable, "scripts/demo.py",
                   "--events", str(events), "--interval", interval,
                   "--timeout", str(timeout)]
        self._reset_run()
        self.runner = self.app.run_process("demo", command)
        self.runner.listeners.append(self._on_event)
        self._set_stage(0, STAGE_ACTIVE)
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._start_pulse()
        self._append_line("GUI", "starting: " + " ".join(command), "info")

    def stop_demo(self) -> None:
        runner = self.runner
        if runner is None or not runner.is_running():
            return
        self._append_line("GUI", "SIGINT \u2192 demo.py stops its own children only", "info")
        runner.stop()

    def _reset_run(self) -> None:
        self.stage_states = [STAGE_PENDING] * len(STAGES)
        self._summary_path = None
        self._state_path = None
        self.btn_inspect.configure(state="disabled")
        self._lines = []
        self._draw_path()
        self._refresh_stage_rows()
        self._set_progress(0)
        self.result_body.configure(text="Demo is running\u2026", fg=theme.FG_DIM)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------- streaming
    def _on_event(self, kind: str, ts: float, line: str) -> None:
        """Process event from the app dispatcher (UI thread).

        Per ProcessRunner's queue contract the exit code travels in the ts
        slot ("exit", exit_code, "") with an empty payload line.
        """
        if kind == "out":
            self._on_line(ts, line)
        elif kind == "exit":
            self._on_exit(int(ts))

    def _on_line(self, ts: float, line: str) -> None:
        self._lines.append((ts, line))
        self._append_line("demo", line, self._classify(line))
        self._match_stages(line)

    def _classify(self, line: str) -> str:
        if line.startswith("ERROR") or "--- " in line and ": FAIL" in line:
            return "error"
        if line.startswith("$ ["):
            return "spawn"
        if line.startswith("[t+") or line.startswith("=== ") or line.startswith("--- "):
            return "mark"
        return "info"

    def _append_line(self, source: str, line: str, tag: str = "info") -> None:
        text = self.log
        text.configure(state="normal")
        text.insert("end", f"[{format_ts(time.time())}] {source}: {line}")
        if not line.endswith("\n"):
            text.insert("end", "\n")
        text.tag_add(tag, "end-2l", "end-1l")
        if self.autoscroll.get():
            text.see("end")
        text.configure(state="disabled")
        # keep the widget from growing without bound
        lines_count = int(text.index("end-1c").split(".")[0])
        if lines_count > 4000:
            text.delete("1.0", f"{lines_count - 3000}.0")

    def _match_stages(self, line: str) -> None:
        for index, (_title, activate, done, _desc) in enumerate(STAGES):
            state = self.stage_states[index]
            if state == STAGE_PENDING and activate in line:
                self._set_stage(index, STAGE_ACTIVE)
            elif state == STAGE_ACTIVE and done in line:
                self._set_stage(index, STAGE_DONE)
            elif state == STAGE_ACTIVE and (line.startswith("ERROR")
                                            or "--- " in line and ": FAIL" in line):
                self._set_stage(index, STAGE_FAIL)
        if "machine summary" in line and ":" in line:
            # demo.py prints "machine summary       : <path>" (padded column)
            tail = line.split(":", 1)[1].strip()
            if tail:
                self._summary_path = tail.split()[0]
        if "reconnect: phase 1 delivered" in line:
            self._set_stage(2, STAGE_DONE)  # phase-1 delivery proves the producer and the watch ran
        if "compaction: COMPACTED detected" in line:
            self._set_stage(7, STAGE_DONE)  # fresh snapshot taken on the same line

    def _set_stage(self, index: int, state: int) -> None:
        if self.stage_states[index] == state:
            return
        self.stage_states[index] = state
        self._refresh_stage_rows()
        self._draw_path()
        self._set_progress(sum(1 for s in self.stage_states if s == STAGE_DONE))

    def _set_progress(self, done: int) -> None:
        self.progress.configure(value=done)
        self.progress_label.configure(text=f"stages: {done}/10")

    def _refresh_stage_rows(self) -> None:
        for index, (dot, state) in enumerate(self.stage_rows):
            s = self.stage_states[index]
            dot.configure(text={STAGE_PENDING: "\u25cb", STAGE_ACTIVE: "\u25cf",
                                STAGE_DONE: "\u2713", STAGE_FAIL: "\u2717"}[s],
                          fg=STAGE_COLORS[s])
            state.configure(text=STAGE_WORDS[s], fg=STAGE_COLORS[s])

    def _on_exit(self, code: int) -> None:
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._stop_pulse()
        if code != 0:
            for index, state in enumerate(self.stage_states):
                if state == STAGE_ACTIVE:
                    self._set_stage(index, STAGE_FAIL)
            # demo.py writes demo-summary.json even when a scenario FAILs.
            summary = self._read_demo_summary()
            if summary is not None:
                self._show_summary()
                self.app.set_status(f"demo finished: {summary.overall} (exit {code})")
                return
            self.result_body.configure(
                text=f"demo.py exited with code {code}. See the log above and the Logs page.",
                fg=theme.FAIL)
            self.app.set_status(f"demo finished: exit code {code}")
            self._refresh_inspector_availability()
            return
        for index, state in enumerate(self.stage_states):
            if state == STAGE_ACTIVE:
                self._set_stage(index, STAGE_DONE)
        self._set_progress(sum(1 for s in self.stage_states if s == STAGE_DONE))
        self._show_summary()

    def _show_summary(self) -> None:
        summary = self._read_demo_summary()
        if summary is None:
            self.result_body.configure(text="demo.py finished (exit 0); "
                                       "machine summary not found in /tmp.",
                                       fg=theme.OK)
            return
        lines = [f"demo {summary.demo_id} \u00b7 overall: {summary.overall}"]
        if summary.verified_events is not None:
            lines.append(f"verified events: {summary.verified_events}")
        for scenario in summary.scenarios:
            status = scenario.get("status", "?")
            detail = str(scenario.get("summary_line", scenario.get("error", "")))[:110]
            lines.append(f"  {scenario.get('scenario', '?')}: {status} \u2014 {detail}")
        color = theme.OK if summary.overall == "PASS" else theme.FAIL
        self.result_body.configure(text="\n".join(lines), fg=color)
        self.app.set_status(f"demo finished: {summary.overall}")
        self._refresh_inspector_availability()

    def _read_demo_summary(self):
        if self._summary_path:
            try:
                data = services.read_json(Path(self._summary_path))
                paths = data.get("paths")
                if isinstance(paths, dict) and isinstance(paths.get("state_file"), str):
                    self._state_path = paths["state_file"]
                from .services import DemoSummary
                return DemoSummary(
                    demo_id=str(data.get("demo_id", "?")),
                    started_at=str(data.get("started_at", "")),
                    overall=str(data.get("overall", "")),
                    verified_events=data.get("total_verified_events"),
                    scenarios=data.get("scenarios") or [],
                    path=self._summary_path,
                )
            except ValueError:
                pass
        return services.last_demo_summary()

    def _refresh_result_from_files(self) -> None:
        summary = services.last_demo_summary()
        if summary is None:
            self._refresh_inspector_availability()
            return
        if not self._state_path:
            self._state_path = services.demo_state_path(summary.path)
        color = theme.OK if summary.overall == "PASS" else theme.FAIL
        self.result_body.configure(
            text=f"demo {summary.demo_id} \u00b7 overall: {summary.overall}"
                 + (f" \u00b7 {summary.verified_events} verified events"
                    if summary.verified_events is not None else ""),
            fg=color)
        self._refresh_inspector_availability()

    # ------------------------------------------------------------- inspector
    def _resolve_state_path(self) -> str | None:
        """state.json of the latest demo: recorded path first, then newest file."""
        if self._state_path and Path(self._state_path).is_file():
            return self._state_path
        latest = services.latest_recovery_state_path()
        return str(latest) if latest is not None else None

    def _refresh_inspector_availability(self) -> None:
        path = self._resolve_state_path()
        self.btn_inspect.configure(state="normal" if path else "disabled")

    def _open_inspector(self) -> None:
        path = self._resolve_state_path()
        if not path:
            return
        inspector = self._inspector
        if inspector is not None and inspector.window.winfo_exists():
            inspector.window.deiconify()
            inspector.window.lift()
            inspector.window.focus_force()
            return
        self._inspector = StateInspectorDialog(self.app, path)

    # ------------------------------------------------------------- path canvas
    def _draw_path(self) -> None:
        canvas = self.path_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 900)
        height = int(canvas.winfo_height() or 96)
        n = len(STAGES)
        margin = 46
        gap = (width - 2 * margin) / (n - 1)
        y = 34
        radius = 13
        coords = []
        for index in range(n):
            x = margin + index * gap
            coords.append((x, y))
            self._draw_node(canvas, index, x, y, radius)
        for index in range(n - 1):
            x1, y1 = coords[index]
            x2, y2 = coords[index + 1]
            state_a = self.stage_states[index]
            state_b = self.stage_states[index + 1]
            both_done = state_a == STAGE_DONE and state_b in (STAGE_DONE, STAGE_ACTIVE)
            color = theme.OK if both_done else (
                theme.ACCENT if state_a == STAGE_DONE and state_b == STAGE_ACTIVE else theme.BORDER)
            canvas.create_line(x1 + radius, y1, x2 - radius, y2, fill=color, width=2)
        canvas.yview_moveto(0)
        self.path_coords = coords

    def _draw_node(self, canvas: tk.Canvas, index: int, x: int, y: int, radius: int) -> None:
        state = self.stage_states[index]
        fill = {STAGE_PENDING: theme.BG_CARD, STAGE_ACTIVE: theme.ACCENT_DIM,
                STAGE_DONE: theme.OK_DIM, STAGE_FAIL: theme.FAIL_DIM}[state]
        outline = STAGE_COLORS[state]
        if state == STAGE_ACTIVE and not self._pulse_on:
            outline = theme.ACCENT_BRIGHT
        symbol = {STAGE_PENDING: str(index + 1), STAGE_ACTIVE: str(index + 1),
                  STAGE_DONE: "\u2713", STAGE_FAIL: "\u2717"}[state]
        canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                           fill=fill, outline=outline, width=2)
        canvas.create_text(x, y, text=symbol, fill=STAGE_COLORS[state],
                           font=self.fonts.small)
        title = STAGES[index][0]
        short = title if len(title) <= 18 else title[:17] + "\u2026"
        canvas.create_text(x, y + radius + 10, text=short,
                           fill=theme.FG if state != STAGE_PENDING else theme.FG_FAINT,
                           font=self.fonts.small)

    def _on_path_click(self, event) -> None:
        coords = getattr(self, "path_coords", [])
        if not coords:
            return
        for index, (x, y) in enumerate(coords):
            if abs(event.x - x) <= 18 and abs(event.y - y) <= 26:
                self._show_stage_info(index)
                return

    def _show_stage_info(self, index: int) -> None:
        title, _act, _done, desc = STAGES[index]
        state = self.stage_states[index]
        self.result_title.configure(text=f"STAGE {index + 1} \u00b7 {title}")
        self.result_body.configure(
            text=f"{desc}\nstate: {STAGE_WORDS[state]}", fg=theme.FG_DIM)

    # ------------------------------------------------------------- pulse
    def _start_pulse(self) -> None:
        if self._pulse_job:
            return
        self._pulse_on = True
        self._pulse_job = self.app.after(450, self._pulse_step)

    def _pulse_step(self) -> None:
        self._pulse_on = not self._pulse_on
        self._draw_path()
        self._pulse_job = self.app.after(450, self._pulse_step)

    def _stop_pulse(self) -> None:
        if self._pulse_job:
            self.app.after_cancel(self._pulse_job)
            self._pulse_job = None
        self._pulse_on = False
        self._draw_path()

    # ------------------------------------------------------------- lifecycle
    def on_show(self) -> None:
        self._draw_path()
        self._refresh_inspector_availability()

    def on_hide(self) -> None:
        pass
