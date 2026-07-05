#!/usr/bin/env python
"""
AutoCut (STUDIO variant) — silence cutter running INSIDE DaVinci Resolve.

Workspace → Scripts → Utility → AutoCut Studio
(installed by `uv run python install.py`, which bakes the repo path below).

Requires Resolve STUDIO: the free version does not run this correctly
(verified on the free Mac App Store build). Free-version users: run the
external GUI instead — `uv run python main.py` — which talks to Resolve
through the AutoCut Bridge script.

Reads the CURRENT timeline, detects silence in each clip's source media
(librosa, via this repo's venv — Resolve's Python needs only the stdlib),
and builds a new timeline next to it. Audible parts get a highlight color;
silent parts stay in place for review — or vanish with auto-delete.
Modeled on YourAverageMo/auto-silence-cut.
"""

import json
import subprocess
import time
import traceback
from pathlib import Path

REPO = Path(r"__REPO__")                    # baked in by install.py
VENV_PY = REPO / ".venv" / "bin" / "python"
SETTINGS_FILE = Path.home() / ".autocut_settings.json"

CLIP_COLORS = ["Orange", "Apricot", "Yellow", "Lime", "Olive", "Green",
               "Teal", "Navy", "Blue", "Purple", "Violet", "Pink",
               "Tan", "Beige", "Brown", "Chocolate"]

DEFAULTS = {
    "threshold_db": -42.0,
    "min_silence": 0.40,
    "left_margin": 0.15,
    "right_margin": 0.15,
    "video_track": 1,
    "highlight_color": "Orange",
    "auto_delete": False,
}


def load_settings():
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        return {**DEFAULTS, **saved}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s):
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    except OSError:
        pass


def detect_silences(path, fps, s, cache={}):
    """Silence regions in source frames, via the repo's librosa detector."""
    key = (path, s["threshold_db"], s["min_silence"])
    if key in cache:
        return cache[key]
    cmd = [str(VENV_PY), "cutlist.py", path,
           "--fps", str(fps),
           "--threshold-db", str(s["threshold_db"]),
           "--min-silence", str(s["min_silence"])]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       cwd=str(REPO), timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            "Silence detection failed for %s:\n%s"
            % (path, (r.stderr or r.stdout)[-800:])
        )
    regions = [(int(a), int(b)) for a, b, _db in json.loads(r.stdout)["silences"]]
    cache[key] = regions
    return regions


def merge(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def clip_segments(item, fps, s):
    """[(src_in, src_out, audible)] for one timeline item, margins applied."""
    src_in = item.GetLeftOffset()
    src_out = src_in + item.GetDuration()
    lm = int(round(s["left_margin"] * fps))
    rm = int(round(s["right_margin"] * fps))

    path = ""
    mpi = item.GetMediaPoolItem()
    if mpi is not None:
        props = mpi.GetClipProperty()
        if isinstance(props, dict):
            path = props.get("File Path", "")
    if not path:
        return [(src_in, src_out, True)], mpi

    silences = []
    for a, b in detect_silences(path, fps, s):
        a = max(a + lm, src_in)
        b = min(b - rm, src_out)
        if b > a:
            silences.append((a, b))

    segs = []
    cursor = src_in
    for a, b in merge(silences):
        if a > cursor:
            segs.append((cursor, a, True))
        segs.append((max(a, cursor), b, False))
        cursor = max(cursor, b)
    if cursor < src_out:
        segs.append((cursor, src_out, True))
    if not any(aud for _, _, aud in segs):
        # fully silent clip: keep it whole rather than lose media
        segs = [(src_in, src_out, True)]
    return segs, mpi


def run_autocut(rv, s, status):
    pm = rv.GetProjectManager()
    project = pm.GetCurrentProject()
    if project is None:
        raise RuntimeError("No project open")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise RuntimeError("No timeline active")
    fps = float(timeline.GetSetting("timelineFrameRate") or 24)
    track = int(s["video_track"])

    items = timeline.GetItemListInTrack("video", track) or []
    if not items:
        raise RuntimeError("No clips on video track %d" % track)

    append_list = []
    flags = []           # audible flag per appended segment
    removed = kept = 0
    for i, item in enumerate(items):
        status("Analyzing clip %d/%d…" % (i + 1, len(items)))
        segs, mpi = clip_segments(item, fps, s)
        if mpi is None:
            print("AutoCut: clip %d has no media pool item — skipped" % (i + 1))
            continue
        for a, b, audible in segs:
            if audible:
                kept += b - a
            else:
                removed += b - a
                if s["auto_delete"]:
                    continue
            append_list.append(
                {"mediaPoolItem": mpi, "startFrame": int(a), "endFrame": int(b)}
            )
            flags.append(audible)

    name = "%s [AutoCut %s]" % (timeline.GetName(), time.strftime("%H.%M.%S"))
    mp = project.GetMediaPool()
    new_tl = mp.CreateEmptyTimeline(name)
    if new_tl is None:
        raise RuntimeError("CreateEmptyTimeline failed (duplicate name?)")
    project.SetCurrentTimeline(new_tl)

    status("Building timeline (%d segments)…" % len(append_list))
    appended = mp.AppendToTimeline(append_list) or []
    print("AutoCut: appended %d/%d segments" % (len(appended), len(append_list)))

    if not s["auto_delete"]:
        status("Coloring audible segments…")
        color = s["highlight_color"]
        for track_type in ("video", "audio"):
            titems = new_tl.GetItemListInTrack(track_type, 1) or []
            for idx, titem in enumerate(titems):
                if idx < len(flags) and flags[idx]:
                    try:
                        titem.SetClipColor(color)
                    except Exception:
                        pass

    summary = "%s — silence %.1fs / kept %.1fs" % (name, removed / fps, kept / fps)
    if s["auto_delete"]:
        summary += " (silence deleted)"
    else:
        summary += (". Silence is uncolored: right-click a colored clip → "
                    "Select Clips With Color → Default Color → delete.")
    print("AutoCut:", summary)
    status(summary)


def main():
    rv = None
    try:
        rv = resolve  # noqa: F821 — injected by Resolve
    except NameError:
        pass
    if rv is None:
        print("AutoCut: run this from Workspace → Scripts inside DaVinci Resolve.")
        return
    if not VENV_PY.exists():
        print("AutoCut: repo venv missing at %s — run `uv sync` in %s"
              % (VENV_PY, REPO))
        return

    s = load_settings()
    ui = fusion.UIManager           # noqa: F821 — injected by Resolve
    disp = bmd.UIDispatcher(ui)     # noqa: F821

    def row(label, item):
        return ui.HGroup([ui.Label({"Text": label, "Weight": 0.55}), item])

    win = disp.AddWindow(
        {"ID": "AutoCutWin", "WindowTitle": "AutoCut — Silence Cutter",
         "Geometry": [300, 300, 430, 340]},
        ui.VGroup({"Spacing": 6}, [
            row("Silence threshold (dB)",
                ui.LineEdit({"ID": "Threshold", "Text": str(s["threshold_db"])})),
            row("Min silence (seconds)",
                ui.LineEdit({"ID": "MinSil", "Text": str(s["min_silence"])})),
            row("Left margin (seconds)",
                ui.LineEdit({"ID": "LMargin", "Text": str(s["left_margin"])})),
            row("Right margin (seconds)",
                ui.LineEdit({"ID": "RMargin", "Text": str(s["right_margin"])})),
            row("Video track",
                ui.LineEdit({"ID": "Track", "Text": str(s["video_track"])})),
            row("Highlight color", ui.ComboBox({"ID": "Color"})),
            ui.CheckBox({"ID": "AutoDelete",
                         "Text": "Auto-delete silence (skip review)",
                         "Checked": bool(s["auto_delete"])}),
            ui.Button({"ID": "Start", "Text": "START"}),
            ui.Label({"ID": "Status", "Text": "Reads the CURRENT timeline. "
                      "Builds a new one — the original is never touched.",
                      "WordWrap": True}),
        ]),
    )
    itm = win.GetItems()
    for c in CLIP_COLORS:
        itm["Color"].AddItem(c)
    if s["highlight_color"] in CLIP_COLORS:
        itm["Color"].CurrentIndex = CLIP_COLORS.index(s["highlight_color"])

    def status(text):
        itm["Status"].Text = text

    def on_start(ev):
        try:
            s.update(
                threshold_db=float(itm["Threshold"].Text),
                min_silence=float(itm["MinSil"].Text),
                left_margin=float(itm["LMargin"].Text),
                right_margin=float(itm["RMargin"].Text),
                video_track=int(itm["Track"].Text),
                highlight_color=itm["Color"].CurrentText,
                auto_delete=bool(itm["AutoDelete"].Checked),
            )
        except ValueError as exc:
            status("Bad value: %s" % exc)
            return
        save_settings(s)
        try:
            run_autocut(rv, s, status)
        except Exception as exc:
            status("ERROR: %s" % exc)
            traceback.print_exc()

    def on_close(ev):
        disp.ExitLoop()

    win.On.Start.Clicked = on_start
    win.On.AutoCutWin.Close = on_close
    win.Show()
    disp.RunLoop()
    win.Hide()


main()
