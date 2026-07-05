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

- **Silence removal** with per-file **auto threshold** — measures each clip's
  noise floor, so it works on camera audio where a fixed dB threshold finds
  nothing
- **Real J-cuts** in the built timeline (configurable lead, off by default)
- **Preview before cutting**: analyze first, see every planned cut with its
  reason, toggle any of them off, then build
- **Whisper AI features** (all offline): filler-word removal, SRT captions
  remapped to the cut timeline, chapter markers on long pauses
- **Apple GPU transcription** via mlx-whisper, including Thai fine-tuned
  models; caption line-breaking is Thai-aware
- **Presets** with built-in templates; progress bar and cancellable runs
- **FCPXML export** of the same edit for Premiere Pro / Final Cut interchange

## How the free-version support works

Free Resolve blocks external scripting — but scripts run from its own menu
are allowed. AutoCut installs a tiny **bridge script** into Resolve's Scripts
menu; the desktop app talks to it through JSON files. One click on
*Workspace → Scripts → Utility → AutoCut Bridge* starts the bridge and (on
macOS) opens the app, which connects automatically.

There is no razor/trim API in any Resolve edition, so "editing" means
building a **new** timeline from your current one — your original is never
modified.

## Where to go next

- [Getting Started](getting-started.md) — install, first run, uninstall
- [How It Works](how-it-works.md) — the full pipeline, step by step
- [Parameter Reference](parameters.md) — every setting explained
- [Developer Guide](developer/index.md) — architecture, bridge protocol,
  and the Resolve API quirks this tool works around
