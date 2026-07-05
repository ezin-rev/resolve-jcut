"""
Offline test suite for AutoCut.

1. Pure-logic units: range merging, keep-segment inversion, SRT generation, FCPXML.
2. Silence detection on synthetic audio with a known silence gap.
3. Full pipeline e2e: a fake bridge responder impersonates Resolve on the other
   side of the file protocol; run_autocut() streams through it and we assert
   the exact segments Resolve would have received.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FPS = 24.0
SCRATCH = Path(__file__).parent / "_artifacts"
SCRATCH.mkdir(exist_ok=True)

# Isolate from any real bridge running inside Resolve — must be set
# before resolve_bridge is imported anywhere.
os.environ["AUTOCUT_BRIDGE_DIR"] = str(SCRATCH / "bridge")

import numpy as np
import soundfile as sf
PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


# ── 1. Pure logic ─────────────────────────────────────────────────────────────
print("== Unit: pure logic ==")
from autocut import _merge_ranges, _keep_segments, _generate_srt, Segment
from timeline_reader import ClipInfo

check("merge overlapping ranges",
      _merge_ranges([(10, 20), (15, 30), (50, 60)]) == [(10, 30), (50, 60)])
check("merge empty", _merge_ranges([]) == [])

clip = ClipInfo(name="c", track_type="video", track_index=1, start=86400, end=86640,
                duration=240, left_offset=48, right_offset=0,
                source_path="", _item=None)
segs = _keep_segments(clip, 0, [(100, 150)])
check("keep segments respect left_offset",
      [(s.src_in, s.src_out) for s in segs] == [(48, 100), (150, 288)],
      str([(s.src_in, s.src_out) for s in segs]))

segs_all_cut = _keep_segments(clip, 0, [(48, 288)])
check("fully-silent clip is kept, not dropped",
      [(s.src_in, s.src_out) for s in segs_all_cut] == [(48, 288)])

# ── 1b. SRT: Thai-safe line breaks + cut alignment ────────────────────────────
print("== SRT Thai line breaks ==")
from transcribe import Transcript, Word as TWord

_words = [TWord(0.1, 0.3, "กกกกกกกกกกกกกกกกกกกก"),   # 20 chars
          TWord(0.3, 0.5, "ขขขขขขขขขขขขขขขขขข"),      # 18 chars -> line at 38
          TWord(0.5, 0.7, "เร"),
          TWord(0.7, 0.9, "ียกตีน"),                    # must NOT start a line
          TWord(3.2, 3.6, "สอง")]                       # inside segment 2
_tr = Transcript(words=_words)
_srt = SCRATCH / "thai.srt"
_generate_srt([Segment(0, 0, 48, timeline_offset=0),
               Segment(0, 72, 120, timeline_offset=48)], {0: _tr}, FPS, _srt)
_txt = _srt.read_text()
check("srt: no line starts with dependent vowel",
      all(not ln or ln[0] not in "ะัาำิีึืุู็่้๊๋์ๆ"
          for ln in _txt.splitlines()), _txt)
check("srt: broken syllable kept together", "เรียกตีน" in _txt, _txt)
check("srt: caption re-starts exactly at the timeline cut",
      "00:00:02,200" in _txt, _txt)

# ── 2. Silence detection on synthetic audio ───────────────────────────────────
print("== Silence detection (synthetic audio) ==")
sr = 44100
tone = lambda sec: 0.5 * np.sin(2 * np.pi * 440 * np.linspace(0, sec, int(sr * sec)))
quiet = lambda sec: np.zeros(int(sr * sec))
audio = np.concatenate([tone(3.0), quiet(2.0), tone(5.0)])   # silence at 3–5s
wav = SCRATCH / "synthetic.wav"
sf.write(wav, audio, sr)

from audio_analyzer import _detect_silence_regions
regions = _detect_silence_regions(str(wav), -42.0, 0.4, FPS)
check("exactly one silence region found", len(regions) == 1, f"got {len(regions)}")
if regions:
    s, e, db = regions[0]
    check("silence located at ~3–5s (frames 72–120)",
          abs(s - 72) <= 3 and abs(e - 120) <= 3, f"got ({s}, {e})")

# ── 3. E2E through a fake bridge ──────────────────────────────────────────────
print("== E2E: pipeline through fake bridge ==")
from resolve_bridge import BRIDGE_DIR, REQ_FILE, RES_FILE, BridgeSession

BRIDGE_DIR.mkdir(exist_ok=True)
for f in (REQ_FILE, RES_FILE):
    f.unlink(missing_ok=True)

received = {"append_calls": [], "created": None, "markers": []}
stop_flag = threading.Event()

CLIP = {
    "name": "interview.wav", "start": 86400, "end": 86400 + 240,
    "duration": 240, "left_offset": 0, "right_offset": 100,
    "source_path": str(wav),
}


def fake_resolve():
    last_id = None
    while not stop_flag.is_set():
        time.sleep(0.05)
        if not REQ_FILE.exists():
            continue
        try:
            req = json.loads(REQ_FILE.read_text())
        except (ValueError, OSError):
            continue
        if req.get("id") == last_id:
            continue
        last_id = req["id"]
        action, p = req["action"], req.get("params") or {}
        if action == "ping":
            result = {"timeline_name": "Interview", "fps": FPS,
                      "start_frame": 86400, "width": 1920, "height": 1080}
        elif action == "clips":
            result = {"clips": [CLIP]}
        elif action == "begin_edit":
            result = {"clips": 1}
        elif action == "create_timeline":
            received["created"] = p["name"]
            result = {}
        elif action == "append_segments":
            received["append_calls"].append(p["segments"])
            result = {"appended": len(p["segments"])}
        elif action == "alternate_zoom":
            result = {"zoomed": 1}
        elif action == "marker":
            received["markers"].append(p)
            result = {"ok": True}
        else:
            result = {}
        RES_FILE.write_text(json.dumps({"id": last_id, "ok": True, "result": result}))


t = threading.Thread(target=fake_resolve, daemon=True)
t.start()

session = BridgeSession()
check("bridge ping", session.timeline_name() == "Interview")
check("bridge fps/resolution", session.fps() == FPS and session.resolution() == (1920, 1080))

from timeline_reader import read_timeline
snap = read_timeline(session, 1, 1)
c = snap.video_tracks[0].clips[0]
check("timeline_reader via clip shims",
      c.name == "interview.wav" and c.duration == 240 and c.source_path == str(wav))

from autocut import run_autocut, AutoCutParams
from config import JCutConfig

params = AutoCutParams(
    remove_silence=True, auto_threshold=False, silence_threshold_db=-42.0, min_silence_sec=0.4,
    padding_frames=6, remove_fillers=False, captions=False,
    chapters=True, chapter_gap_sec=1.0,
    punch_in=True, zoom_amount=1.15, build_mode="stream",
)
result = run_autocut(session, JCutConfig(fps=FPS), params)

check("timeline created with AutoCut name",
      received["created"] and "[AutoCut" in received["created"])
flat = [s for call in received["append_calls"] for s in call]
check("two keep-segments appended", len(flat) == 2, str(flat))
if len(flat) == 2:
    a, b = flat
    check("segment 1 ends at silence start (+padding)",
          a["start"] == 0 and abs(a["end"] - 78) <= 3, str(a))
    check("segment 2 starts at silence end (-padding)",
          abs(b["start"] - 114) <= 3 and b["end"] == 240, str(b))
check("~1.5s removed", abs(result["removed_sec"] - 1.5) < 0.3,
      f"{result['removed_sec']:.2f}s")
check("punch-in requested", result.get("zoomed_clips") == 1)
check("chapter marker placed at gap", len(received["markers"]) == 1
      and received["markers"][0]["color"] == "Purple", str(received["markers"]))

# ── 3b. Stream J-cut (split A/V via mediaType + recordFrame) ─────────────────
print("== E2E: stream J-cut ==")
received["append_calls"].clear()
params_j = AutoCutParams(
    remove_silence=True, auto_threshold=False, silence_threshold_db=-42.0,
    min_silence_sec=0.4, padding_frames=6, remove_fillers=False,
    captions=False, chapters=False, punch_in=False,
    build_mode="stream", jcut_frames=12,
)
result_j = run_autocut(session, JCutConfig(fps=FPS), params_j)
flatj = [s for call in received["append_calls"] for s in call]
vid = [s for s in flatj if s.get("mediaType") == 1]
aud = [s for s in flatj if s.get("mediaType") == 2]
check("stream jcut: 2 video + 2 audio segments",
      len(vid) == 2 and len(aud) == 2, str(flatj))
if len(vid) == 2 and len(aud) == 2:
    HOLD = 12
    # audio untouched: plain compact cut at 78
    check("stream jcut: audio unmoved and contiguous",
          aud[0]["start"] == 0 and abs(aud[0]["end"] - 78) <= 3
          and aud[0]["recordFrame"] == 86400
          and aud[1]["recordFrame"] - 86400 == aud[0]["end"] - aud[0]["start"]
          and abs(aud[1]["start"] - 114) <= 3, str(aud))
    # video 1 holds 12f into its removed silence; video 2 head trimmed 12f
    check("stream jcut: video 1 tail extends by hold",
          vid[0]["end"] - aud[0]["end"] == HOLD, str(vid))
    check("stream jcut: video 2 head trimmed by hold",
          vid[1]["start"] - aud[1]["start"] == HOLD
          and vid[1]["end"] == aud[1]["end"], str(vid))
    check("stream jcut: video cut lands hold frames after audio cut",
          vid[1]["recordFrame"] - aud[1]["recordFrame"] == HOLD)
    check("stream jcut: video blocks contiguous",
          vid[0]["recordFrame"] + (vid[0]["end"] - vid[0]["start"])
          == vid[1]["recordFrame"])
check("stream jcut: result reports lead", result_j.get("jcut_frames") == 12
      and result_j.get("jcut_cuts") == 1)

# ── 4. FCPXML mode ────────────────────────────────────────────────────────────
print("== E2E: FCPXML build mode ==")
params.build_mode = "fcpxml"
params.chapters = False


def handle_import(_last=[None]):
    pass  # import_timeline handled below


# extend fake responder for import_timeline via a second pass: rerun with patched thread
stop_flag.set(); t.join(timeout=1)
received["xml_path"] = None
stop_flag.clear()


def fake_resolve2():
    last_id = None
    while not stop_flag.is_set():
        time.sleep(0.05)
        if not REQ_FILE.exists():
            continue
        try:
            req = json.loads(REQ_FILE.read_text())
        except (ValueError, OSError):
            continue
        if req.get("id") == last_id:
            continue
        last_id = req["id"]
        action, p = req["action"], req.get("params") or {}
        if action == "ping":
            result = {"timeline_name": "Interview", "fps": FPS,
                      "start_frame": 86400, "width": 1920, "height": 1080}
        elif action == "clips":
            result = {"clips": [CLIP]}
        elif action == "import_timeline":
            received["xml_path"] = p["path"]
            result = {"timeline_name": "Imported"}
        else:
            result = {}
        RES_FILE.write_text(json.dumps({"id": last_id, "ok": True, "result": result}))


t2 = threading.Thread(target=fake_resolve2, daemon=True)
t2.start()

result = run_autocut(session, JCutConfig(fps=FPS), params)
check("fcpxml written and import requested",
      received["xml_path"] and Path(received["xml_path"]).exists())
if received["xml_path"]:
    import xml.dom.minidom
    doc = xml.dom.minidom.parse(received["xml_path"])
    clips_xml = doc.getElementsByTagName("asset-clip")
    zooms = doc.getElementsByTagName("adjust-transform")
    check("fcpxml has 4 asset-clips (video+audio lanes)",
          len(clips_xml) == 4, str(len(clips_xml)))
    check("fcpxml video clips on lane 1",
          sum(1 for c in clips_xml if c.getAttribute("lane") == "1") == 2)
    check("fcpxml audio clips on lane -1",
          sum(1 for c in clips_xml if c.getAttribute("lane") == "-1") == 2)
    assets = doc.getElementsByTagName("asset")
    check("fcpxml dual assets (video-only + audio-only)",
          len(assets) == 2
          and {a.getAttribute("hasVideo") + a.getAttribute("hasAudio")
               for a in assets} == {"10", "01"})
    vrefs = {c.getAttribute("ref") for c in clips_xml if c.getAttribute("lane") == "1"}
    arefs = {c.getAttribute("ref") for c in clips_xml if c.getAttribute("lane") == "-1"}
    check("fcpxml lanes reference split assets", vrefs != arefs
          and len(vrefs) == 1 and len(arefs) == 1)
    check("fcpxml has 1 punch-in transform (alternating)", len(zooms) == 1)
check("result reports imported timeline name", result["timeline"] == "Imported")

# ── 5. Analyze / preview toggle ───────────────────────────────────────────────
print("== Analyze phase & cut toggling ==")
from autocut import analyze_autocut, build_segments

p2 = AutoCutParams(remove_silence=True, auto_threshold=False,
                   silence_threshold_db=-42.0,
                   min_silence_sec=0.4, padding_frames=6, remove_fillers=False,
                   captions=False, chapters=False, punch_in=False)
analysis = analyze_autocut(session, JCutConfig(fps=FPS), p2)
check("analysis finds 1 planned cut", len(analysis.cuts) == 1,
      str(analysis.cuts))
check("cut reason is silence",
      bool(analysis.cuts) and "silence" in analysis.cuts[0].reason)
check("enabled cut -> 2 keep-segments", len(build_segments(analysis, p2)) == 2)
analysis.cuts[0].enabled = False
segs2 = build_segments(analysis, p2)
check("disabled cut -> 1 full segment",
      len(segs2) == 1 and (segs2[0].src_in, segs2[0].src_out) == (0, 240),
      str(segs2))

# ── 6. Cancellation ───────────────────────────────────────────────────────────
print("== Cancellation ==")
from transcribe import Cancelled

ev = threading.Event()
ev.set()
try:
    analyze_autocut(session, JCutConfig(fps=FPS), p2, cancel=ev)
    check("pre-set cancel raises Cancelled", False)
except Cancelled:
    check("pre-set cancel raises Cancelled", True)

stop_flag.set()

# ── 7. J-cut audio lead in FCPXML (pure, no bridge) ───────────────────────────
print("== FCPXML J-cut audio lead ==")
from fcpxml_export import write_fcpxml

jsegs = [Segment(0, 0, 78, timeline_offset=0),
         Segment(0, 114, 240, timeline_offset=78)]
jxml = SCRATCH / "jcut.fcpxml"
write_fcpxml(jxml, "JTest", jsegs, {0: str(wav)}, FPS, 1920, 1080,
             jcut_frames=12)
jdoc = xml.dom.minidom.parse(str(jxml))
ac = jdoc.getElementsByTagName("asset-clip")
video = [c for c in ac if c.getAttribute("lane") == "1"]
audio = [c for c in ac if c.getAttribute("lane") == "-1"]
fr = lambda v: int(v.split("/")[0])          # "102/24s" -> 102 frames
check("jcut: audio unmoved (plain cut at 78)",
      fr(audio[0].getAttribute("offset")) == 0
      and fr(audio[0].getAttribute("duration")) == 78
      and fr(audio[1].getAttribute("offset")) == 78
      and fr(audio[1].getAttribute("start")) == 114
      and fr(audio[1].getAttribute("duration")) == 126)
check("jcut: video 1 holds 12f into removed silence",
      fr(video[0].getAttribute("duration")) == 90,
      video[0].getAttribute("duration"))
check("jcut: video 2 head trimmed by 12f",
      fr(video[1].getAttribute("offset")) == 90
      and fr(video[1].getAttribute("start")) == 126
      and fr(video[1].getAttribute("duration")) == 114,
      video[1].getAttribute("start"))
check("jcut: video cut 12f after audio cut, total length unchanged",
      fr(video[1].getAttribute("offset"))
      - fr(audio[1].getAttribute("offset")) == 12
      and fr(video[1].getAttribute("offset"))
      + fr(video[1].getAttribute("duration")) == 78 + 126)

# ── 8. In-Resolve script segment logic (margins + review flags) ──────────────
print("== autocut_script clip_segments ==")
_repo = Path(__file__).resolve().parent.parent
_src = (_repo / "autocut_script.py").read_text()
_src = _src.replace('REPO = Path(r"__REPO__")', f'REPO = Path(r"{_repo}")')
_src = _src.replace("\nmain()\n", "\n")
_ns = {}
exec(compile(_src, "autocut_script.py", "exec"), _ns)


class _FakeMPI:
    def GetClipProperty(self):
        return {"File Path": str(wav)}


class _FakeItem:
    def GetLeftOffset(self):
        return 0

    def GetDuration(self):
        return 240

    def GetMediaPoolItem(self):
        return _FakeMPI()


_settings = {"threshold_db": -42.0, "min_silence": 0.4,
             "left_margin": 0.25, "right_margin": 0.25}
segs3, _mpi = _ns["clip_segments"](_FakeItem(), FPS, _settings)
aud = [(a, b) for a, b, f in segs3 if f]
sil = [(a, b) for a, b, f in segs3 if not f]
check("script: 2 audible + 1 silent segment",
      len(aud) == 2 and len(sil) == 1, str(segs3))
if sil:
    s0, e0 = sil[0]
    check("script: margins shrink silence by 6f each side",
          abs(s0 - 78) <= 3 and abs(e0 - 113) <= 3, str(sil[0]))
check("script: segments are contiguous",
      all(segs3[i][1] == segs3[i + 1][0] for i in range(len(segs3) - 1)))

print()
print(f"{'='*50}")
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("  FAILED:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
