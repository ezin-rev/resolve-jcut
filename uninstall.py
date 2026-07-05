"""
Remove everything AutoCut installed outside this repo folder.

    uv run python uninstall.py             # remove Resolve/launchd integration
    uv run python uninstall.py --purge     # also remove ~/.autocut (presets,
                                           # converted models — can be GBs)
    uv run python uninstall.py --dry-run   # show what would be removed

Removes: the bridge and Studio scripts from Resolve's Scripts menu, the
macOS launch agent, the bridge exchange dir, and the in-Resolve settings
file. Leaves the repo itself and the shared HuggingFace cache
(~/.cache/huggingface) alone. A running bridge is asked to stop and a
running app is closed first.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from resolve_bridge import (_LAUNCH_AGENT, _RESOLVE_SCRIPTS_DIRS,
                            _resolve_home)

DRY = "--dry-run" in sys.argv
PURGE = "--purge" in sys.argv


def _log(action: str, target) -> None:
    print(f"{'would ' if DRY else ''}{action}: {target}")


def _rm(path: Path) -> None:
    if not path.exists():
        return
    _log("remove", path)
    if DRY:
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def main() -> None:
    bridge_dir = _resolve_home() / ".autocut_bridge"

    # close a running app
    try:
        pid = int((bridge_dir / "gui.pid").read_text())
        _log("stop AutoCut app (pid)", pid)
        if not DRY:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass

    # ask a running bridge to stop
    if (bridge_dir / "owner").exists():
        _log("stop bridge inside Resolve", bridge_dir)
        if not DRY:
            try:
                (bridge_dir / "request.json").write_text(json.dumps(
                    {"id": str(uuid.uuid4()), "action": "stop", "params": {}}))
                time.sleep(1.0)
            except OSError:
                pass

    # launchd agent (macOS)
    if sys.platform == "darwin" and _LAUNCH_AGENT.exists():
        _log("unload launch agent", _LAUNCH_AGENT)
        if not DRY:
            subprocess.run(["launchctl", "unload", str(_LAUNCH_AGENT)],
                           capture_output=True)
        _rm(_LAUNCH_AGENT)

    # scripts in Resolve's menu
    scripts_dir = _RESOLVE_SCRIPTS_DIRS.get(sys.platform)
    if scripts_dir:
        _rm(scripts_dir / "AutoCut Bridge.py")
        _rm(scripts_dir / "AutoCut Studio.py")

    # bridge exchange dir + in-Resolve settings
    _rm(bridge_dir)
    _rm(_resolve_home() / ".autocut_settings.json")

    # user data (presets, locally converted models, logs)
    autocut_home = Path.home() / ".autocut"
    if PURGE:
        _rm(autocut_home)
    elif autocut_home.exists():
        print(f"kept: {autocut_home} (presets/models — remove with --purge)")

    print("done." if not DRY else "dry run — nothing was removed.")
    print("The repo folder itself and ~/.cache/huggingface are untouched.")


if __name__ == "__main__":
    main()
