"""WatchLab Desktop main window: sidebar navigation, page container, status bar.

Owns the shared process registry and the thread-safe UI mailbox: worker
threads never touch Tkinter directly, they push callbacks into a queue that
the UI thread drains with a short `after()` poll.
"""
from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable, TypeVar

from . import theme
from .process import ProcessRunner, format_duration, format_ts
from . import services

T = TypeVar("T")

PAGE_ORDER = ("dashboard", "recovery", "events", "experiments", "analytics", "logs")
PAGE_TITLES = {
    "dashboard": "Dashboard",
    "recovery": "Recovery",
    "events": "Events",
    "experiments": "Experiments",
    "analytics": "Analytics",
    "logs": "Logs",
}


class WatchLabApp(tk.Tk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("WatchLab Desktop")
        self.geometry("1180x760")
        self.minsize(980, 620)
        self.configure(bg=theme.BG_ROOT)
        self.fonts = theme.apply_theme(self)

        self.runners: list[ProcessRunner] = []
        self._closing = False
        self._mailbox: queue.Queue[Callable[[], None]] = queue.Queue()
        self._health_after_job: str | None = None

        self._build_layout()
        self._build_pages()
        self.show_page("dashboard")
        self._drain_job = self.after(80, self._drain_mailbox)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(80, self._drain_mailbox)

    # ------------------------------------------------------------- layout
    def _build_layout(self) -> None:
        self.sidebar = tk.Frame(self, bg=theme.BG_SIDEBAR, width=210)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        header = tk.Frame(self.sidebar, bg=theme.BG_SIDEBAR)
        header.pack(fill="x", padx=18, pady=(22, 4))
        tk.Label(header, text="WatchLab", fg=theme.FG, bg=theme.BG_SIDEBAR,
                 font=self.fonts.title).pack(anchor="w")
        tk.Label(header, text="Desktop \u00b7 etcd Watch recovery",
                 fg=theme.FG_DIM, bg=theme.BG_SIDEBAR,
                 font=self.fonts.small).pack(anchor="w")

        theme.separator(self.sidebar, theme.BORDER).pack(fill="x", padx=14, pady=14)

        self.nav_buttons: dict[str, tuple[tk.Button, tk.Frame]] = {}
        for name in PAGE_ORDER:
            row = tk.Frame(self.sidebar, bg=theme.BG_SIDEBAR)
            row.pack(fill="x", padx=10, pady=1)
            indicator = tk.Frame(row, bg=theme.BG_SIDEBAR, width=3)
            indicator.pack(side="left", fill="y", padx=(0, 4))
            btn = tk.Button(row, text=PAGE_TITLES[name], command=lambda n=name: self.show_page(n),
                            font=self.fonts.nav, bg=theme.BG_SIDEBAR, fg=theme.FG_DIM,
                            activebackground=theme.BG_CARD_ALT, activeforeground=theme.FG,
                            relief="flat", bd=0, anchor="w", padx=14, pady=9,
                            cursor="hand2", highlightthickness=0, takefocus=0)
            btn.pack(fill="x")
            btn.bind("<Enter>", lambda e, b=btn: str(b.cget("state")) == "normal"
                     and b.configure(fg=theme.FG))
            btn.bind("<Leave>", lambda e, b=btn: str(b.cget("state")) == "normal"
                     and b.configure(fg=theme.FG_DIM))
            self.nav_buttons[name] = (btn, indicator)

        spacer = tk.Frame(self.sidebar, bg=theme.BG_SIDEBAR)
        spacer.pack(fill="both", expand=True)
        footer = tk.Frame(self.sidebar, bg=theme.BG_SIDEBAR)
        footer.pack(fill="x", padx=18, pady=14)
        tk.Label(footer, text="Go backend: bin/watchlab", fg=theme.FG_FAINT,
                 bg=theme.BG_SIDEBAR, font=self.fonts.small).pack(anchor="w")
        tk.Label(footer, text="Python \u00b7 Tkinter frontend", fg=theme.FG_FAINT,
                 bg=theme.BG_SIDEBAR, font=self.fonts.small).pack(anchor="w")

        self.content = tk.Frame(self, bg=theme.BG_ROOT)
        self.content.pack(side="left", fill="both", expand=True)

        self.statusbar = tk.Frame(self, bg=theme.BG_SIDEBAR, height=30)
        self.statusbar.pack(side="bottom", fill="x")
        self.statusbar.pack_propagate(False)
        self.status_left = tk.Label(self.statusbar, text="idle", fg=theme.FG_DIM,
                                    bg=theme.BG_SIDEBAR, font=self.fonts.small)
        self.status_left.pack(side="left", padx=12)
        self.status_right = tk.Label(self.statusbar, text="", fg=theme.FG_DIM,
                                     bg=theme.BG_SIDEBAR, font=self.fonts.small)
        self.status_right.pack(side="right", padx=12)

    def _build_pages(self) -> None:
        # imported here so a broken page module cannot hide the window frame
        from .dashboard import DashboardPage
        from .recovery import RecoveryPage
        from .events import EventsPage
        from .experiments import ExperimentsPage
        from .analytics import AnalyticsPage
        from .logs import LogsPage

        self.pages: dict[str, object] = {
            "dashboard": DashboardPage(self),
            "recovery": RecoveryPage(self),
            "events": EventsPage(self),
            "experiments": ExperimentsPage(self),
            "analytics": AnalyticsPage(self),
            "logs": LogsPage(self),
        }

    # ------------------------------------------------------------- paging
    def show_page(self, name: str) -> None:
        for nav, (btn, indicator) in self.nav_buttons.items():
            if nav == name:
                btn.configure(fg=theme.ACCENT_BRIGHT)
                indicator.configure(bg=theme.ACCENT)
            else:
                btn.configure(fg=theme.FG_DIM)
                indicator.configure(bg=theme.BG_SIDEBAR)
        for page_name, page in self.pages.items():
            frame = getattr(page, "frame", None)
            if frame is None:
                continue
            if page_name == name:
                frame.pack(fill="both", expand=True)
                on_show = getattr(page, "on_show", None)
                if callable(on_show):
                    on_show()
            else:
                frame.pack_forget()
                on_hide = getattr(page, "on_hide", None)
                if callable(on_hide):
                    on_hide()

    # ----------------------------------------------------------- mailbox
    def ui_call(self, callback: Callable[[], None]) -> None:
        """Schedule a callback on the UI thread (thread-safe)."""
        self._mailbox.put(callback)

    def _drain_mailbox(self) -> None:
        if self._closing:
            return
        self._drain_job = self.after(80, self._drain_mailbox)
        drained = 0
        while drained < 200:
            try:
                callback = self._mailbox.get_nowait()
            except queue.Empty:
                break
            drained += 1
            try:
                callback()
            except Exception:  # keep the UI alive on page bugs
                import traceback
                traceback.print_exc()
        self._dispatch_runners()

    def _dispatch_runners(self) -> None:
        """Drain each runner queue on the UI thread and call its listeners.

        Worker threads only put events into queue.Queue; listeners always run
        on the UI thread here, so Tkinter is touched from a single thread.
        """
        for runner in self.runners:
            for kind, ts, payload in runner.drain():
                for listener in tuple(runner.listeners):
                    try:
                        listener(kind, ts, payload)
                    except Exception:  # keep the UI alive on page bugs
                        import traceback
                        traceback.print_exc()

    # ----------------------------------------------------------- processes
    def run_process(self, name: str, command: list[str], on_done: Callable[[], None] | None = None
                    ) -> ProcessRunner:
        runner = ProcessRunner(name, command, services.GUI_LOG_DIR, cwd=services.PROJECT_ROOT)
        if on_done is not None:
            runner.listeners.append(lambda kind, ts, line: on_done() if kind == "exit" else None)
        self.runners.append(runner)
        runner.start()
        self.set_status(f"running: {name}")
        return runner

    def stop_runner(self, runner: ProcessRunner) -> None:
        if runner.is_running():
            self.set_status(f"stopping: {runner.name}")
        runner.stop()

    def set_status(self, text: str) -> None:
        self.status_left.configure(text=text)

    def set_status_right(self, text: str) -> None:
        self.status_right.configure(text=text)

    # ------------------------------------------------------------- closing
    def on_close(self) -> None:
        """Stop only our own GUI-spawned processes, then close."""
        self._closing = True
        job = getattr(self, "_drain_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._drain_job = None
        for runner in self.runners:
            if runner.is_running():
                runner.stop()
        self.destroy()
