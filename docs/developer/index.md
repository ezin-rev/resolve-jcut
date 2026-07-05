# Architecture

This page is for anyone extending or fixing AutoCut. It covers the module
map, the session abstraction, and the Resolve constraints that shaped the
design — most of which were established empirically and are not documented
by Blackmagic anywhere.

## Module map

| File | Responsibility |
|---|---|
| `main.py` | GUI entry point; exits early if another instance is running |
| `gui/app.py` | Tkinter app; run-state, Analyze/Build orchestration, presets UI |
| `autocut.py` | The pipeline: `analyze_autocut` → `build_segments` → `apply_autocut`; SRT generation |
| `resolve_bridge.py` | `connect()`; `ResolveSession` (direct API) and `BridgeSession` (file bridge); script/agent installers |
| `bridge_script.py` | Runs **inside** Resolve; executes bridge requests. Stdlib only |
| `audio_analyzer.py` | Audio decoding (librosa → PyAV fallback), silence detection, adaptive threshold |
| `transcribe.py` | Whisper front-end: faster-whisper (CPU) and mlx-whisper (Apple GPU) behind one `transcribe()` |
| `fcpxml_export.py` | FCPXML writer with the same J-cut lead math as stream mode |
| `timeline_reader.py` | Timeline snapshot → `ClipInfo` list |
| `presets.py` | Named parameter sets at `~/.autocut/presets.json` |
| `cutlist.py` | CLI: silence detection on one file, prints JSON (also used by the Studio script) |
| `autocut_script.py` | Separate Studio-only in-Resolve script (template; `install.py` bakes the repo path in) |

## The session abstraction

`resolve_bridge.connect()` returns one of two session objects with the same
interface:

- **`ResolveSession`** — direct `DaVinciResolveScript` API. Works on Studio,
  where external scripting is allowed.
- **`BridgeSession`** — file-based IPC with `bridge_script.py` running inside
  Resolve. The only way in on the free version.

Everything above the session layer is transport-agnostic.

!!! important "Adding a session capability means touching three places"
    `ResolveSession`, `BridgeSession`, and `bridge_script.py`'s `_handle()`.
    Miss one and the feature works on Studio but silently 404s on the free
    version (or vice versa). See [Bridge Protocol](bridge-protocol.md) for
    the wire format and action list.

`connect()` probes the direct API in a daemon thread with a 2 s timeout —
`scriptapp("Resolve")` can block *indefinitely* while Resolve starts or
quits, and once it has hung it is never probed again in that process.

## Resolve API constraints that shaped the design

- **No razor/split/trim API exists in any Resolve edition.** "Editing" means
  building a new timeline from `AppendToTimeline` calls with explicit
  `startFrame`/`endFrame`. This is the root cause of the whole two-phase
  design.
- `AppendToTimeline` honors `mediaType` (1 = video-only, 2 = audio-only) and
  `recordFrame` (explicit timeline position) — verified on Resolve 21
  free/MAS. This is what makes real J-cuts possible in stream mode.
- **FCPXML imports on the free MAS build produce permanently offline media.**
  Imported items have no `MediaPoolItem` at all, so `ReplaceClip` relinking
  cannot fix them. `importSourceClips: True` fails outright; `False` imports
  orphans; `sourceClipsPath` doesn't help. All verified live. FCPXML mode is
  therefore labeled Premiere/FCP-interchange-only.
- Subtitle import (`AddTrack("subtitle")` + `AppendToTimeline`) works on some
  builds and not others; the code treats it as best-effort with a media-pool
  fallback.
- For FCPXML consumed by other NLEs: declare each source as **two assets**
  (video-only + audio-only) — `srcEnable` is ignored — and keep the XML next
  to its media.

## The sandboxed Mac App Store build

The free Resolve from the Mac App Store
(`com.blackmagic-design.DaVinciResolveLite`) runs sandboxed, and several
things follow from that. All handled in `resolve_bridge._resolve_home()` /
`_scripts_dir_darwin()`:

- The app's `HOME` is
  `~/Library/Containers/com.blackmagic-design.DaVinciResolveLite/Data` —
  `Path.home()` inside any in-Resolve script resolves there. The bridge
  exchange directory must live under *that* home, not the user's.
- The Scripts menu scans `<container>/Library/Application
  Support/Fusion/Scripts/` — **without** the usual `Blackmagic
  Design/DaVinci Resolve` prefix. The presence of Resolve's
  Utility/Comp/Edit tree there is how the MAS layout is detected.
- Resolve's script runner only sees the **python.org framework Python**
  (`/Library/Frameworks/Python.framework`). Homebrew/CLT installs are
  invisible to it.
- Resolve runs menu scripts as separate `fuscript` processes that can
  **outlive Resolve itself**. The bridge self-exits when Resolve stops
  responding (`ResolveGone`) — earlier builds squatted on the bridge files
  and poisoned the protocol. When debugging weird bridge behavior, check
  `ps -e | grep fuscript` for stale processes.

### One-click launch (macOS)

Running AutoCut Bridge from Resolve's menu touches
`.autocut_bridge/launch_gui`. A user LaunchAgent
(`~/Library/LaunchAgents/com.autocut.launcher.plist`, installed by
`install_bridge_script()`, `WatchPaths` on the trigger) runs
`AutoCut.command` **outside** the sandbox — the sandboxed script process
cannot use `/usr/bin/open`; LaunchServices refuses silently. The GUI stamps
`gui.pid` in the bridge dir so the bridge skips launching when the app is
already running. Agent output lands in `~/.autocut/gui.log`;
`AutoCut.command` prepends the usual uv install locations to `PATH` because
launchd's environment is minimal.

## Constraints on in-Resolve code

`bridge_script.py` and `autocut_script.py` run under Resolve's Python, not
the project venv:

- **Stdlib only.** No librosa, no numpy, nothing from the venv.
- Heavy work is delegated: the Studio script shells out to
  `.venv/bin/python cutlist.py` — calling the venv interpreter directly, not
  through `uv`, because the sandboxed Resolve process has a container HOME
  and `uv run` would try to re-provision Python there.
- Both must compile standalone:
  `uv run python -m py_compile autocut_script.py bridge_script.py`.
- After editing `bridge_script.py`, re-run AutoCut Bridge inside Resolve —
  the running instance is the old code. (Reconnecting from the GUI reinstalls
  the script file, but only Resolve can restart the process.)

## GUI threading — the Tk rule

Cross-thread `tkinter.after()` fails **silently** on Tk 9 (bundled with uv's
Python 3.12) — worker-thread results never reach the window, which looks
like "connected but the GUI never shows it". Therefore every worker→UI hop
goes through `app.ui_call`, a `queue.Queue` drained on the main thread every
50 ms. **Never call any Tk method — including `after()` — from a worker
thread.** If you add a background task, marshal its results the same way.

Cancellation is a `threading.Event`; the pipeline raises
`transcribe.Cancelled` — catch it *before* `Exception` in any new handler.

## Logging

Everything logs under the `"autocut"` logger namespace with
`propagate = False`. **Never attach handlers to the root logger** —
numba/librosa emit hundreds of records at import time and will flood the
console. The in-Resolve scripts use plain `print()`, which shows in
Resolve's Console.
