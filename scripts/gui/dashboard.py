"""Dashboard page: real metrics from files and live checks, quick actions."""
from __future__ import annotations

import threading
import tkinter as tk

from . import services, theme


class DashboardPage:
    def __init__(self, app) -> None:
        self.app = app
        self.frame = tk.Frame(app.content, bg=theme.BG_ROOT)
        self.fonts = app.fonts

        self._env: services.EnvInfo | None = None
        self._health: tuple[bool, str] | None = None
        self._demo = None

        self._build()
        self.refresh()

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        header = tk.Frame(self.frame, bg=theme.BG_ROOT)
        header.pack(fill="x", padx=24, pady=(20, 8))
        tk.Label(header, text="WatchLab", fg=theme.FG, bg=theme.BG_ROOT,
                 font=self.fonts.title).pack(side="left")
        tk.Label(header, text="  recovery of etcd Watch API state \u00b7 MVCC compaction",
                 fg=theme.FG_DIM, bg=theme.BG_ROOT,
                 font=self.fonts.base).pack(side="left", pady=(6, 0))

        actions = tk.Frame(header, bg=theme.BG_ROOT)
        actions.pack(side="right")
        self.btn_demo = theme.button(actions, "\u25b6  Run demo", self._open_recovery,
                                     kind="primary", fonts=self.fonts)
        self.btn_demo.pack(side="left", padx=(0, 8))
        self.btn_check = theme.button(actions, "\u21bb  Refresh", self.refresh,
                                      kind="secondary", fonts=self.fonts)
        self.btn_check.pack(side="left", padx=(0, 8))
        self.btn_analyze = theme.button(actions, "Analyze results", self._run_analyze,
                                        kind="secondary", fonts=self.fonts)
        self.btn_analyze.pack(side="left")

        self._build_metric_cards()
        self._build_system_cards()

    def _build_metric_cards(self) -> None:
        grid = tk.Frame(self.frame, bg=theme.BG_ROOT)
        grid.pack(fill="x", padx=24, pady=(10, 4))
        for col in range(4):
            grid.grid_columnconfigure(col, weight=1, uniform="metrics")

        self.completed_card = self._metric_card(grid, 0, 0, "COMPLETED SERIES", "\u2014")
        self.selected_card = self._metric_card(grid, 0, 1, "SELECTED SERIES", "\u2014")
        self.verified_card = self._metric_card(grid, 0, 2, "VERIFIED EVENTS", "\u2014")
        self.demo_card = self._metric_card(grid, 0, 3, "LAST DEMO RESULT", "\u2014")

        self.demo_detail = tk.Label(self.demo_card.winfo_children()[0], text="",
                                    fg=theme.FG_DIM, bg=theme.BG_CARD,
                                    font=self.fonts.small, justify="left", anchor="w")
        self.demo_detail.pack(anchor="w", pady=(4, 0))

    def _metric_card(self, parent, row: int, col: int, caption: str, value: str) -> theme.card:
        outer = theme.card(parent, padding=14)
        outer.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        inner = outer.winfo_children()[0]
        theme.label(inner, caption, font_key="card_label", fg=theme.FG_DIM).pack(anchor="w")
        theme.label(inner, value, font_key="card_value", fg=theme.ACCENT_BRIGHT).pack(
            anchor="w", pady=(4, 0))
        return outer

    def _set_metric(self, card: theme.card, value: str, fg: str = theme.ACCENT_BRIGHT,
                    detail: str | None = None) -> None:
        inner = card.winfo_children()[0]
        labels = inner.winfo_children()
        labels[1].configure(text=value, fg=fg)
        if detail is not None:
            labels[2].configure(text=detail)

    def _build_system_cards(self) -> None:
        grid = tk.Frame(self.frame, bg=theme.BG_ROOT)
        grid.pack(fill="x", padx=24, pady=(4, 4))
        for col in range(4):
            grid.grid_columnconfigure(col, weight=1, uniform="system")

        self.binary_card = self._state_card(grid, 0, 0, "GO BINARY (BACKEND)")
        self.etcd_card = self._state_card(grid, 0, 1, "ETCD (127.0.0.1:2379)")
        self.python_card = self._state_card(grid, 0, 2, "PYTHON")
        self.go_card = self._state_card(grid, 0, 3, "GO TOOLCHAIN")

        note = tk.Frame(self.frame, bg=theme.BG_ROOT)
        note.pack(fill="x", padx=24, pady=(6, 18))
        theme.label(note,
                    "Compaction is only ever executed by scripts/demo.py against its own isolated etcd. "
                    "The GUI never compacts the user's etcd and never starts benchmark series.",
                    font_key="small", fg=theme.FG_FAINT, bg=theme.BG_ROOT,
                    wraplength=900).pack(anchor="w")

    def _state_card(self, parent, row: int, col: int, caption: str) -> theme.card:
        outer = theme.card(parent, padding=14)
        outer.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        inner = outer.winfo_children()[0]
        theme.label(inner, caption, font_key="card_label", fg=theme.FG_DIM).pack(anchor="w")
        self._state_row(inner, "\u25cf", theme.FG_FAINT, "checking\u2026")
        return outer

    def _state_row(self, inner: tk.Frame, dot: str, color: str, text: str) -> tuple[tk.Label, tk.Label]:
        row = tk.Frame(inner, bg=theme.BG_CARD)
        row.pack(anchor="w", pady=(6, 0))
        dot_label = tk.Label(row, text=dot, fg=color, bg=theme.BG_CARD,
                             font=self.fonts.base)
        dot_label.pack(side="left")
        text_label = tk.Label(row, text=" " + text, fg=theme.FG, bg=theme.BG_CARD,
                              font=self.fonts.small)
        text_label.pack(side="left")
        return dot_label, text_label

    def _set_state(self, card: theme.card, color: str, text: str, detail: str = "") -> None:
        inner = card.winfo_children()[0]
        row = inner.winfo_children()[1]
        dot_label, text_label = row.winfo_children()
        dot_label.configure(fg=color)
        text_label.configure(text=" " + text)
        if detail:
            theme.label(inner, detail, font_key="small", fg=theme.FG_FAINT,
                        wraplength=240).pack(anchor="w", pady=(4, 0))
        else:
            for child in inner.winfo_children()[2:]:
                child.destroy()

    # ------------------------------------------------------------ actions
    def refresh(self) -> None:
        self.btn_check.configure(state="disabled")
        self.app.set_status("checking environment\u2026")
        threading.Thread(target=self._worker_env, daemon=True).start()
        threading.Thread(target=self._worker_health, daemon=True).start()
        self._refresh_demo()

    def _worker_env(self) -> None:
        env = services.check_environment()
        series = services.completed_series()
        self.app.ui_call(lambda: self._apply_env(env, series))

    def _worker_health(self) -> None:
        ok, detail = services.etcd_health(services.DEFAULT_ENDPOINT)
        self.app.ui_call(lambda: self._apply_health(ok, detail))

    def _refresh_demo(self) -> None:
        demo = services.last_demo_summary()
        self._demo = demo
        if demo is None:
            self._set_metric(self.demo_card, "no runs yet", theme.FG_FAINT)
            self.demo_detail.configure(
                text="run scripts/demo.py from the Recovery page")
            return
        fg = theme.OK if demo.overall == "PASS" else theme.FAIL
        self._set_metric(self.demo_card, demo.overall or "?", fg)
        self.demo_detail.configure(
            text=f"demo {demo.demo_id}\n"
                 f"{demo.verified_events if demo.verified_events is not None else '?'} verified events")

    def _apply_env(self, env: services.EnvInfo, series: list[str]) -> None:
        self._env = env
        self.btn_check.configure(state="normal")
        self.app.set_status("ready")

        self._set_metric(self.completed_card, str(len(series)), theme.FG)
        self._set_metric(self.selected_card, str(services.selected_series()), theme.FG)
        verified = services.verified_events()
        self._set_metric(self.verified_card,
                         f"{verified:,}" if verified is not None else "n/a",
                         theme.FG if verified is not None else theme.FG_FAINT)

        status_colors = {"ok": theme.OK, "missing": theme.FAIL, "stale": theme.WARN}
        color = status_colors.get(env.binary_status, theme.FG_FAINT)
        self._set_state(self.binary_card, color, env.binary_status, env.binary_detail)
        self._set_state(self.python_card, theme.OK, f"Python {env.python_version}")
        self._set_state(self.go_card, theme.OK if env.go_ok else theme.FAIL,
                        env.go_version)

    def _apply_health(self, ok: bool, detail: str) -> None:
        self._health = (ok, detail)
        self._set_state(self.etcd_card, theme.OK if ok else theme.FAIL,
                        "healthy" if ok else "unavailable", detail)
        self.app.set_status_right(
            f"etcd {services.DEFAULT_ENDPOINT}: {'healthy' if ok else 'unavailable'}")

    def _open_recovery(self) -> None:
        self.app.show_page("recovery")

    def _run_analyze(self) -> None:
        self.app.run_process("analyze-results", [self._python(), "scripts/analyze_results.py"])
        self.app.show_page("logs")

    def _python(self) -> str:
        import sys
        return sys.executable

    def on_show(self) -> None:
        self._refresh_demo()

    def on_hide(self) -> None:
        pass
