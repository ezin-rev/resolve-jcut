#!/usr/bin/env python3
"""
AutoCut for DaVinci Resolve
Entry point — launch this script while DaVinci Resolve is running.

    uv run python main.py

Free version: run Workspace → Scripts → Utility → AutoCut Bridge inside
Resolve first, then click Connect. Studio connects via the API directly.
"""

import logging
import sys

# Only our own logger — keeps numba/librosa off the root handler
logging.getLogger("autocut").setLevel(logging.INFO)
logging.getLogger("autocut").addHandler(logging.StreamHandler(sys.stdout))


def _already_running() -> bool:
    try:
        import os
        from resolve_bridge import BRIDGE_DIR
        pid = int((BRIDGE_DIR / "gui.pid").read_text())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def main() -> None:
    if _already_running():
        print("AutoCut is already running.")
        return
    try:
        from gui.app import AutoCutApp
    except ImportError as exc:
        sys.exit(f"Import error: {exc}\nRun:  uv sync")

    app = AutoCutApp(verbose="--verbose" in sys.argv)
    app.mainloop()


if __name__ == "__main__":
    main()
