#!/usr/bin/env bash
# Install AutoCut dependencies (macOS / Linux) using uv
set -euo pipefail

if ! command -v uv &>/dev/null; then
    echo "uv not found — installing via official installer"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> uv $(uv --version)"
echo "==> Syncing dependencies"
uv sync

echo ""
echo "Done. With DaVinci Resolve open, run:"
echo "  Workspace → Scripts → Utility → AutoCut Bridge   (free version)"
echo "or launch the app directly:"
echo "  uv run python main.py"
