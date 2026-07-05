# AutoCut for DaVinci Resolve

Automatic silence removal with **real J-cuts** — for the **free** version of
DaVinci Resolve, which normally can't be scripted from outside at all.

Point it at your timeline and it builds a new one next to it: silences cut,
and at every edit the previous shot **holds** over the start of your next
line, so you hear the new sentence before the picture cuts. Your voice track
is never moved or padded — the J-cut comes from trimming video, the way an
editor would do it by hand.

*Not affiliated with the commercial AutoCut plugin — this is an independent
open-source tool.*

## Features

- **Silence removal** with per-file **auto threshold** (measures each clip's
  noise floor — works on camera audio where a fixed dB threshold finds nothing)
- **Real J-cuts** in the built timeline (configurable lead, off by default)
- **Preview before cutting**: analyze first, see every planned cut with its
  reason, toggle any of them off, then build
- **Whisper AI features** (all offline): filler-word removal, SRT captions
  remapped to the cut timeline, chapter markers on long pauses
- **Apple GPU transcription** via mlx-whisper, including Thai fine-tuned
  models; caption line-breaking is Thai-aware (never splits า/ำ from the word)
- **Presets** with built-in templates; progress bar and cancellable runs
- **FCPXML export** of the same edit for Premiere Pro / Final Cut interchange

## How it works on the free version

Free Resolve blocks external scripting — but scripts run from its own menu
are allowed. AutoCut installs a tiny **bridge script** into Resolve's Scripts
menu; the desktop app talks to it through JSON files. One click on
`Workspace → Scripts → Utility → AutoCut Bridge` starts the bridge **and
opens the app** (macOS), which connects automatically.

There is no razor/trim API in any Resolve edition, so "editing" means
building a **new** timeline from your current one — your original is never
modified.

## Requirements

- DaVinci Resolve 18+ (free or Studio). Tested on Resolve 21 free from the
  Mac App Store — the most restricted variant there is.
- Python 3.12+ managed by [uv](https://docs.astral.sh/uv/) for the app
- For Resolve's script runner on macOS: Python from
  [python.org](https://www.python.org/downloads/) (Homebrew/Xcode Pythons are
  invisible to Resolve)
- macOS is the primary platform (Apple Silicon gets GPU transcription).
  Windows/Linux bridge paths are implemented but untested — reports welcome.

## Install

```bash
git clone <this repo> && cd autocut
./install.sh          # installs uv if needed, then dependencies
```

Then open DaVinci Resolve with a project, and run
**Workspace → Scripts → Utility → AutoCut Bridge** (the app installs the
script on first connect; run `uv run python main.py` once if the menu entry
is missing). On macOS the app opens automatically from then on — one click
is the whole startup.

### Uninstall

```bash
uv run python uninstall.py            # removes the Resolve scripts, launch agent, bridge files
uv run python uninstall.py --purge    # also removes ~/.autocut (presets + converted models)
```

Add `--dry-run` to preview. The repo folder and the shared HuggingFace model
cache are never touched.

## Usage

1. Open the timeline you want to cut (it is read as-is; video/audio track 1
   by default).
2. Pick a preset — *Talking Head (J-cut)* is the usual starting point.
3. **Analyze (preview)** — review the cut list on the Preview tab. Click a
   row's ✓ to keep that section instead of cutting it.
4. **▶ Build Timeline** — a new timeline named `<name> [AutoCut hh.mm.ss]`
   appears, cuts landing clip by clip.

Or skip the preview with **▶ Run AutoCut**.

### Thai transcription (and other languages)

The model box accepts any
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) model name or
HuggingFace repo, plus `mlx:` prefixed repos for Apple-GPU inference.
Included aliases:

| Alias | What it is |
|---|---|
| `base` / `medium` / … | vanilla Whisper, CPU |
| `large-v3-turbo-mlx` | fast multilingual, Apple GPU |
| `thai-distill-mlx` | distilled [Thonburian Whisper](https://github.com/biodatlab/thonburian-whisper) Thai fine-tune, Apple GPU |
| `thai-large-v3-mlx` | **full** Thonburian Thai model, Apple GPU — best Thai accuracy, needs a one-time local conversion (below) |

Converting the full Thai model (no published MLX build exists):

```bash
curl -sLO https://raw.githubusercontent.com/ml-explore/mlx-examples/main/whisper/convert.py
uv run python convert.py --torch-name-or-path biodatlab/whisper-th-large-v3-combined \
    --mlx-path ~/.autocut/models/thai-large-v3-mlx --dtype float16
mv ~/.autocut/models/thai-large-v3-mlx/model.safetensors \
   ~/.autocut/models/thai-large-v3-mlx/weights.safetensors
```

If the conversion isn't present, `thai-large-v3-mlx` falls back to the
distilled model automatically.

## Limitations (honest ones)

- The built timeline is new; nothing is trimmed in place (no such API exists).
- On the free Mac App Store build, importing the FCPXML back into **Resolve**
  yields offline media — FCPXML mode is for Premiere/FCP; Resolve uses the
  live-append mode, which is the default.
- Captions: the app tries to place the SRT on a subtitle track; if your build
  refuses, the SRT lands in the media pool and one drag finishes the job.
- J-cut holds only happen between segments of the same source clip (that's
  where the removed silence provides the video to hold on).

## Development

```bash
uv run python tests/test_autocut.py    # offline suite, no Resolve needed
```

Full documentation — pipeline internals, every parameter, the bridge
protocol, and the Resolve API quirks this tool works around (sandboxed
script processes, FCPXML importer behavior, Tk 9 threading) — lives in
[`docs/`](docs/index.md), published via GitHub Pages. Preview locally with
`uv run --group docs mkdocs serve`.

## Credits

- Workflow inspired by [YourAverageMo/auto-silence-cut](https://github.com/YourAverageMo/auto-silence-cut)
- Thai models by [biodatlab (Thonburian Whisper)](https://github.com/biodatlab/thonburian-whisper)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
  [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper),
  [librosa](https://librosa.org/)

MIT licensed — see [LICENSE](LICENSE).
