"""
Install the STUDIO in-Resolve script:  uv run python install.py

Copies autocut_script.py → "<Resolve scripts>/Utility/AutoCut Studio.py"
with this repo's absolute path baked in. Free-version users don't need
this — the external GUI (`uv run python main.py`) installs its own bridge
script automatically on connect.
"""

import sys
from pathlib import Path

from resolve_bridge import _RESOLVE_SCRIPTS_DIRS


def main() -> None:
    repo = Path(__file__).resolve().parent
    scripts_dir = _RESOLVE_SCRIPTS_DIRS.get(sys.platform)
    if scripts_dir is None:
        sys.exit(f"Unsupported platform: {sys.platform}")
    scripts_dir.mkdir(parents=True, exist_ok=True)

    src = (repo / "autocut_script.py").read_text()
    baked = src.replace("__REPO__", str(repo))
    if baked == src:
        sys.exit("No __REPO__ placeholder found — template corrupted?")

    (scripts_dir / "AutoCut.py").unlink(missing_ok=True)  # pre-rename install
    dst = scripts_dir / "AutoCut Studio.py"
    dst.write_text(baked)
    print(f"Installed: {dst}")
    print("In Resolve STUDIO: Workspace → Scripts → Utility → AutoCut Studio")


if __name__ == "__main__":
    main()
