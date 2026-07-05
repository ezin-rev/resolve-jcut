# Bridge Protocol

How the desktop app talks to `bridge_script.py` running inside the free
version of Resolve. It is deliberately primitive: two JSON files and a poll
loop. No sockets (the sandboxed MAS build makes networking a permissions
question), no dependencies, nothing to install inside Resolve beyond one
stdlib script.

## Files

All under the **bridge directory**: `<Resolve HOME>/.autocut_bridge`, which
on a sandboxed MAS install is inside the app container (see
[Architecture](index.md#the-sandboxed-mac-app-store-build)). Tests override
it with the `AUTOCUT_BRIDGE_DIR` environment variable.

| File | Written by | Purpose |
|---|---|---|
| `request.json` | app | one pending request |
| `response.json` | bridge | response to the matching request |
| `owner` | bridge | instance takeover token |
| `gui.pid` | app | app liveness (bridge skips auto-launch if alive) |
| `launch_gui` | bridge | trigger file watched by the macOS LaunchAgent |

## Wire format

Request:

```json
{"id": "<uuid4>", "action": "clips", "params": {"track_type": "video", "index": 1}}
```

Response (written atomically: `.tmp` then rename):

```json
{"id": "<same uuid>", "ok": true, "result": {"clips": [...]}}
{"id": "<same uuid>", "ok": false, "error": "message + short traceback"}
```

Mechanics:

- The bridge polls `request.json` every **0.25 s**; the app polls
  `response.json` every **0.15 s** until its deadline (per-action timeouts
  range from 4 s for `ping` to 120 s for `import_timeline`).
- The `id` is how a response is matched to its request; the bridge remembers
  the last handled id, so a request file is executed **once** even though it
  stays on disk.
- One request in flight at a time. There is no queue — the app is the only
  client and its calls are serialized.
- `ok: false` surfaces in the app as a `RuntimeError("Bridge error: …")`;
  no response before the deadline raises `TimeoutError` with instructions to
  re-run the bridge script.

## Lifecycle

- **Newest instance wins.** On start, the bridge writes a fresh UUID to
  `owner`; any older instance still polling sees the change and exits.
  This prevents two `fuscript` processes racing for requests.
- **Self-exit when Resolve dies.** If `GetProjectManager()` stops responding
  the bridge answers the pending request with an error and exits
  (`ResolveGone`) instead of squatting — Resolve runs menu scripts as
  separate processes that outlive it.
- Hard runtime cap of 8 h; the GUI's `stop` action ends it cleanly.

## Actions

`ping`
:   → `timeline_name`, `fps`, `start_frame`, `width`, `height`. Also the
    connection health check; `BridgeSession.refresh_timeline()` is a re-ping.

`clips` — `{track_type, index}`
:   → `{clips: [{name, start, end, duration, left_offset, right_offset,
    source_path}]}`. Timeline items with the media-pool file path resolved.

`begin_edit` — `{video_track}`
:   Caches the track's `MediaPoolItem`s in bridge memory (they survive across
    requests in `STATE`). Must be called **before** `create_timeline`
    switches away from the source timeline. → `{clips: n}`

`create_timeline` — `{name}`
:   `CreateEmptyTimeline` + make it current. Fails on duplicate names.

`append_segments` — `{segments: [{clip_index, start, end, [mediaType, trackIndex, recordFrame]}]}`
:   `AppendToTimeline` against the cached media-pool items. Frames are
    **source frames**. `mediaType` 1 = video-only, 2 = audio-only;
    `recordFrame` places the clip explicitly. → `{appended: n}`

`marker` — `{frame, color, name, note}`
:   `AddMarker`. Takes an **absolute** timeline frame; the bridge subtracts
    the timeline start offset itself (see [Frame Spaces](frame-spaces.md)).

`alternate_zoom` — `{video_track, zoom}`
:   Sets `ZoomX`/`ZoomY` on every second clip. → `{zoomed: n}`

`import_media` — `{paths}`
:   Media-pool import. → `{imported: n}`

`import_srt` — `{path}`
:   Best-effort SRT placement: import, `AddTrack("subtitle")` if none,
    `AppendToTimeline`. Returns diagnostic detail plus `ok` = whether any
    subtitle items actually landed; the app falls back to plain media-pool
    import when `ok` is false.

`import_timeline` — `{path, options}`
:   `ImportTimelineFromFile` (FCPXML etc.). → `{timeline_name}`

`set_timeline` — `{name}`
:   Switch the current timeline by name.

`relink` — `{paths: {clip_name: file_path}}`
:   Point offline media-pool entries back at real files via `ReplaceClip`.
    Useless for FCPXML orphans, which have no pool item at all.
    → `{relinked, no_mpi}`

`methods`
:   `dir()` introspection of the live timeline/project/mediapool objects —
    API archaeology for undocumented calls. → sorted method-name lists.

`stop`
:   Ends the bridge loop. Handled before any timeline access so it works
    even when Resolve state is broken.

## Adding an action

Three places, always:

1. `bridge_script.py` `_handle()` — the implementation running inside
   Resolve. Stdlib only; JSON-serializable results only (no Resolve objects
   across the wire).
2. `BridgeSession` — a typed wrapper around `self._request("your_action", …)`
   with a realistic timeout.
3. `ResolveSession` — the same capability via the direct API, so Studio users
   get it too.

Then re-run AutoCut Bridge inside Resolve — the running `fuscript` process is
still the old code — and extend the fake responder in `tests/test_autocut.py`
if the pipeline uses the new action.

## Probing Resolve from a terminal

With the bridge running, Resolve's behavior can be probed directly — no GUI
needed:

```python
from resolve_bridge import BridgeSession
s = BridgeSession()
s._request("methods", timeout=10)
s._request("import_timeline", {"path": "/path/to.fcpxml",
                               "options": {"importSourceClips": False}})
```

This is how the FCPXML importer behavior documented in
[Architecture](index.md#resolve-api-constraints-that-shaped-the-design) was
established. `methods` plus a throwaway request is the fastest way to test
an undocumented API call before writing any code.
