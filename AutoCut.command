#!/bin/zsh
# Launches the AutoCut GUI. Double-clickable in Finder, and run by the
# launchd agent when AutoCut Bridge is started inside DaVinci Resolve.
# launchd provides a minimal PATH — add the usual uv locations.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")" && exec uv run python main.py --verbose
