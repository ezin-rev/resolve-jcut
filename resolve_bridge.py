"""
Connection layer for DaVinci Resolve — supports both Studio and FREE version.

Studio:  external scripting via DaVinciResolveScript (direct API).
Free:    external scripting is disabled by Blackmagic. We fall back to a
         file-based bridge: a helper script (bridge_script.py) is auto-installed
         into Resolve's Scripts menu; the user runs it once from
         Workspace → Scripts → AutoCut Bridge, and it executes our commands
         from inside Resolve where scripting IS allowed.

connect() tries the direct API first, then the file bridge. Both return
session objects with the same interface, so the rest of the code doesn't care.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

log = logging.getLogger("autocut.bridge")

# Mac App Store version ("Lite") runs sandboxed: its HOME is the container Data
# dir, so both the scripts folder and the bridge exchange dir must live there.
_MAS_CONTAINER = (
    Path.home() / "Library/Containers/com.blackmagic-design.DaVinciResolveLite/Data"
)


def _resolve_home() -> Path:
    """HOME as seen by the Resolve process (container Data dir if sandboxed)."""
    if sys.platform == "darwin" and _MAS_CONTAINER.exists():
        return _MAS_CONTAINER
    return Path.home()


# AUTOCUT_BRIDGE_DIR override exists so tests can run an isolated fake bridge
# without racing a real one inside Resolve.
BRIDGE_DIR = Path(os.environ.get("AUTOCUT_BRIDGE_DIR") or _resolve_home() / ".autocut_bridge")
REQ_FILE = BRIDGE_DIR / "request.json"
RES_FILE = BRIDGE_DIR / "response.json"

def _scripts_dir_darwin() -> Path:
    # The MAS sandboxed version scans <container>/Library/Application Support/
    # Fusion/Scripts (no "Blackmagic Design/DaVinci Resolve" prefix) — it
    # creates the Utility/Comp/Edit folder tree there on first launch.
    mas_scripts = _resolve_home() / "Library/Application Support/Fusion/Scripts"
    if mas_scripts.is_dir():
        return mas_scripts / "Utility"
    return (Path.home() / "Library/Application Support/Blackmagic Design"
                          "/DaVinci Resolve/Fusion/Scripts/Utility")


# Where Resolve looks for user utility scripts (Workspace → Scripts)
_RESOLVE_SCRIPTS_DIRS = {
    "darwin": _scripts_dir_darwin() if sys.platform == "darwin" else None,
    "linux": Path.home() / ".local/share/DaVinciResolve/Fusion/Scripts/Utility",
    "win32": Path.home() / "AppData/Roaming/Blackmagic Design"
                           "/DaVinci Resolve/Support/Fusion/Scripts/Utility",
}

# Ordered: newer Resolve versions first
_SCRIPTING_MODULE_PATHS = {
    "darwin": [
        Path("/Applications/DaVinci Resolve.app/Contents/Resources/Developer/Scripting/Modules"),
        Path("/Applications/DaVinci Resolve Studio.app/Contents/Resources/Developer/Scripting/Modules"),
        Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"),
    ],
    "linux": [
        Path("/opt/resolve/libs/Fusion/Modules"),
    ],
    "win32": [
        Path(r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"),
    ],
}


_LAUNCH_AGENT = Path.home() / "Library/LaunchAgents/com.autocut.launcher.plist"


def install_launch_agent() -> None:
    """launchd agent that starts the AutoCut app when the bridge (sandboxed,
    cannot use /usr/bin/open) touches the launch_gui trigger file."""
    repo = Path(__file__).resolve().parent
    trigger = _resolve_home() / ".autocut_bridge" / "launch_gui"
    log_path = Path.home() / ".autocut" / "gui.log"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.autocut.launcher</string>
    <key>ProgramArguments</key>
    <array><string>/bin/zsh</string><string>{repo / 'AutoCut.command'}</string></array>
    <key>WatchPaths</key>
    <array><string>{trigger}</string></array>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>{log_path}</string>
    <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
"""
    if _LAUNCH_AGENT.exists() and _LAUNCH_AGENT.read_text() == plist:
        return
    trigger.unlink(missing_ok=True)   # avoid a spurious launch on load
    log_path.parent.mkdir(exist_ok=True)
    _LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    _LAUNCH_AGENT.write_text(plist)
    subprocess.run(["launchctl", "unload", str(_LAUNCH_AGENT)],
                   capture_output=True)
    subprocess.run(["launchctl", "load", str(_LAUNCH_AGENT)],
                   capture_output=True)
    log.debug("launch agent installed: %s", _LAUNCH_AGENT)


def install_bridge_script() -> Path:
    """Install bridge_script.py into Resolve's Scripts menu. Returns path."""
    scripts_dir = _RESOLVE_SCRIPTS_DIRS.get(sys.platform)
    if scripts_dir is None:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")
    scripts_dir.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).parent / "bridge_script.py"
    dst = scripts_dir / "AutoCut Bridge.py"
    shutil.copyfile(src, dst)
    if sys.platform == "darwin":
        try:
            install_launch_agent()
        except Exception as exc:
            log.debug("launch agent install failed: %s", exc)
    return dst


# ── Direct API session (Studio only) ─────────────────────────────────────────

class ResolveSession:
    """Direct connection via DaVinciResolveScript (Resolve Studio)."""

    def __init__(self, resolve: Any) -> None:
        self._resolve = resolve
        pm = resolve.GetProjectManager()
        self._project = pm.GetCurrentProject()
        if self._project is None:
            raise RuntimeError("No project is currently open in DaVinci Resolve.")
        self._timeline = self._project.GetCurrentTimeline()
        if self._timeline is None:
            raise RuntimeError("No timeline is active in the current project.")

    @property
    def timeline(self) -> Any:
        return self._timeline

    def refresh_timeline(self) -> None:
        self._timeline = self._project.GetCurrentTimeline()
        if self._timeline is None:
            raise RuntimeError("Active timeline was closed.")

    def timeline_name(self) -> str:
        return self._timeline.GetName()

    def fps(self) -> float:
        try:
            return float(self._timeline.GetSetting("timelineFrameRate"))
        except (TypeError, ValueError):
            return 24.0

    def resolution(self) -> tuple:
        try:
            return (
                int(self._timeline.GetSetting("timelineResolutionWidth")),
                int(self._timeline.GetSetting("timelineResolutionHeight")),
            )
        except (TypeError, ValueError):
            return (1920, 1080)

    def clips_on_track(self, track_type: str, index: int) -> list:
        items = self._timeline.GetItemsInTrack(track_type, index)
        return list(items.values()) if items else []

    def set_marker(self, frame: int, color: str, name: str, note: str = "") -> bool:
        # AddMarker expects a frame relative to timeline start
        rel = frame - self.timeline_start_frame()
        return self._timeline.AddMarker(rel, color, name, note, 1, "")

    def timeline_start_frame(self) -> int:
        try:
            return int(self._timeline.GetStartFrame())
        except Exception:
            pass
        fps = self.fps()
        parts = self._timeline.GetStartTimecode().replace(";", ":").split(":")
        h, m, s, f = (int(p) for p in parts)
        return int((h * 3600 + m * 60 + s) * fps + f)

    def begin_edit(self, video_track: int) -> int:
        """Cache source clips' media pool items before switching timelines."""
        items = self.clips_on_track("video", video_track)
        self._source_mpis = [it.GetMediaPoolItem() for it in items]
        return len(items)

    def create_timeline(self, name: str) -> None:
        mp = self._project.GetMediaPool()
        new_tl = mp.CreateEmptyTimeline(name)
        if new_tl is None:
            raise RuntimeError("CreateEmptyTimeline failed (duplicate name?)")
        self._project.SetCurrentTimeline(new_tl)
        self._timeline = new_tl

    def append_segments(self, segments: List[dict]) -> int:
        """segments: [{clip_index, start, end, [mediaType, trackIndex,
        recordFrame]}] in source frames. Appends to current timeline."""
        mpis = getattr(self, "_source_mpis", [])
        clip_infos = []
        for seg in segments:
            mpi = mpis[seg["clip_index"]] if seg["clip_index"] < len(mpis) else None
            if mpi is None:
                continue
            info = {
                "mediaPoolItem": mpi,
                "startFrame": int(seg["start"]),
                "endFrame": int(seg["end"]),
            }
            for key in ("mediaType", "trackIndex", "recordFrame"):
                if key in seg:
                    info[key] = int(seg[key])
            clip_infos.append(info)
        if not clip_infos:
            return 0
        appended = self._project.GetMediaPool().AppendToTimeline(clip_infos)
        return len(appended or [])

    def set_timeline(self, name: str) -> None:
        pm = self._resolve.GetProjectManager()
        proj = pm.GetCurrentProject()
        for i in range(1, int(proj.GetTimelineCount()) + 1):
            cand = proj.GetTimelineByIndex(i)
            if cand and cand.GetName() == name:
                proj.SetCurrentTimeline(cand)
                self._timeline = cand
                return
        raise RuntimeError(f"No timeline named {name!r}")

    def import_srt(self, path: str) -> bool:
        mp = self._project.GetMediaPool()
        items = mp.ImportMedia([path]) or []
        if not items:
            return False
        if int(self._timeline.GetTrackCount("subtitle") or 0) == 0:
            self._timeline.AddTrack("subtitle")
        mp.AppendToTimeline(items)
        count = 0
        for i in range(1, int(self._timeline.GetTrackCount("subtitle") or 0) + 1):
            count += len(self._timeline.GetItemsInTrack("subtitle", i) or {})
        return count > 0

    def relink(self, paths: dict) -> int:
        count = 0
        for track_type in ("video", "audio"):
            i = 1
            while True:
                items = self._timeline.GetItemsInTrack(track_type, i)
                if not items:
                    break
                for item in items.values():
                    mpi = item.GetMediaPoolItem()
                    if mpi is None:
                        continue
                    props = mpi.GetClipProperty()
                    if isinstance(props, dict) and not props.get("File Path"):
                        target = paths.get(mpi.GetName())
                        if target and mpi.ReplaceClip(target):
                            count += 1
                i += 1
        return count

    def import_timeline_file(self, path: str) -> str:
        mp = self._project.GetMediaPool()
        # importSourceClips=False links to media already in the pool; the
        # default (re-import sources) fails outright on the free MAS build.
        # The XML must sit next to its media for the relink to find it.
        new_tl = mp.ImportTimelineFromFile(path, {"importSourceClips": False})
        if new_tl is None:
            raise RuntimeError(f"ImportTimelineFromFile failed for {path}")
        self._project.SetCurrentTimeline(new_tl)
        self._timeline = new_tl
        return new_tl.GetName()

    def alternate_zoom(self, video_track: int, zoom: float) -> int:
        self.refresh_timeline()
        items = self.clips_on_track("video", video_track)
        count = 0
        for i, item in enumerate(items):
            z = zoom if i % 2 == 1 else 1.0
            try:
                if item.SetProperty("ZoomX", z) and item.SetProperty("ZoomY", z):
                    count += 1
            except Exception:
                pass
        return count

    def import_media(self, paths: List[str]) -> int:
        imported = self._project.GetMediaPool().ImportMedia(paths)
        return len(imported or [])


# ── File-bridge session (works with FREE version) ────────────────────────────

class _PoolShim:
    def __init__(self, path: str) -> None:
        self._path = path

    def GetClipProperty(self) -> dict:
        return {"File Path": self._path}


class _ClipShim:
    """Mimics the TimelineItem methods timeline_reader uses."""

    def __init__(self, d: dict) -> None:
        self._d = d

    def GetName(self): return self._d["name"]
    def GetStart(self): return self._d["start"]
    def GetEnd(self): return self._d["end"]
    def GetDuration(self): return self._d["duration"]
    def GetLeftOffset(self): return self._d["left_offset"]
    def GetRightOffset(self): return self._d["right_offset"]
    def GetMediaPoolItem(self): return _PoolShim(self._d.get("source_path", ""))


class _TimelineShim:
    def __init__(self, name: str) -> None:
        self._name = name

    def GetName(self) -> str:
        return self._name


class BridgeSession:
    """Talks to bridge_script.py running inside Resolve via files in ~/.autocut_bridge."""

    def __init__(self) -> None:
        info = self._request("ping", timeout=4.0)
        self._info = info

    @property
    def timeline(self) -> _TimelineShim:
        return _TimelineShim(self._info["timeline_name"])

    def refresh_timeline(self) -> None:
        self._info = self._request("ping", timeout=4.0)

    def timeline_name(self) -> str:
        return self._info["timeline_name"]

    def fps(self) -> float:
        return float(self._info["fps"])

    def timeline_start_frame(self) -> int:
        return int(self._info["start_frame"])

    def resolution(self) -> tuple:
        return (
            int(self._info.get("width", 1920)),
            int(self._info.get("height", 1080)),
        )

    def clips_on_track(self, track_type: str, index: int) -> list:
        result = self._request(
            "clips", {"track_type": track_type, "index": index}, timeout=15.0
        )
        return [_ClipShim(d) for d in result["clips"]]

    def set_marker(self, frame: int, color: str, name: str, note: str = "") -> bool:
        result = self._request(
            "marker",
            {"frame": frame, "color": color, "name": name, "note": note},
            timeout=10.0,
        )
        return bool(result.get("ok"))

    def begin_edit(self, video_track: int) -> int:
        result = self._request("begin_edit", {"video_track": video_track}, timeout=15.0)
        return int(result.get("clips", 0))

    def create_timeline(self, name: str) -> None:
        self._request("create_timeline", {"name": name}, timeout=30.0)

    def append_segments(self, segments: List[dict]) -> int:
        result = self._request(
            "append_segments", {"segments": segments}, timeout=60.0
        )
        return int(result.get("appended", 0))

    def import_timeline_file(self, path: str) -> str:
        result = self._request(
            "import_timeline",
            {"path": path, "options": {"importSourceClips": False}},
            timeout=120.0,
        )
        return str(result.get("timeline_name", ""))

    def relink(self, paths: dict) -> int:
        result = self._request("relink", {"paths": paths}, timeout=60.0)
        return int(result.get("relinked", 0))

    def set_timeline(self, name: str) -> None:
        self._request("set_timeline", {"name": name}, timeout=15.0)
        self.refresh_timeline()

    def import_srt(self, path: str) -> bool:
        result = self._request("import_srt", {"path": path}, timeout=60.0)
        return bool(result.get("ok"))

    def alternate_zoom(self, video_track: int, zoom: float) -> int:
        result = self._request(
            "alternate_zoom", {"video_track": video_track, "zoom": zoom}, timeout=60.0
        )
        return int(result.get("zoomed", 0))

    def import_media(self, paths: List[str]) -> int:
        result = self._request("import_media", {"paths": paths}, timeout=60.0)
        return int(result.get("imported", 0))

    def _request(self, action: str, params: Optional[dict] = None,
                 timeout: float = 10.0) -> dict:
        BRIDGE_DIR.mkdir(exist_ok=True)
        req_id = str(uuid.uuid4())
        started = time.time()
        log.debug("bridge → %s %.200s", action, params or {})
        REQ_FILE.write_text(json.dumps(
            {"id": req_id, "action": action, "params": params or {}}
        ))
        deadline = started + timeout
        while time.time() < deadline:
            time.sleep(0.15)
            if not RES_FILE.exists():
                continue
            try:
                res = json.loads(RES_FILE.read_text())
            except (ValueError, OSError):
                continue
            if res.get("id") != req_id:
                continue
            if not res.get("ok"):
                raise RuntimeError(f"Bridge error: {res.get('error')}")
            result = res.get("result") or {}
            log.debug("bridge ← %s ok in %.2fs: %.200s",
                      action, time.time() - started, result)
            return result
        raise TimeoutError(
            "No response from the AutoCut Bridge script.\n\n"
            "In DaVinci Resolve, go to:\n"
            "    Workspace → Scripts → AutoCut Bridge\n"
            "run it, then click Connect again."
        )


# ── connect() ─────────────────────────────────────────────────────────────────

def _try_direct_api() -> Optional[ResolveSession]:
    candidates = _SCRIPTING_MODULE_PATHS.get(sys.platform, [])
    for module_dir in candidates:
        if module_dir.exists():
            p = str(module_dir)
            if p not in sys.path:
                sys.path.insert(0, p)
            break
    else:
        return None
    try:
        import DaVinciResolveScript as dvr  # type: ignore[import]
        resolve = dvr.scriptapp("Resolve")
        if resolve is None:
            return None
        return ResolveSession(resolve)
    except Exception:
        return None


# scriptapp() can block indefinitely while Resolve is starting up or quitting;
# once it hangs, never probe again in this process (the free version's answer
# won't change) — the stuck daemon thread dies with the app.
_DIRECT_API_HUNG = False


def _probe_direct_api(timeout: float = 2.0) -> Optional[ResolveSession]:
    global _DIRECT_API_HUNG
    if _DIRECT_API_HUNG:
        return None
    box = {}
    t = threading.Thread(target=lambda: box.update(s=_try_direct_api()), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.debug("direct API probe hung >%.0fs — assuming free version", timeout)
        _DIRECT_API_HUNG = True
        return None
    return box.get("s")


def connect():
    """Returns ResolveSession (Studio) or BridgeSession (free version)."""
    session = _probe_direct_api()
    if session is not None:
        return session
    log.debug("direct API unavailable, falling back to file bridge at %s", BRIDGE_DIR)

    # Free version path: make sure the bridge script is installed, then ping it
    install_bridge_script()
    try:
        return BridgeSession()
    except TimeoutError as exc:
        raise RuntimeError(
            "Direct connection failed (normal on the FREE version of Resolve — "
            "external scripting is Studio-only).\n\n"
            "The AutoCut Bridge script has been installed into Resolve for you.\n"
            "In DaVinci Resolve:\n"
            "    Workspace → Scripts → AutoCut Bridge\n"
            "Run it (a console window appears), then click Connect again."
        ) from exc
    except RuntimeError as exc:
        raise RuntimeError(
            "The AutoCut Bridge is running but Resolve reported an error.\n"
            "Make sure a project and timeline are open, then re-run\n"
            "    Workspace → Scripts → Utility → AutoCut Bridge\n"
            "and click Connect again.\n\n"
            f"Details: {exc}"
        ) from exc
