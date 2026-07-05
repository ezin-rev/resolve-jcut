#!/usr/bin/env python
"""
AutoCut Bridge — runs INSIDE DaVinci Resolve (free version compatible).

The free version of Resolve does not allow external scripting, but it does
allow scripts launched from its own menu. This script is auto-installed to:

    Workspace → Scripts → AutoCut Bridge

Run it there once, then click "Connect" in the AutoCut GUI. It polls
~/.autocut_bridge/request.json for commands, executes them with the `resolve`
object Resolve injects, and writes responses. Stdlib only — Resolve runs
this with whatever system Python it finds.
"""

import json
import os
import time
import traceback
import uuid
from pathlib import Path

BRIDGE_DIR = Path.home() / ".autocut_bridge"
REQ_FILE = BRIDGE_DIR / "request.json"
RES_FILE = BRIDGE_DIR / "response.json"
OWNER_FILE = BRIDGE_DIR / "owner"
MAX_RUNTIME_SEC = 8 * 3600
POLL_SEC = 0.25

# Session state for streaming edits (survives across requests)
STATE = {}


def _get_resolve():
    # The object must actually work, not just exist — a half-initialised one
    # returns None from GetProjectManager() and poisons every request.
    candidates = []
    try:
        candidates.append(resolve)  # noqa: F821 — injected by Resolve's Scripts menu
    except NameError:
        pass
    try:
        import DaVinciResolveScript as dvr
        candidates.append(dvr.scriptapp("Resolve"))
    except Exception:
        pass
    for rv in candidates:
        try:
            if rv is not None and rv.GetProjectManager() is not None:
                return rv
        except Exception:
            continue
    return None


class ResolveGone(RuntimeError):
    """Resolve quit under us — this orphaned process must exit, not squat."""


def _timeline(rv):
    try:
        pm = rv.GetProjectManager()
    except Exception:
        pm = None
    if pm is None:
        raise ResolveGone(
            "Resolve's scripting API stopped responding — in Resolve, run "
            "Workspace -> Scripts -> Utility -> AutoCut Bridge again."
        )
    proj = pm.GetCurrentProject()
    if proj is None:
        raise RuntimeError("No project open")
    tl = proj.GetCurrentTimeline()
    if tl is None:
        raise RuntimeError("No timeline active")
    return proj, tl


def _start_frame(tl):
    try:
        return int(tl.GetStartFrame())
    except Exception:
        pass
    fps = float(tl.GetSetting("timelineFrameRate") or 24)
    parts = tl.GetStartTimecode().replace(";", ":").split(":")
    h, m, s, f = (int(p) for p in parts)
    return int((h * 3600 + m * 60 + s) * fps + f)


def _clip_dicts(tl, track_type, index):
    items = tl.GetItemsInTrack(track_type, index) or {}
    out = []
    for item in items.values():
        path = ""
        try:
            mpi = item.GetMediaPoolItem()
            if mpi is not None:
                props = mpi.GetClipProperty()
                if isinstance(props, dict):
                    path = props.get("File Path", "")
        except Exception:
            pass
        out.append({
            "name": item.GetName(),
            "start": item.GetStart(),
            "end": item.GetEnd(),
            "duration": item.GetDuration(),
            "left_offset": item.GetLeftOffset(),
            "right_offset": item.GetRightOffset(),
            "source_path": path,
        })
    return out


def _handle(rv, action, params):
    # stop must work even when the timeline (or rv itself) is broken
    if action == "stop":
        return {"ok": True, "stopping": True}

    proj, tl = _timeline(rv)

    if action == "ping":
        return {
            "timeline_name": tl.GetName(),
            "fps": float(tl.GetSetting("timelineFrameRate") or 24),
            "start_frame": _start_frame(tl),
            "width": int(tl.GetSetting("timelineResolutionWidth") or 1920),
            "height": int(tl.GetSetting("timelineResolutionHeight") or 1080),
        }

    if action == "clips":
        return {"clips": _clip_dicts(tl, params["track_type"], params["index"])}

    if action == "marker":
        rel = int(params["frame"]) - _start_frame(tl)
        ok = tl.AddMarker(rel, params["color"], params["name"],
                          params.get("note", ""), 1, "")
        return {"ok": bool(ok)}

    # Streaming edit: begin_edit caches the source clips, create_timeline
    # switches to a fresh timeline, append_segments lands cuts live.
    if action == "begin_edit":
        items = list((tl.GetItemsInTrack("video", params["video_track"]) or {}).values())
        STATE["source_mpis"] = [it.GetMediaPoolItem() for it in items]
        return {"ok": True, "clips": len(items)}

    if action == "create_timeline":
        mp = proj.GetMediaPool()
        new_tl = mp.CreateEmptyTimeline(params["name"])
        if new_tl is None:
            raise RuntimeError("CreateEmptyTimeline failed (duplicate name?)")
        proj.SetCurrentTimeline(new_tl)
        return {"ok": True}

    if action == "append_segments":
        mpis = STATE.get("source_mpis") or []
        mp = proj.GetMediaPool()
        clip_infos = []
        for seg in params["segments"]:
            mpi = mpis[seg["clip_index"]] if seg["clip_index"] < len(mpis) else None
            if mpi is None:
                continue
            info = {
                "mediaPoolItem": mpi,
                "startFrame": int(seg["start"]),
                "endFrame": int(seg["end"]),
            }
            # Optional placement keys (Resolve 18.5+): 1=video-only,
            # 2=audio-only; recordFrame positions the clip explicitly.
            for key in ("mediaType", "trackIndex", "recordFrame"):
                if key in seg:
                    info[key] = int(seg[key])
            clip_infos.append(info)
        if not clip_infos:
            return {"ok": True, "appended": 0}
        appended = mp.AppendToTimeline(clip_infos)
        return {"ok": True, "appended": len(appended or [])}

    if action == "alternate_zoom":
        items = list((tl.GetItemsInTrack("video", params["video_track"]) or {}).values())
        zoom = float(params["zoom"])
        count = 0
        for i, item in enumerate(items):
            z = zoom if i % 2 == 1 else 1.0
            try:
                ok1 = item.SetProperty("ZoomX", z)
                ok2 = item.SetProperty("ZoomY", z)
                if ok1 and ok2:
                    count += 1
            except Exception:
                pass
        return {"ok": True, "zoomed": count}

    if action == "import_media":
        mp = proj.GetMediaPool()
        imported = mp.ImportMedia(params["paths"])
        return {"ok": True, "imported": len(imported or [])}

    if action == "import_srt":
        # Land an SRT on the timeline's subtitle track. Free version has no
        # documented call for this — try the plausible routes and report.
        mp = proj.GetMediaPool()
        items = mp.ImportMedia([params["path"]]) or []
        detail = {"imported": len(items)}
        try:
            if int(tl.GetTrackCount("subtitle") or 0) == 0:
                detail["track_added"] = bool(tl.AddTrack("subtitle"))
        except Exception as exc:
            detail["track_error"] = str(exc)
        appended = []
        if items:
            try:
                appended = mp.AppendToTimeline(items) or []
            except Exception as exc:
                detail["append_error"] = str(exc)
            if not appended:
                try:
                    appended = mp.AppendToTimeline(
                        [{"mediaPoolItem": items[0],
                          "recordFrame": _start_frame(tl)}]) or []
                    detail["via"] = "clipinfo"
                except Exception as exc:
                    detail["append_error2"] = str(exc)
        detail["appended"] = len(appended)
        subs = []
        try:
            for i in range(1, int(tl.GetTrackCount("subtitle") or 0) + 1):
                subs.extend((tl.GetItemsInTrack("subtitle", i) or {}).values())
        except Exception:
            pass
        detail["subtitle_items"] = len(subs)
        detail["ok"] = len(subs) > 0
        return detail

    if action == "methods":
        mp = proj.GetMediaPool()
        objs = {"timeline": tl, "project": proj, "mediapool": mp}
        return {k: sorted(m for m in dir(v) if not m.startswith("_"))
                for k, v in objs.items()}

    if action == "set_timeline":
        want = params["name"]
        for i in range(1, int(proj.GetTimelineCount()) + 1):
            cand = proj.GetTimelineByIndex(i)
            if cand and cand.GetName() == want:
                proj.SetCurrentTimeline(cand)
                return {"ok": True, "timeline_name": want}
        raise RuntimeError("No timeline named %r" % want)

    if action == "relink":
        # Fix offline media on the current timeline: point each item's pool
        # entry back at a real file, matched by clip name.
        paths = params.get("paths") or {}
        fixed = 0
        missing = 0
        seen = set()
        for track_type in ("video", "audio"):
            i = 1
            while True:
                items = tl.GetItemsInTrack(track_type, i) or {}
                if not items:
                    break
                for item in items.values():
                    try:
                        mpi = item.GetMediaPoolItem()
                    except Exception:
                        mpi = None
                    if mpi is None:
                        missing += 1
                        continue
                    mid = mpi.GetMediaId() if hasattr(mpi, "GetMediaId") else id(mpi)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    props = mpi.GetClipProperty()
                    current = props.get("File Path", "") if isinstance(props, dict) else ""
                    if current:
                        continue
                    name = mpi.GetName()
                    target = paths.get(name) or paths.get(name.rsplit(".", 1)[0])
                    if target and mpi.ReplaceClip(target):
                        fixed += 1
                i += 1
        return {"ok": True, "relinked": fixed, "no_mpi": missing}

    if action == "import_timeline":
        mp = proj.GetMediaPool()
        new_tl = mp.ImportTimelineFromFile(params["path"], params.get("options") or {})
        if new_tl is None:
            raise RuntimeError("ImportTimelineFromFile failed for %s" % params["path"])
        proj.SetCurrentTimeline(new_tl)
        return {"ok": True, "timeline_name": new_tl.GetName()}

    raise RuntimeError("Unknown action: %s" % action)


def _launch_gui():
    """One-click UX: touching this trigger file makes launchd (outside the
    sandbox — /usr/bin/open is refused from in here) start the AutoCut app.
    The LaunchAgent watching it is installed by the app on every connect."""
    try:
        pid = int((BRIDGE_DIR / "gui.pid").read_text())
        os.kill(pid, 0)
        return                       # app already running
    except (OSError, ValueError):
        pass
    try:
        (BRIDGE_DIR / "launch_gui").write_text(str(time.time()))
        print("AutoCut Bridge: asked launchd to open the AutoCut app…")
    except OSError as exc:
        print("AutoCut Bridge: could not signal app launch (%s) — "
              "double-click AutoCut.command in the repo folder." % exc)


def main():
    rv = _get_resolve()
    if rv is None:
        print("AutoCut Bridge: Resolve's scripting API is not responding. "
              "Run this from Workspace -> Scripts inside DaVinci Resolve, "
              "with a project open.")
        return

    BRIDGE_DIR.mkdir(exist_ok=True)
    # Clear stale files from a previous session
    for f in (REQ_FILE, RES_FILE):
        try:
            f.unlink()
        except OSError:
            pass

    # Newest instance wins: claim the bridge so any older copy of this
    # script still polling exits instead of racing us for requests.
    my_id = str(uuid.uuid4())
    OWNER_FILE.write_text(my_id)

    _launch_gui()

    print("AutoCut Bridge running. Leave this alone and use the AutoCut GUI. "
          "(Stops automatically when the GUI sends 'stop'.)")

    last_id = None
    deadline = time.time() + MAX_RUNTIME_SEC
    while time.time() < deadline:
        time.sleep(POLL_SEC)
        try:
            if OWNER_FILE.read_text() != my_id:
                print("AutoCut Bridge: a newer instance took over, exiting.")
                return
        except OSError:
            pass
        if not REQ_FILE.exists():
            continue
        try:
            req = json.loads(REQ_FILE.read_text())
        except (ValueError, OSError):
            continue
        req_id = req.get("id")
        if req_id is None or req_id == last_id:
            continue
        last_id = req_id

        resolve_gone = False
        try:
            result = _handle(rv, req.get("action"), req.get("params") or {})
            payload = {"id": req_id, "ok": True, "result": result}
        except ResolveGone as exc:
            payload = {"id": req_id, "ok": False, "error": str(exc)}
            resolve_gone = True
        except Exception as exc:
            payload = {"id": req_id, "ok": False,
                       "error": "%s\n%s" % (exc, traceback.format_exc(limit=3))}

        tmp = RES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.rename(RES_FILE)

        if resolve_gone:
            print("AutoCut Bridge: Resolve is gone, exiting.")
            return

        if payload.get("ok") and isinstance(payload.get("result"), dict) \
                and payload["result"].get("stopping"):
            break

    print("AutoCut Bridge stopped.")


main()
