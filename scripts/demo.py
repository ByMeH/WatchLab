#!/usr/bin/env python3
"""WatchLab end-to-end demonstration against a disposable, isolated etcd.

`python scripts/demo.py` performs, in one run:

  1. environment checks (Python/Go/etcd/etcdctl; builds bin/watchlab when
     missing or older than the Go sources);
  2. starts a private single-member etcd: a unique data-dir, free localhost
     client/peer ports, a unique cluster token. Compaction is only ever
     executed against this instance; the fixed ports 2379/2380 and any
     pre-existing etcd of the user are never used or touched;
  3. reconnect scenario: Watch -> Producer phase 1 -> wait until the watcher
     has all events -> stop only our own Watch process -> Producer phase 2 ->
     resume Watch from L+1 -> event-level validation (request_id, revision,
     sequence, key, SHA-256);
  4. compaction scenario: Producer phase 1 -> Recover (initial snapshot) ->
     stop Recover after WATCH_CREATED -> Producer phase 2 -> compact THIS
     etcd at the last phase-2 revision -> restart Recover with the same state
     file -> COMPACTED detected -> fresh snapshot -> Watch continues at R+1 ->
     Producer phase 3 -> the tail is validated per event;
  5. final-state verification: checkpoint revision, key count and per-key
     SHA-256 / mod_revision against the Producer acknowledgements.

Artifacts are written under /tmp/watchlab-demo-<demo-id>/ and are never
deleted automatically. The project's results/, reports/ and historical
experimental data are not modified. Only the Python standard library is used.

Exit codes: 0 - all scenarios PASS, 1 - failure, 130 - interrupted.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validation  # noqa: E402  (project module: canonical event-level matching rules)

DEMO_ROOT = Path("/tmp")
RESERVED_PORTS = {2379, 2380}  # the user's own test etcd may live there; never touch it
STOP_MARKERS = ("WATCH_STOPPED", "RECOVER_STOPPED", "PRODUCER_STOPPED", "PRODUCER_DONE")


class DemoError(RuntimeError):
    """Fatal demo failure with a human-readable message."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file; tolerate a truncated final line (interrupted write)."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for number, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            if number == len(lines):
                continue  # interrupted final write
            raise DemoError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def parse_go_duration(text: str) -> float:
    """Parse a Go-style duration such as 100ms, 2s, 1m into seconds."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", text.strip())
    if not match:
        raise DemoError(f"unsupported duration {text!r}; use forms like 100ms or 2s")
    unit = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2)]
    value = float(match.group(1)) * unit
    if value < 0 or value > 5.0:
        raise DemoError(f"--interval must be between 0 and 5s, got {text!r}")
    return value


def find_free_port(reserved: set[int]) -> int:
    """Ask the kernel for a free localhost port, avoiding reserved ports."""
    for _ in range(100):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in reserved and port not in RESERVED_PORTS:
            return port
    raise DemoError("could not find a free localhost port")


def assert_port_free(port: int) -> None:
    """Pre-flight bind probe; fails cleanly before etcd is even started.

    SO_REUSEADDR is set so TIME_WAIT leftovers do not produce false alarms;
    an active listener on the port still makes the bind fail.
    """
    probe = socket.socket()
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise DemoError(
            f"port {port} on 127.0.0.1 is busy ({exc}); refusing to start etcd on it"
        ) from exc
    finally:
        probe.close()


def binary_staleness(root: Path) -> str | None:
    """Return a human-readable reason to rebuild bin/watchlab, or None."""
    binary = root / "bin" / "watchlab"
    if not binary.is_file():
        return "binary missing"
    sources = [root / "main.go", root / "recover.go", root / "go.mod", root / "go.sum"]
    newer = [s.name for s in sources if s.is_file() and s.stat().st_mtime > binary.stat().st_mtime]
    if newer:
        return "older than " + ", ".join(newer)
    return None


def verify_state_matches(state: dict[str, Any], acks: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify the final recovery state against every Producer acknowledgement.

    For each acknowledged PUT: the key must exist in the state map, the stored
    value must hash to the acknowledged value_sha256, and the stored
    mod_revision must equal the acknowledged revision.
    """
    problems: list[str] = []
    keys: dict[str, Any] = state.get("keys", {})
    for ack in acks:
        key = str(ack.get("key"))
        entry = keys.get(key)
        if entry is None:
            problems.append(f"key missing from final state: {key}")
            continue
        digest = hashlib.sha256(str(entry.get("value", "")).encode("utf-8")).hexdigest()
        if digest != ack.get("value_sha256"):
            problems.append(f"value SHA-256 mismatch for {key}")
        stored_revision = entry.get("mod_revision")
        if stored_revision is None or int(stored_revision) != int(ack["revision"]):
            problems.append(
                f"mod_revision mismatch for {key}: state={stored_revision} producer={ack.get('revision')}"
            )
    extra = sorted(set(keys) - {str(a.get("key")) for a in acks})
    if extra:
        problems.append(f"unexpected keys in final state: {extra[:5]}")
    return {"ok": not problems, "checked_keys": len(acks), "problems": problems[:10]}


def tool_output(args: list[str], timeout: float = 30.0) -> str:
    """Run a short version/inspection command, requiring exit code 0."""
    try:
        proc = subprocess.run(
            [str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DemoError(f"command timed out after {timeout}s: {' '.join(str(a) for a in args)}") from exc
    if proc.returncode != 0:
        raise DemoError(
            f"command failed with exit code {proc.returncode}: {' '.join(str(a) for a in args)}\n{proc.stdout}"
        )
    return proc.stdout


def run_command(args: list[str], *, timeout: float = 60.0, cwd: Path | None = None) -> str:
    """Run a blocking command, stream its output, and require exit code 0."""
    printable = " ".join(str(a) for a in args)
    if cwd is not None:
        printable += f"  (cwd={cwd})"
    print("$ " + printable, flush=True)
    proc = subprocess.Popen(
        [str(a) for a in args],
        cwd=None if cwd is None else str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    collected: list[str] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            collected.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        return_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise DemoError(
            f"command timed out after {timeout}s: {printable}\n{''.join(collected[-20:])}"
        ) from None
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    if return_code != 0:
        raise DemoError(
            f"command failed with exit code {return_code}: {printable}\n{''.join(collected[-30:])}"
        )
    return "".join(collected)


class TeeStream:
    """Mirror a standard stream into the demo log file."""

    def __init__(self, primary: Any, mirror: Any):
        self._primary = primary
        self._mirror = mirror

    def write(self, data: str) -> int:
        self._primary.write(data)
        try:
            self._mirror.write(data)
        except ValueError:
            pass  # log file already closed
        return len(data)

    def flush(self) -> None:
        self._primary.flush()
        try:
            self._mirror.flush()
        except ValueError:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


def check_environment() -> dict[str, str]:
    """Verify Python/Go/etcd/etcdctl are available; collect version strings."""
    info: dict[str, str] = {"python": sys.version.split()[0]}
    if sys.version_info < (3, 10):
        raise DemoError(f"Python 3.10+ is required, found {info['python']}")
    for tool in ("go", "etcd", "etcdctl"):
        path = shutil.which(tool)
        if path is None:
            raise DemoError(f"required command not found in PATH: {tool}")
        info[tool] = path
    info["go_version"] = tool_output([info["go"], "version"]).strip()
    info["etcd_version"] = tool_output([info["etcd"], "--version"]).splitlines()[0].strip()
    info["etcdctl_version"] = tool_output([info["etcdctl"], "version"]).splitlines()[0].strip()
    return info


def ensure_binary(root: Path, go_path: str) -> Path:
    """Build bin/watchlab when missing or stale; smoke-test it with `help`."""
    binary = root / "bin" / "watchlab"
    if not (root / "go.mod").is_file():
        raise DemoError(f"not a WatchLab project root (no go.mod): {root}")
    reason = binary_staleness(root)
    if reason:
        print(f"Building bin/watchlab ({reason})", flush=True)
        run_command([go_path, "build", "-o", str(binary), "."], cwd=root, timeout=300)
    else:
        print(f"bin/watchlab is up to date: {binary}", flush=True)
    run_command([str(binary), "help"], timeout=30)
    return binary


class ManagedProcess:
    """A child process owned by this demo, with a console log and timeouts."""

    def __init__(self, name: str, args: list[str], console_path: Path):
        console_path.parent.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.args = [str(a) for a in args]
        self.console_path = console_path
        self.console_handle = console_path.open("w", encoding="utf-8")
        print(f"$ [{name}] " + " ".join(self.args), flush=True)
        self.proc = subprocess.Popen(
            self.args, stdout=self.console_handle, stderr=subprocess.STDOUT, text=True,
        )

    def poll(self) -> int | None:
        return self.proc.poll()

    def console_text(self) -> str:
        try:
            return self.console_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def _fail_with_log(self, message: str) -> DemoError:
        tail = "\n".join(self.console_text().splitlines()[-15:])
        return DemoError(f"{message}\n[{self.name} console: {self.console_path}]\n{tail}")

    def wait_for(self, needle: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.console_text():
                return
            code = self.proc.poll()
            if code is not None:
                raise self._fail_with_log(f"[{self.name}] exited with code {code} before {needle!r}")
            time.sleep(0.05)
        raise self._fail_with_log(f"[{self.name}] timed out after {timeout}s waiting for {needle!r}")

    def wait_until(self, predicate: Callable[[], Any | None], description: str, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value is not None:
                return value
            code = self.proc.poll()
            if code is not None:
                raise self._fail_with_log(
                    f"[{self.name}] exited with code {code} while waiting for {description}")
            time.sleep(0.05)
        raise self._fail_with_log(f"[{self.name}] timed out after {timeout}s waiting for {description}")

    def wait_jsonl_count(self, path: Path, record_type: str, count: int, timeout: float) -> list[dict[str, Any]]:
        def ready() -> list[dict[str, Any]] | None:
            records = read_jsonl(path)
            if sum(1 for r in records if r.get("type") == record_type) >= count:
                return records
            return None

        return self.wait_until(
            ready, f"{count} {record_type!r} records in {path.name}", timeout)

    def wait_exit(self, timeout: float = 30.0) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            raise self._fail_with_log(f"[{self.name}] did not exit within {timeout}s; stopped") from None
        if code != 0:
            raise self._fail_with_log(f"[{self.name}] exited with code {code}")
        return code

    def stop(self, timeout: float = 8.0) -> None:
        """Gracefully stop OUR process: SIGINT, then SIGTERM, then SIGKILL."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        self.close_console()
        text = self.console_text()
        if not any(marker in text for marker in STOP_MARKERS):
            print(
                f"NOTICE: [{self.name}] stopped without a normal stop marker; inspect {self.console_path}",
                flush=True,
            )

    def close_console(self) -> None:
        if not self.console_handle.closed:
            self.console_handle.close()


class EtcdInstance:
    """A disposable single-member etcd started by this demo on free localhost ports."""

    def __init__(self, demo_dir: Path, demo_id: str, client_port: int, peer_port: int,
                 etcd_path: str, etcdctl_path: str):
        self.name = f"watchlab-demo-{demo_id}"
        self.data_dir = demo_dir / "etcd-data"
        self.console_path = demo_dir / "etcd-console.log"
        self.client_port = client_port
        self.peer_port = peer_port
        self.endpoint = f"http://127.0.0.1:{client_port}"
        self.etcd_path = etcd_path
        self.etcdctl_path = etcdctl_path
        self.token = f"watchlab-demo-{demo_id}"
        self.proc: subprocess.Popen | None = None
        self._console_handle: Any | None = None

    def console_text(self) -> str:
        try:
            return self.console_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def poll(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def start(self, health_timeout: float = 30.0) -> None:
        if self.data_dir.exists():
            raise DemoError(f"refusing to start: data-dir {self.data_dir} already exists")
        args = [
            self.etcd_path,
            "--name", self.name,
            "--data-dir", str(self.data_dir),
            "--listen-client-urls", self.endpoint,
            "--advertise-client-urls", self.endpoint,
            "--listen-peer-urls", f"http://127.0.0.1:{self.peer_port}",
            "--initial-advertise-peer-urls", f"http://127.0.0.1:{self.peer_port}",
            "--initial-cluster", f"{self.name}=http://127.0.0.1:{self.peer_port}",
            "--initial-cluster-token", self.token,
            "--initial-cluster-state", "new",
        ]
        self._console_handle = self.console_path.open("w", encoding="utf-8")
        print("$ [etcd] " + " ".join(args), flush=True)
        self.proc = subprocess.Popen(
            args, stdout=self._console_handle, stderr=subprocess.STDOUT, text=True,
        )

        deadline = time.monotonic() + health_timeout
        healthy = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            probe = subprocess.run(
                [self.etcdctl_path, f"--endpoints={self.endpoint}", "endpoint", "health"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10,
            )
            if probe.returncode == 0:
                healthy = True
                break
            time.sleep(0.3)
        if not healthy:
            if self.proc is not None and self.proc.poll() is None:
                self.stop()
            tail = "\n".join(self.console_text().splitlines()[-15:])
            raise DemoError(
                f"isolated etcd did not become healthy within {health_timeout}s "
                f"({self.console_path}):\n{tail}"
            )
        run_command([self.etcdctl_path, f"--endpoints={self.endpoint}", "endpoint", "health"], timeout=15)

    def stop(self) -> int:
        """Stop OUR etcd via SIGTERM (clean shutdown); never touch other processes."""
        code: int | None = None
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                code = self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                code = self.proc.wait()
        elif self.proc is not None:
            code = self.proc.returncode
        if self._console_handle is not None and not self._console_handle.closed:
            self._console_handle.close()
        return 0 if code is None else code


class Demo:
    """Orchestrates the full WatchLab demonstration."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.demo_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        self.demo_dir = DEMO_ROOT / f"watchlab-demo-{self.demo_id}"
        self.tools: dict[str, str] = {}
        self.binary: Path | None = None
        self.etcd: EtcdInstance | None = None
        self.children: list[ManagedProcess] = []
        self.reports: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.started_monotonic = time.monotonic()
        self.started_at = utc_now()
        self._log_handle: Any | None = None
        self._stdout_orig = sys.stdout
        self._stderr_orig = sys.stderr
        self._cleaned = False

    # ------------------------------------------------------------- plumbing

    def mark(self, label: str) -> None:
        elapsed = time.monotonic() - self.started_monotonic
        self.steps.append({"step": label, "elapsed_s": round(elapsed, 3)})
        print(f"[t+{elapsed:6.2f}s] {label}", flush=True)

    def spawn(self, name: str, args: list[str], console_path: Path) -> ManagedProcess:
        child = ManagedProcess(name, args, console_path)
        self.children.append(child)
        return child

    def shutdown_child(self, child: ManagedProcess | None) -> None:
        if child is None:
            return
        try:
            if child.poll() is None:
                child.stop()
            else:
                child.close_console()
        except Exception as exc:  # cleanup must never mask the original error
            print(f"NOTICE: failed to stop {child.name}: {exc}", flush=True)
        if child in self.children:
            self.children.remove(child)

    def cleanup(self) -> None:
        """Stop every process this demo started; never touch foreign processes."""
        if self._cleaned:
            return
        self._cleaned = True
        for child in list(self.children):
            self.shutdown_child(child)
        if self.etcd is not None:
            code = self.etcd.stop()
            detail = (f"stopped by our SIGTERM (signal {-code})"
                      if isinstance(code, int) and code < 0 else f"exit code {code}")
            print(f"etcd stopped ({detail}); data-dir kept: {self.etcd.data_dir}", flush=True)

    def close_log(self) -> None:
        sys.stdout = self._stdout_orig
        sys.stderr = self._stderr_orig
        if self._log_handle is not None and not self._log_handle.closed:
            self._log_handle.close()

    def _install_tee(self) -> None:
        base = DEMO_ROOT / f"watchlab-demo-{self.demo_id}"
        candidate, suffix = base, 0
        while True:
            try:
                candidate.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                suffix += 1
                candidate = base.with_name(f"{base.name}-{suffix}")
        self.demo_dir = candidate
        self.log_path = self.demo_dir / "demo.log"
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        sys.stdout = TeeStream(sys.stdout, self._log_handle)
        sys.stderr = TeeStream(sys.stderr, self._log_handle)

    # ------------------------------------------------------------- prepare

    def prepare(self) -> None:
        self.tools = check_environment()  # before any artifact is created
        self._install_tee()
        print("=== WatchLab demonstration ===")
        print(f"python {self.tools['python']} | {self.tools['go_version']}")
        print(f"{self.tools['etcd_version']} | {self.tools['etcdctl_version']}")
        print(f"events per phase: {self.args.events}, interval: {self.args.interval}, "
              f"stage timeout: {self.args.timeout}s")
        self.mark("environment checked")
        self.binary = ensure_binary(PROJECT_ROOT, self.tools["go"])
        self.mark("binary ready")
        self.etcd = self._start_isolated_etcd()
        self.mark("isolated etcd healthy")

    def _start_isolated_etcd(self) -> EtcdInstance:
        client_port = self.args.client_port
        peer_port = self.args.peer_port
        if client_port is not None:
            require(client_port not in RESERVED_PORTS,
                    f"port {client_port} is a reserved WatchLab test port {RESERVED_PORTS}; "
                    "refusing to use it")
            assert_port_free(client_port)
        else:
            client_port = find_free_port(RESERVED_PORTS)
        if peer_port is not None:
            require(peer_port not in RESERVED_PORTS,
                    f"port {peer_port} is a reserved WatchLab test port {RESERVED_PORTS}; "
                    "refusing to use it")
            require(peer_port != client_port, "--peer-port must differ from --client-port")
            assert_port_free(peer_port)
        else:
            peer_port = find_free_port(RESERVED_PORTS | {client_port})

        instance = EtcdInstance(
            self.demo_dir, self.demo_id, client_port, peer_port,
            self.tools["etcd"], self.tools["etcdctl"],
        )
        print(f"isolation: fresh data-dir {instance.data_dir}")
        print(f"isolation: client port {client_port}, peer port {peer_port}, localhost only")
        print(f"isolation: initial-cluster-token {instance.token}")
        print("isolation: compaction will target this instance only; "
              "no pre-existing etcd is used")
        instance.start(health_timeout=max(self.args.timeout, 30.0))
        return instance

    # ------------------------------------------------------------- helpers

    def produce(self, base: Path, tag: str, run_id: str, prefix: str,
                count: int, interval: str) -> list[dict[str, Any]]:
        """Run our Producer to completion and return its put_ack records."""
        out_path = base / f"producer-{tag}.jsonl"
        limit = max(self.args.timeout, count * parse_go_duration(interval) + 30.0)
        producer = self.spawn(f"producer-{tag}", [
            str(self.binary), "produce",
            "--endpoint", self.etcd.endpoint,
            "--run-id", run_id,
            "--prefix", prefix,
            "--count", str(count),
            "--interval", interval,
            "--out", str(out_path),
        ], base / f"producer-{tag}-console.log")
        try:
            producer.wait_for("PRODUCER_DONE", timeout=limit)
            producer.wait_exit(timeout=15.0)
        finally:
            self.shutdown_child(producer)
        records = read_jsonl(out_path)
        require(len(records) == count,
                f"producer {tag}: expected {count} put_ack records, found {len(records)}")
        require(all(r.get("type") == "put_ack" for r in records),
                f"producer {tag}: unexpected record types in {out_path.name}")
        return records

    # ------------------------------------------------------------- scenario A

    def scenario_reconnect(self) -> dict[str, Any]:
        n = self.args.events
        interval = self.args.interval
        stage = self.args.timeout
        base = self.demo_dir / "reconnect"
        base.mkdir(parents=True, exist_ok=True)
        prefix = f"/watchlab/demo/{self.demo_id}/reconnect/events/"
        watch1: ManagedProcess | None = None
        watch2: ManagedProcess | None = None
        try:
            # 1. Watch up.
            watch1 = self.spawn("watch-initial", [
                str(self.binary), "watch",
                "--endpoint", self.etcd.endpoint,
                "--run-id", f"{self.demo_id}-recon-a",
                "--prefix", prefix,
                "--out", str(base / "watch-initial.jsonl"),
            ], base / "watch-initial-console.log")
            watch1.wait_for("WATCH_CREATED", stage)

            # 2. First small series; wait until the watcher has every event.
            acks1 = self.produce(base, "recon-p1", f"{self.demo_id}-recon-p1",
                                 prefix + "phase1/", n, interval)
            initial_records = watch1.wait_jsonl_count(
                base / "watch-initial.jsonl", "watch_event", n, stage)
            initial_events = [r for r in initial_records if r.get("type") == "watch_event"]
            require(len(initial_events) == n,
                    f"initial Watch recorded {len(initial_events)} events, expected {n}")
            self.mark(f"reconnect: phase 1 delivered ({n}/{n} events)")

            # 3. Stop only our own Watch process.
            watch1.stop()
            require(watch1.proc.returncode == 0,
                    f"initial Watch exited with code {watch1.proc.returncode} on SIGINT")

            # 4. Last fully processed revision, then the second series while nobody watches.
            last_seen = max(int(r["event_revision"]) for r in initial_events)
            last_acked = int(acks1[-1]["revision"])
            require(last_seen == last_acked,
                    f"watcher's last revision {last_seen} != last acknowledged revision {last_acked}")
            acks2 = self.produce(base, "recon-p2", f"{self.demo_id}-recon-p2",
                                 prefix + "phase2/", n, interval)
            self.mark(f"reconnect: phase 2 written while Watch was down (L={last_seen})")

            # 5. Resume from L+1 and confirm the second series arrives.
            watch2 = self.spawn("watch-resumed", [
                str(self.binary), "watch",
                "--endpoint", self.etcd.endpoint,
                "--run-id", f"{self.demo_id}-recon-b",
                "--prefix", prefix,
                "--start-revision", str(last_seen + 1),
                "--out", str(base / "watch-resumed.jsonl"),
            ], base / "watch-resumed-console.log")
            resumed_records = watch2.wait_jsonl_count(
                base / "watch-resumed.jsonl", "watch_event", n, stage)
            watch2.stop()
            require(watch2.proc.returncode == 0,
                    f"resumed Watch exited with code {watch2.proc.returncode} on SIGINT")

            # 6. Event-level validation (primary evidence; markers are supplementary).
            report = validation.validate_reconnect_split(acks1 + acks2, initial_records, resumed_records)
            require(report["ok"],
                    "reconnect validation failed: " + validation.format_anomalies(report))
            resumed_first = report["boundary"].get("resumed_first_revision")
            require(resumed_first == last_seen + 1,
                    f"resumed Watch first revision {resumed_first} != L+1 ({last_seen + 1})")

            markers = {
                "initial_watch_created": "WATCH_CREATED" in watch1.console_text(),
                "initial_watch_stopped": "WATCH_STOPPED" in watch1.console_text(),
                "resumed_watch_created": "WATCH_CREATED" in watch2.console_text(),
                "resumed_watch_stopped": "WATCH_STOPPED" in watch2.console_text(),
            }
            require(all(markers.values()), f"missing console markers: {markers}")

            return {
                "expected_events": 2 * n,
                "matched_events": report["matched"],
                "missing": report["missing_count"],
                "duplicates": report["duplicate_count"],
                "unexpected": report["unexpected_count"],
                "mismatches": report["mismatch_count"],
                "last_fully_processed_revision": last_seen,
                "resumed_from_revision": last_seen + 1,
                "resumed_first_event_revision": resumed_first,
                "console_markers": markers,
                "validation": report,
                "artifacts": {
                    "producer_phase1": str(base / "producer-recon-p1.jsonl"),
                    "producer_phase2": str(base / "producer-recon-p2.jsonl"),
                    "watch_initial": str(base / "watch-initial.jsonl"),
                    "watch_resumed": str(base / "watch-resumed.jsonl"),
                    "console_logs": str(base),
                },
                "summary_line": (
                    f"matched {report['matched']}/{2 * n} events "
                    f"(key/revision/sequence/SHA-256), no gaps; "
                    f"Watch resumed at revision {last_seen + 1} = L+1"
                ),
            }
        finally:
            self.shutdown_child(watch2)
            self.shutdown_child(watch1)

    # ------------------------------------------------------------- scenario B

    def scenario_compaction(self) -> dict[str, Any]:
        n = self.args.events
        interval = self.args.interval
        stage = self.args.timeout
        base = self.demo_dir / "compaction"
        base.mkdir(parents=True, exist_ok=True)
        prefix = f"/watchlab/demo/{self.demo_id}/compaction/events/"
        state_path = base / "state.json"
        recover1: ManagedProcess | None = None
        recover2: ManagedProcess | None = None
        try:
            # 1. Producer phase 1.
            acks1 = self.produce(base, "comp-p1", f"{self.demo_id}-comp-p1",
                                 prefix + "phase1/", n, interval)
            phase1_last = int(acks1[-1]["revision"])

            # 2. Recover starts, takes the initial snapshot and watches.
            recover1 = self.spawn("recover-initial", [
                str(self.binary), "recover",
                "--endpoint", self.etcd.endpoint,
                "--run-id", f"{self.demo_id}-comp-a",
                "--prefix", prefix,
                "--state", str(state_path),
                "--out", str(base / "recover-initial.jsonl"),
            ], base / "recover-initial-console.log")
            recover1.wait_for("RECOVER_WATCH_CREATED", stage)
            initial_log = read_jsonl(base / "recover-initial.jsonl")
            initial_snapshots = [r for r in initial_log if r.get("type") == "snapshot"]
            require(len(initial_snapshots) == 1,
                    f"initial Recover journal must contain exactly one snapshot record, "
                    f"got {len(initial_snapshots)}")
            require(sum(1 for r in initial_log if r.get("type") == "watch_created") == 1,
                    "initial Recover journal must contain exactly one watch_created record")
            require(not [r for r in initial_log if r.get("type") == "watch_event"],
                    "initial Recover journal must not contain events before phase 2")
            state1 = self._load_state(state_path)
            require(state1["prefix"] == prefix, "state prefix mismatch")
            require(int(state1["snapshot_revision"]) == phase1_last,
                    f"initial snapshot revision {state1['snapshot_revision']} != "
                    f"last phase-1 revision {phase1_last}")
            require(int(state1["last_revision"]) == phase1_last,
                    f"initial checkpoint {state1['last_revision']} != {phase1_last}")
            require(len(state1["keys"]) == n,
                    f"initial snapshot must contain {n} keys, has {len(state1['keys'])}")
            self.mark(f"compaction: initial snapshot at revision {phase1_last} ({n} keys)")

            # 3. Stop our Recover after WATCH_CREATED.
            recover1.stop()
            require(recover1.proc.returncode == 0,
                    f"initial Recover exited with code {recover1.proc.returncode} on SIGINT")

            # 4. Producer phase 2 while Recover is down.
            acks2 = self.produce(base, "comp-p2", f"{self.demo_id}-comp-p2",
                                 prefix + "phase2/", n, interval)
            compact_revision = int(acks2[-1]["revision"])
            self.mark(f"compaction: phase 2 written, compacting at revision {compact_revision}")

            # 5. Compact THIS etcd instance at the last phase-2 revision.
            compact_output = run_command([
                self.etcd.etcdctl_path, f"--endpoints={self.etcd.endpoint}",
                "compact", str(compact_revision),
            ], timeout=30)
            (base / "etcdctl-compact.log").write_text(compact_output, encoding="utf-8")
            reported = re.search(r"compacted revision (\d+)", compact_output)
            if reported:
                require(int(reported.group(1)) == compact_revision,
                        f"etcdctl compacted revision {reported.group(1)}, expected {compact_revision}")
            else:
                print(f"NOTICE: unexpected etcdctl compact output format: {compact_output.strip()!r}",
                      flush=True)

            # 6. Restart Recover with the same state file; it must detect COMPACTED,
            #    take a fresh snapshot and resume the Watch at R+1.
            recover2 = self.spawn("recover-after-compaction", [
                str(self.binary), "recover",
                "--endpoint", self.etcd.endpoint,
                "--run-id", f"{self.demo_id}-comp-b",
                "--prefix", prefix,
                "--state", str(state_path),
                "--out", str(base / "recover-after.jsonl"),
            ], base / "recover-after-console.log")
            recover2.wait_for("RECOVER_COMPACTED", stage)

            def journal_ready() -> dict[str, Any] | None:
                records = read_jsonl(base / "recover-after.jsonl")
                canceled = [r for r in records if r.get("type") == "watch_canceled"]
                snapshots = [r for r in records if r.get("type") == "snapshot"]
                created = [r for r in records if r.get("type") == "watch_created"]
                if len(canceled) == 1 and len(snapshots) == 1 and len(created) >= 2:
                    return {"records": records, "canceled": canceled,
                            "snapshots": snapshots, "created": created}
                return None

            ready = recover2.wait_until(
                journal_ready,
                "compaction journal (1 watch_canceled, 1 snapshot, >=2 watch_created)",
                stage,
            )
            snapshot_record = ready["snapshots"][0]
            canceled_record = ready["canceled"][0]
            require(int(canceled_record.get("compact_revision", -1)) == compact_revision,
                    f"watch_canceled compact_revision {canceled_record.get('compact_revision')} "
                    f"!= compacted revision {compact_revision}")
            require(int(snapshot_record["snapshot_revision"]) == compact_revision,
                    f"fresh snapshot revision {snapshot_record['snapshot_revision']} != "
                    f"compacted revision {compact_revision}")
            self.mark(f"compaction: COMPACTED detected; fresh snapshot at revision "
                      f"{snapshot_record['snapshot_revision']} ({snapshot_record.get('key_count')} keys)")

            # 7. Producer phase 3; the replacement Watch must deliver it.
            acks3 = self.produce(base, "comp-p3", f"{self.demo_id}-comp-p3",
                                 prefix + "phase3/", n, interval)
            recover2.wait_jsonl_count(base / "recover-after.jsonl", "watch_event", n, stage)
            recover2.stop()
            require(recover2.proc.returncode == 0,
                    f"restarted Recover exited with code {recover2.proc.returncode} on SIGINT")
            self.mark(f"compaction: phase 3 delivered to the replacement Watch ({n}/{n} events)")

            # 8. Event-level validation of the tail (primary evidence).
            records_after = read_jsonl(base / "recover-after.jsonl")
            tail = validation.validate_compaction_tail(
                acks3, records_after,
                phase2_prefix=prefix + "phase3/",
                expected_snapshot_keys=2 * n,  # the fresh snapshot covers phases 1+2
            )
            require(tail["ok"],
                    "compaction-tail validation failed: " + validation.format_anomalies(tail))

            # 9. Final state: checkpoint, key count, per-key SHA-256 and mod_revision.
            state2 = self._load_state(state_path)
            final_checkpoint = int(state2["last_revision"])
            last_acked = int(acks3[-1]["revision"])
            require(final_checkpoint == last_acked,
                    f"final checkpoint {final_checkpoint} != last producer revision {last_acked}")
            require(len(state2["keys"]) == 3 * n,
                    f"final state must contain {3 * n} keys, has {len(state2['keys'])}")
            state_checks = verify_state_matches(state2, acks1 + acks2 + acks3)
            require(state_checks["ok"],
                    "final state verification failed: " + "; ".join(state_checks["problems"]))

            console = recover2.console_text()
            markers = {
                "state_loaded": "RECOVER_STATE_LOADED" in console,
                "compacted_detected": "RECOVER_COMPACTED" in console,
                "fresh_snapshot": "RECOVER_SNAPSHOT" in console,
                "watch_resumed": "RECOVER_WATCH_CREATED" in console,
                "graceful_stop": "RECOVER_STOPPED" in console,
            }
            require(all(markers.values()), f"missing console markers: {markers}")

            return {
                "expected_tail_events": n,
                "matched_events": tail["matched"],
                "missing": tail["missing_count"],
                "duplicates": tail["duplicate_count"],
                "unexpected": tail["unexpected_count"],
                "mismatches": tail["mismatch_count"],
                "compact_revision": compact_revision,
                "snapshot_revision": int(snapshot_record["snapshot_revision"]),
                "snapshot_key_count": int(snapshot_record.get("key_count", -1)),
                "new_watch_start_revision": tail["boundary"].get("new_watch_start_revision"),
                "final_state": {
                    "last_revision": final_checkpoint,
                    "snapshot_revision": int(state2["snapshot_revision"]),
                    "key_count": len(state2["keys"]),
                    "data_bytes": state2.get("data_bytes"),
                },
                "state_checks": state_checks,
                "console_markers": markers,
                "validation": tail,
                "artifacts": {
                    "producer_phase1": str(base / "producer-comp-p1.jsonl"),
                    "producer_phase2": str(base / "producer-comp-p2.jsonl"),
                    "producer_phase3": str(base / "producer-comp-p3.jsonl"),
                    "recover_initial": str(base / "recover-initial.jsonl"),
                    "recover_after": str(base / "recover-after.jsonl"),
                    "state_file": str(state_path),
                    "compact_log": str(base / "etcdctl-compact.log"),
                    "console_logs": str(base),
                },
                "summary_line": (
                    f"compact_revision={compact_revision}, fresh snapshot at "
                    f"{snapshot_record['snapshot_revision']} with "
                    f"{snapshot_record.get('key_count')} keys, tail matched "
                    f"{tail['matched']}/{n}; Watch resumed at "
                    f"{tail['boundary'].get('new_watch_start_revision')} = R+1"
                ),
            }
        finally:
            self.shutdown_child(recover2)
            self.shutdown_child(recover1)

    def _load_state(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DemoError(f"cannot read recovery state {path}: {exc}") from exc

    # ------------------------------------------------------------- scenarios

    def run_one(self, name: str, func: Callable[[], dict[str, Any]]) -> None:
        print(f"\n=== SCENARIO: {name} ===", flush=True)
        started = time.monotonic()
        try:
            report = func()
            report["scenario"] = name
            report["status"] = "PASS"
        except Exception as exc:  # a failing scenario must not abort the summary
            report = {"scenario": name, "status": "FAIL",
                      "error": f"{type(exc).__name__}: {exc}"}
            print(f"ERROR in {name}: {exc}", flush=True)
            if not isinstance(exc, DemoError):
                traceback.print_exc()
        report["wall_seconds"] = round(time.monotonic() - started, 3)
        self.reports.append(report)
        status = report["status"]
        detail = report.get("summary_line", report.get("error", ""))
        print(f"--- {name}: {status} ({report['wall_seconds']}s) {detail}", flush=True)

    def run_scenarios(self) -> None:
        self.run_one("reconnect", self.scenario_reconnect)
        if self.etcd is not None and self.etcd.poll() is None:
            self.run_one("compaction-recovery", self.scenario_compaction)
        else:
            self.reports.append({
                "scenario": "compaction-recovery", "status": "FAIL",
                "error": "isolated etcd is not running; scenario skipped",
            })
            print("ERROR: isolated etcd is not running; compaction scenario skipped", flush=True)

    # ------------------------------------------------------------- summary

    def artifact_listing(self) -> dict[str, str]:
        listing: dict[str, str] = {}
        for path in sorted(self.demo_dir.rglob("*")):
            if path.is_file() and "etcd-data" not in path.parts:
                listing[str(path.relative_to(self.demo_dir))] = f"{path.stat().st_size} bytes"
        return listing

    def write_summary(self) -> int:
        expected = {"reconnect", "compaction-recovery"}
        present = {r.get("scenario") for r in self.reports}
        overall = expected.issubset(present) and all(r.get("status") == "PASS" for r in self.reports)
        total_matched = sum(int(r.get("matched_events", 0)) for r in self.reports)
        compaction = next((r for r in self.reports if r.get("scenario") == "compaction-recovery"), {})
        finished_at = utc_now()
        summary = {
            "demo_id": self.demo_id,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "overall": "PASS" if overall else "FAIL",
            "command": "python scripts/demo.py " + " ".join(
                f"{k}={v}" for k, v in vars(self.args).items() if v is not None),
            "versions": {
                "python": self.tools.get("python"),
                "go": self.tools.get("go_version"),
                "etcd": self.tools.get("etcd_version"),
                "etcdctl": self.tools.get("etcdctl_version"),
            },
            "isolation": {
                "endpoint": self.etcd.endpoint if self.etcd else None,
                "client_port": self.etcd.client_port if self.etcd else None,
                "peer_port": self.etcd.peer_port if self.etcd else None,
                "data_dir": str(self.etcd.data_dir) if self.etcd else None,
                "initial_cluster_token": self.etcd.token if self.etcd else None,
                "bound_to": "127.0.0.1 only",
                "reserved_ports_avoided": sorted(RESERVED_PORTS),
                "compaction_scope": "this demo's own etcd instance only",
                "preexisting_etcd_touched": False,
            },
            "events_per_phase": self.args.events,
            "interval": self.args.interval,
            "scenarios": self.reports,
            "total_verified_events": total_matched,
            "total_verified_state_keys": (
                compaction.get("state_checks", {}).get("checked_keys", 0)),
            "final_checkpoint": compaction.get("final_state"),
            "steps": self.steps,
            "artifacts": self.artifact_listing(),
            "paths": {
                "demo_dir": str(self.demo_dir),
                "demo_log": str(self.log_path),
                "etcd_log": str(self.etcd.console_path) if self.etcd else None,
                "etcd_data_dir": str(self.etcd.data_dir) if self.etcd else None,
                "state_file": str(self.demo_dir / "compaction" / "state.json"),
            },
            "note": (
                "Single-node local demonstration on a disposable etcd; "
                "descriptive only, not a benchmark. Matched events are validated "
                "per request_id against producer acknowledgements (key, revision, "
                "sequence, SHA-256). snapshot data_bytes is the sum of key+value "
                "UTF-8 lengths, not the on-disk JSON size."
            ),
        }
        summary_path = self.demo_dir / "demo-summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        print("\n=== WATCHLAB DEMO SUMMARY ===", flush=True)
        for report in self.reports:
            print(f"{report.get('scenario', '?'):<20}: {report.get('status', '?')}  "
                  f"{report.get('summary_line', report.get('error', ''))}", flush=True)
        print(f"total verified events : {total_matched} matched by request_id "
              f"(key/revision/sequence/SHA-256)", flush=True)
        final_state = compaction.get("final_state")
        if final_state:
            print(f"final checkpoint      : last_revision={final_state['last_revision']}, "
                  f"snapshot_revision={final_state['snapshot_revision']}, "
                  f"keys={final_state['key_count']}", flush=True)
        print(f"overall               : {summary['overall']}", flush=True)
        print(f"demo directory (kept) : {self.demo_dir}", flush=True)
        print(f"full demo log         : {self.log_path}", flush=True)
        if self.etcd is not None:
            print(f"etcd log              : {self.etcd.console_path}", flush=True)
            print(f"etcd data-dir         : {self.etcd.data_dir}", flush=True)
        print(f"recovery state file   : {self.demo_dir / 'compaction' / 'state.json'}", flush=True)
        print(f"machine summary       : {summary_path}", flush=True)
        return 0 if overall else 1



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="WatchLab end-to-end demonstration on an isolated, disposable etcd instance.")
    parser.add_argument("--events", type=int, default=5,
                        help="events per phase, 1..100 (default: 5)")
    parser.add_argument("--interval", default="100ms",
                        help="delay between writes passed to Producer (default: 100ms)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for each stage (default: 30)")
    parser.add_argument("--client-port", type=int, default=None,
                        help="test hook: fixed client port instead of an auto-selected free port")
    parser.add_argument("--peer-port", type=int, default=None,
                        help="test hook: fixed peer port instead of an auto-selected free port")
    args = parser.parse_args(argv)
    if not 1 <= args.events <= 100:
        parser.error("--events must be between 1 and 100")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    def _sigterm_to_interrupt(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGTERM, _sigterm_to_interrupt)

    demo = Demo(args)
    atexit.register(demo.cleanup)
    exit_code = 1
    try:
        demo.prepare()
        demo.run_scenarios()
        exit_code = demo.write_summary()
    except KeyboardInterrupt:
        print("\nDEMO INTERRUPTED — cleaning up", flush=True)
        exit_code = 130
    except DemoError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        exit_code = 1
    except Exception as exc:  # unexpected bug: show the traceback honestly
        traceback.print_exc()
        print(f"ERROR: unexpected {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        try:
            demo.cleanup()
        finally:
            demo.close_log()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
