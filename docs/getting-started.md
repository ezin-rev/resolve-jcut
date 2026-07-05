# Getting Started

## Requirements

- **DaVinci Resolve 18+**, free or Studio. Tested on Resolve 21 free from the
  Mac App Store — the most restricted variant there is.
- **Python 3.12+** managed by [uv](https://docs.astral.sh/uv/) for the app.
- For Resolve's own script runner on macOS: Python from
  [python.org](https://www.python.org/downloads/). Homebrew and Xcode
  command-line-tools Pythons are invisible to Resolve.
- macOS is the primary platform (Apple Silicon gets GPU transcription).
  Windows/Linux bridge paths are implemented but untested — reports welcome.

## Install

```bash
git clone <this repo> && cd autocut
./install.sh          # installs uv if needed, then dependencies
```

On Windows, run `install.bat` instead.

Then open DaVinci Resolve with a project, and run
**Workspace → Scripts → Utility → AutoCut Bridge**. The app installs the
bridge script on first connect — if the menu entry is missing, run
`uv run python main.py` once, click **Connect**, and it appears.

On macOS the app opens automatically whenever you run the bridge from the
menu — one click is the whole startup. (A user LaunchAgent watches a trigger
file; see [Architecture](developer/index.md#one-click-launch-macos) for how
that works and why it's needed.)

## First run

1. Open the timeline you want to cut. It is read as-is; video/audio track 1
   by default.
2. Pick a preset — *Talking Head (J-cut)* is the usual starting point.
3. **Analyze (preview)** — review the cut list on the Preview tab. Each row
   shows what will be removed and why (`silence (-51 dB)`, `filler 'um'`).
   Click a row's ✓ to keep that section instead of cutting it.
4. **▶ Build Timeline** — a new timeline named `<name> [AutoCut hh.mm.ss]`
   appears next to your original, cuts landing clip by clip.

Or skip the preview with **▶ Run AutoCut**.

!!! note "Analyze and Build are checked against each other"
    Build refuses to run if the current Resolve timeline changed since
    Analyze, and warns (but proceeds) if you changed detection settings in
    between. Re-run Analyze after switching timelines.

## Uninstall

```bash
uv run python uninstall.py            # removes the Resolve scripts, launch agent, bridge files
uv run python uninstall.py --purge    # also removes ~/.autocut (presets + converted models)
```

Add `--dry-run` to preview what would be removed. The repo folder and the
shared Hugging Face model cache are never touched.

## Studio version

Resolve Studio allows external scripting, so the app connects directly —
no bridge script needed. There is also a separate in-Resolve script
(`autocut_script.py`, installed by `uv run python install.py` as
"AutoCut Studio") with a silence-only, review-by-clip-color workflow. It
does **not** work on the free Mac App Store build.
