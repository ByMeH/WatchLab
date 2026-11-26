"""Managed subprocess runner for the GUI.

Tkinter-free module. Wraps Popen with a reader thread and a thread-safe
queue.Queue, so the UI thread never blocks on child output. Streams are
tee'd into a per-run log file under logs/gui/. Stop sequence mirrors
scripts/demo.py::ManagedProcess.stop: SIGINT -> SIGTERM -> SIGKILL.

Only processes started by the GUI are ever stopped: signals go to the
spawned child (and its process group, which the GUI created), never to a
pre-existing etcd or other WatchLab processes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Sequence


class ProcessRunner:
    """One managed child process with streamed output.

    Queue items:
      ("out", timestamp, line)          -- one stdout/stderr line
      ("exit", exit_code, "")           -- process finished
    """

    def __init__(self, name: str, command: Sequence[str], log_dir: Path,
                 cwd: Path | None = None, tail_limit: int = 4000) -> None:
        self.name = name
        self.command = [str(part) for part in command]
        self.log_dir = Path(log_dir)
        self.cwd = Path(cwd) if cwd else None
        self.tail_limit = tail_limit
        self.queue: Queue[tuple[str, float, str]] = Queue()
        self.listeners: list[Callable[[str, float, str], None]] = []
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.log_path: Path | None = None
        self._tail: deque[tuple[float, str]] = deque(maxlen=tail_limit)
        self._reader: threading.Thread | None = None
        self._log_handle = None
        self._stop_lock = threading.Lock()

    # ------------------------------------------------------------ control
    def start(self) -> None:
        if self.proc is not None:
            raise RuntimeError(f"process {self.name!r} already started")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = self.log_dir / f"{self.name}-{stamp}-{uuid.uuid4().hex[:6]}.log"
        self._log_handle = self.log_path.open("w", encoding="utf-8", errors="replace")
        self.started_at = time.time()
        try:
            self.proc = subprocess.Popen(
                self.command,
                cwd=str(self.cwd) if self.cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,  # own process group: we only ever kill ours
            )
        except OSError as exc:
            # start failure (missing binary, permission denied): record it as a
            # finished, failed run instead of raising into the UI thread.
            self._log_handle.write(f"cannot start {self.command[0]}: {exc}\n")
            self._log_handle.close()
            self.exit_code = 127  # shell convention: command not found
            self.finished_at = time.time()
            self.queue.put(("exit", self.exit_code, ""))
            return
        self._reader = threading.Thread(
            target=self._read_loop, name=f"gui-proc-{self.name}", daemon=True)
        self._reader.start()

    def wait(self, timeout: float = 10.0) -> int | None:
        """Block until the exit event (helpers/tests; the UI never blocks)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.exit_code is not None:
                return self.exit_code
            time.sleep(0.05)
        return self.exit_code

    def _read_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                ts = time.time()
                self._tail.append((ts, line))
                self._log_handle.write(line)
                self._log_handle.flush()
                self.queue.put(("out", ts, line))
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            code = proc.wait()
            self.exit_code = code
            self.finished_at = time.time()
            self._log_handle.close()
            self.queue.put(("exit", code, ""))

    def poll(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def is_running(self) -> bool:
        return self.proc is not None and self.poll() is None

    # --------------------------------------------------------------- stop
    def stop(self, timeout: float = 8.0) -> bool:
        """Gracefully stop OUR process: SIGINT, then SIGTERM, then SIGKILL.

        Returns True when the process is no longer running afterwards.
        """
        with self._stop_lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return True
            try:
                proc.send_signal(signal.SIGINT)
            except (OSError, ProcessLookupError):
                return True
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                except (OSError, ProcessLookupError):
                    return True
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
            return proc.poll() is not None

    # -------------------------------------------------------------- reads
    def drain(self) -> list[tuple[str, float, str]]:
        """Pop all queued events (UI thread calls this from a poll loop)."""
        items: list[tuple[str, float, str]] = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except Empty:
                break
        return items

    def tail(self, limit: int = 200) -> list[tuple[float, str]]:
        """Last `limit` buffered lines (used by the Logs page)."""
        return list(self._tail)[-limit:]

    def read_log(self, limit: int = 2000) -> list[tuple[float, str]]:
        """Read the saved log file (finished processes)."""
        if self.log_path is None or not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [(self.started_at or 0.0, line) for line in lines[-limit:]]

    def describe(self) -> dict[str, object]:
        status = "running" if self.is_running() else (
            "finished" if self.exit_code is not None else "not started")
        return {
            "name": self.name,
            "command": " ".join(self.command),
            "status": status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_path": str(self.log_path) if self.log_path else "",
        }


def format_ts(ts: float) -> str:
    """Wall-clock timestamp for log lines."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_duration(start: float | None, end: float | None) -> str:
    if start is None:
        return "-"
    end = end if end is not None else time.time()
    return f"{end - start:.1f}s"
