#!/usr/bin/env python3
"""WatchLab Desktop — graphical frontend for the WatchLab etcd research bench.

Run from the project root:
    python scripts/gui.py

The GUI is a frontend and orchestrator only: Go (bin/watchlab) stays the
backend for etcd interaction, scripts/demo.py stays the demo orchestrator,
scripts/analyze_results.py stays the analytics tool. Requires Tkinter from
the Python standard library; no extra pip dependencies.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent


def check_tkinter() -> None:
    """Fail with a clear message when the Tkinter system package is missing."""
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        print("WatchLab Desktop requires Tkinter, which is not available in this "
              f"Python build ({sys.version.split()[0]}).", file=sys.stderr)
        print("Install the system package for your distribution, e.g.:", file=sys.stderr)
        print("  Debian/Ubuntu : sudo apt install python3-tk", file=sys.stderr)
        print("  Fedora        : sudo dnf install python3-tkinter", file=sys.stderr)
        print("  Arch          : sudo pacman -S tk", file=sys.stderr)
        print(f"detail: {exc}", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    check_tkinter()
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from gui.app import WatchLabApp
    except Exception as exc:
        print(f"Cannot start WatchLab Desktop: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    app = WatchLabApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
