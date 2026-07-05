"""
One-click AutoCut pipeline (AutoCut / Firecut style).

Two phases, usable separately (GUI preview) or fused (run_autocut):
  analyze_autocut()  transcribe + detect → PlannedCuts; touches nothing
  apply_autocut()    build a NEW timeline from the enabled cuts

Steps inside apply, all optional except the cut itself:
  build timeline (stream or FCPXML, the latter with real J-cut audio leads)
  → alternate punch-in zoom → SRT captions → chapter markers
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from audio_analyzer import _detect_silence_regions, librosa_available
from config import JCutConfig
from timeline_reader import ClipInfo, read_timeline
from transcribe import Cancelled

log = logging.getLogger("autocut.pipeline")

# progress(fraction 0..1, human-readable message)
ProgressFn = Callable[[float, str], None]


@dataclass
class AutoCutParams:
    # Silence removal
    remove_silence: bool = True
    auto_threshold: bool = True     # per-file adaptive threshold (recommended)
    silence_threshold_db: float = -42.0
    min_silence_sec: float = 0.40
    padding_frames: int = 6

    # Filler word removal (needs Whisper)
    remove_fillers: bool = False
    filler_words: str = "um, uh, uhm, uhh, er, erm, hmm"

    # Punch-in zoom
    punch_in: bool = False
    zoom_amount: float = 1.15

    # Captions
    captions: bool = False

    # Chapter markers
    chapters: bool = False
    chapter_gap_sec: float = 2.0

    # Whisper
    model_size: str = "base"
    language: str = ""       # "" = auto-detect

    # "stream": append segments to the new timeline live, clip by clip
    # "fcpxml": write the whole edit as FCPXML and import in one shot
    build_mode: str = "stream"

    # Real J-cut in FCPXML mode: audio leads each video cut by this many
    # frames (safe because cuts sit in silence). 0 = off.
    jcut_frames: int = 0

    def filler_set(self) -> set:
        return {w.strip().lower() for w in self.filler_words.split(",") if w.strip()}

    def needs_transcript(self) -> bool:
        return self.remove_fillers or self.captions or self.chapters


@dataclass
class PlannedCut:
    clip_index: int
    clip_name: str
    start_frame: int        # source frames
    end_frame: int
    reason: str             # "silence (-51 dB)" / "filler 'um'"
    enabled: bool = True

    def duration_sec(self, fps: float) -> float:
        return (self.end_frame - self.start_frame) / fps


@dataclass
class Analysis:
    fps: float
    clips: List[ClipInfo]
    cuts: List[PlannedCut]
    transcripts: Dict[int, object] = field(default_factory=dict)

    def total_frames(self) -> int:
        return sum(c.duration for c in self.clips)


@dataclass
class Segment:
    clip_index: int
    src_in: int             # source frames
    src_out: int
    timeline_offset: int = 0   # frame position in the new timeline
    gap_before_sec: float = 0.0  # how much source time was removed before this


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("Cancelled by user")


def _merge_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged = [ranges[0]]
    for s, e in ranges[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _planned_cuts_for_clip(
    clip: ClipInfo,
    clip_index: int,
    params: AutoCutParams,
    fps: float,
    transcript,
) -> List[PlannedCut]:
    """Cuts to propose, in source frames, clamped to the clip. Not merged —
    each keeps its reason; overlaps collapse in build_segments()."""
    clip_in = clip.left_offset
    clip_out = clip_in + clip.duration
    cuts: List[PlannedCut] = []

    if params.remove_silence and clip.source_path and librosa_available():
        try:
            threshold = None if params.auto_threshold else params.silence_threshold_db
            regions = _detect_silence_regions(
                clip.source_path, threshold, params.min_silence_sec, fps,
            )
            log.debug("'%s': %d silence regions (threshold %s, >= %.2fs)",
                      clip.name, len(regions),
                      "auto" if threshold is None else f"{threshold:.0f} dB",
                      params.min_silence_sec)
            for s, e, db in regions:
                log.debug("  silence %.2fs–%.2fs (%.1f dB)", s / fps, e / fps, db)
                s = max(s + params.padding_frames, clip_in)
                e = min(e - params.padding_frames, clip_out)
                if e > s:
                    cuts.append(PlannedCut(clip_index, clip.name, s, e,
                                           f"silence ({db:.0f} dB)"))
        except Exception as exc:
            log.warning("Silence analysis failed for %s: %s: %s",
                        clip.name, type(exc).__name__, exc)

    if params.remove_fillers and transcript is not None:
        fillers = params.filler_set()
        for w in transcript.words:
            if w.normalized in fillers:
                s = max(int(w.start * fps) - 1, clip_in)
                e = min(int(w.end * fps) + 1, clip_out)
                if e > s:
                    log.debug("  filler '%s' at %.2fs", w.normalized, w.start)
                    cuts.append(PlannedCut(clip_index, clip.name, s, e,
                                           f"filler '{w.normalized}'"))

    return cuts


def _keep_segments(
    clip: ClipInfo, clip_index: int, cut_ranges: List[Tuple[int, int]]
) -> List[Segment]:
    clip_in = clip.left_offset
    clip_out = clip_in + clip.duration
    keep: List[Segment] = []
    cursor = clip_in
    for s, e in cut_ranges:
        if s > cursor:
            keep.append(Segment(clip_index, cursor, s))
        cursor = max(cursor, e)
    if cursor < clip_out:
        keep.append(Segment(clip_index, cursor, clip_out))
    if not keep:  # everything was silence — keep the clip rather than lose it
        keep.append(Segment(clip_index, clip_in, clip_out))
    return keep


def analyze_autocut(
    session,
    config: JCutConfig,
    params: AutoCutParams,
    progress: Optional[ProgressFn] = None,
    cancel=None,
) -> Analysis:
    """Phase 1: detect everything, cut nothing."""
    snapshot = read_timeline(session, config.video_track, config.audio_track)
    fps = snapshot.fps
    clips = sorted(snapshot.video_tracks[0].clips, key=lambda c: c.start)
    if not clips:
        raise RuntimeError(f"No clips on video track {config.video_track}")

    if params.needs_transcript():
        from transcribe import whisper_available
        if not whisper_available():
            raise RuntimeError("faster-whisper not installed — run: uv sync")

    transcripts: Dict[int, object] = {}
    by_path: Dict[str, object] = {}
    cuts: List[PlannedCut] = []
    n = len(clips)

    for idx, clip in enumerate(clips):
        _check(cancel)
        if progress:
            progress(idx / n, f"Analyzing clip {idx + 1}/{n}: {clip.name}")
        if params.needs_transcript() and clip.source_path:
            from transcribe import transcribe
            if clip.source_path not in by_path:
                sub = None
                if progress:
                    sub = (lambda f, base=idx / n:
                           progress(base + f / n,
                                    f"Transcribing {idx + 1}/{n}: {clip.name}"))
                by_path[clip.source_path] = transcribe(
                    clip.source_path,
                    model_size=params.model_size,
                    language=params.language or None,
                    progress=sub,
                    cancel=cancel,
                )
            transcripts[idx] = by_path[clip.source_path]
        cuts.extend(_planned_cuts_for_clip(clip, idx, params, fps,
                                           transcripts.get(idx)))

    if progress:
        progress(1.0, "Analysis complete")
    log.info("Analysis: %d planned cuts across %d clips", len(cuts), n)
    return Analysis(fps=fps, clips=clips, cuts=cuts, transcripts=transcripts)


def build_segments(analysis: Analysis, params: AutoCutParams) -> List[Segment]:
    """Invert the enabled cuts into keep-segments with timeline positions."""
    segments: List[Segment] = []
    cursor = 0
    fps = analysis.fps
    for idx, clip in enumerate(analysis.clips):
        ranges = _merge_ranges([
            (c.start_frame, c.end_frame)
            for c in analysis.cuts if c.clip_index == idx and c.enabled
        ])
        segs = _keep_segments(clip, idx, ranges)
        prev_out: Optional[int] = None
        clip_kept = 0
        for seg in segs:
            if prev_out is not None:
                seg.gap_before_sec = (seg.src_in - prev_out) / fps
            prev_out = seg.src_out
            seg.timeline_offset = cursor
            cursor += seg.src_out - seg.src_in
            clip_kept += seg.src_out - seg.src_in
            segments.append(seg)
            log.debug("  keep src %d–%d (%.2fs) → timeline %.2fs",
                      seg.src_in, seg.src_out,
                      (seg.src_out - seg.src_in) / fps,
                      seg.timeline_offset / fps)
        log.info("Clip %d/%d '%s': %d segments, kept %.1fs / removed %.1fs",
                 idx + 1, len(analysis.clips), clip.name, len(segs),
                 clip_kept / fps, (clip.duration - clip_kept) / fps)
    return segments


def apply_autocut(
    session,
    config: JCutConfig,
    params: AutoCutParams,
    analysis: Analysis,
    progress: Optional[ProgressFn] = None,
    cancel=None,
) -> dict:
    """Phase 2: build the new timeline from the enabled cuts."""
    fps = analysis.fps
    clips = analysis.clips
    transcripts = analysis.transcripts
    segments = build_segments(analysis, params)
    kept = sum(s.src_out - s.src_in for s in segments)

    name = f"{session.timeline_name()} [AutoCut {time.strftime('%H.%M.%S')}]"
    streaming = params.build_mode == "stream"
    result = {
        "timeline": name,
        "segments": len(segments),
        "removed_sec": max(analysis.total_frames() - kept, 0) / fps,
    }

    if streaming:
        session.begin_edit(config.video_track)
        session.create_timeline(name)
        session.refresh_timeline()
        log.info("Created timeline '%s' — cuts will appear as clips finish", name)
        j = max(int(params.jcut_frames), 0)
        appended = 0
        if j > 0:
            # Real J-cut: the voice is NEVER moved or padded — the previous
            # shot's VIDEO holds over the start of the next line. Video tail
            # extends into its own removed silence (the speaker pausing on
            # camera); the next segment's video head is trimmed by the same
            # amount. Only possible between segments of the same source clip
            # with enough removed silence between them.
            start = session.timeline_start_frame()
            n = len(segments)
            holds = [0] * n          # holds[i] = video hold at the cut AFTER segment i
            for i in range(n - 1):
                a, b = segments[i], segments[i + 1]
                if a.clip_index == b.clip_index:
                    gap = b.src_in - a.src_out
                    holds[i] = max(0, min(j, gap, (b.src_out - b.src_in) - 1))
            video_batch = []
            for i, s in enumerate(segments):
                prev_hold = holds[i - 1] if i > 0 else 0
                video_batch.append(
                    {"clip_index": s.clip_index,
                     "start": s.src_in + prev_hold,
                     "end": s.src_out + holds[i],
                     "mediaType": 1,
                     "recordFrame": start + s.timeline_offset + prev_hold}
                )
            audio_batch = [
                {"clip_index": s.clip_index, "start": s.src_in, "end": s.src_out,
                 "mediaType": 2, "recordFrame": start + s.timeline_offset}
                for s in segments
            ]
            total = len(video_batch) + len(audio_batch)
            done = 0
            for batch in (video_batch, audio_batch):
                for k in range(0, len(batch), 20):
                    _check(cancel)
                    chunk = batch[k:k + 20]
                    appended += session.append_segments(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done / total, f"Placing segments {done}/{total}")
            result["jcut_frames"] = j
            result["jcut_cuts"] = sum(1 for h in holds if h > 0)
            log.info("J-cut: video holds over the next line at %d of %d cuts",
                     result["jcut_cuts"], max(n - 1, 0))
        else:
            by_clip: Dict[int, List[Segment]] = {}
            for s in segments:
                by_clip.setdefault(s.clip_index, []).append(s)
            for done, idx in enumerate(sorted(by_clip), 1):
                _check(cancel)
                appended += session.append_segments(
                    [{"clip_index": s.clip_index, "start": s.src_in, "end": s.src_out}
                     for s in by_clip[idx]]
                )
                if progress:
                    progress(done / len(by_clip),
                             f"Appending clip {done}/{len(by_clip)}")
        session.refresh_timeline()
        result["appended"] = appended
        if params.punch_in:
            result["zoomed_clips"] = session.alternate_zoom(1, params.zoom_amount)
    else:
        from fcpxml_export import write_fcpxml
        first_src = next((c.source_path for c in clips if c.source_path), "")
        out_dir = Path(first_src).parent if first_src else Path.home() / "Desktop"
        xml_path = out_dir / f"{name}.fcpxml"
        width, height = session.resolution()
        if progress:
            progress(0.3, "Writing FCPXML…")
        write_fcpxml(
            xml_path, name, segments,
            {i: c.source_path for i, c in enumerate(clips)},
            fps, width, height,
            punch_in=params.punch_in, zoom=params.zoom_amount,
            jcut_frames=params.jcut_frames,
        )
        if progress:
            progress(0.6, "Importing timeline into Resolve…")
        imported_name = session.import_timeline_file(str(xml_path))
        session.refresh_timeline()
        log.warning(
            "Resolve free (MAS) imports FCPXML with OFFLINE media — the file "
            "itself is valid (Premiere/FCP import it fine). For Resolve, use "
            "the Live append build mode: J-cuts work there too.")
        result["timeline"] = imported_name or name
        result["fcpxml_path"] = str(xml_path)
        if params.jcut_frames:
            result["jcut_frames"] = params.jcut_frames
        if params.punch_in:
            result["zoomed_clips"] = sum(1 for i in range(len(segments)) if i % 2 == 1)

    # Captions
    if params.captions:
        if transcripts:
            first_src = next((c.source_path for c in clips if c.source_path), "")
            out_dir = Path(first_src).parent if first_src else Path.home() / "Desktop"
            srt_path = out_dir / f"{name}.srt"
            count = _generate_srt(segments, transcripts, fps, srt_path)
            placed = False
            try:
                placed = session.import_srt(str(srt_path))
            except Exception as exc:
                log.debug("import_srt failed: %s", exc)
            if not placed:
                session.import_media([str(srt_path)])
                log.warning("Could not place SRT on a subtitle track — it is "
                            "in the media pool; drag it onto the timeline")
            result["srt_path"] = str(srt_path)
            result["captions"] = count
            result["srt_on_timeline"] = placed
        else:
            log.warning("Captions requested but no transcript in this analysis — "
                        "re-run Analyze with captions enabled")

    # Chapter markers
    if params.chapters:
        start_frame = session.timeline_start_frame()
        placed = 0
        for seg in segments:
            if seg.gap_before_sec >= params.chapter_gap_sec:
                title = f"Chapter {placed + 2}"
                transcript = transcripts.get(seg.clip_index)
                if transcript is not None:
                    words = transcript.words_between(
                        seg.src_in / fps, seg.src_in / fps + 4.0
                    )[:5]
                    if words:
                        title = "".join(w.text for w in words).strip()
                session.set_marker(
                    start_frame + seg.timeline_offset, "Purple", title, "chapter"
                )
                placed += 1
        result["chapters"] = placed

    if progress:
        progress(1.0, "Done")
    return result


def run_autocut(
    session,
    config: JCutConfig,
    params: AutoCutParams,
    progress: Optional[ProgressFn] = None,
    cancel=None,
) -> dict:
    """Analyze + apply in one shot (no preview)."""
    p1 = (lambda f, m: progress(f * 0.7, m)) if progress else None
    p2 = (lambda f, m: progress(0.7 + f * 0.3, m)) if progress else None
    analysis = analyze_autocut(session, config, params, progress=p1, cancel=cancel)
    return apply_autocut(session, config, params, analysis,
                         progress=p2, cancel=cancel)


def _format_srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# Thai has no spaces and Whisper emits sub-word fragments — a line must not
# start with a dependent vowel/tone mark, nor end on a leading vowel, or the
# break lands mid-syllable (า/ำ orphaned at the start of the next caption).
_TH_NO_LINE_START = set("ะัาำิีึืุู็่้๊๋์ๆฯๅ")
_TH_NO_LINE_END = set("เแโใไ")


def _safe_break(prev_text: str, next_text: str) -> bool:
    p = prev_text.rstrip()
    n = next_text.lstrip()
    if not p or not n:
        return True
    return n[0] not in _TH_NO_LINE_START and p[-1] not in _TH_NO_LINE_END


def _generate_srt(
    segments: List[Segment],
    transcripts: Dict[int, object],
    fps: float,
    out_path: Path,
) -> int:
    """Map transcript words through the kept segments onto new-timeline time."""
    entries: List[Tuple[float, float, str]] = []
    line_words: List[Tuple[float, float, str]] = []

    def flush():
        if not line_words:
            return
        start = line_words[0][0]
        end = line_words[-1][1]
        text = "".join(w[2] for w in line_words).strip()
        if text:
            entries.append((start, end, text))
        line_words.clear()

    for seg in segments:
        transcript = transcripts.get(seg.clip_index)
        if transcript is None:
            continue
        src_in_sec = seg.src_in / fps
        src_out_sec = seg.src_out / fps
        for w in transcript.words_between(src_in_sec, src_out_sec):
            tl_sec = (seg.timeline_offset / fps) + (w.start - src_in_sec)
            tl_end = tl_sec + (w.end - w.start)
            if line_words:
                prev_end = line_words[-1][1]
                line_len = sum(len(x[2]) for x in line_words)
                over = (tl_sec - prev_end > 0.7 or line_len > 38
                        or tl_end - line_words[0][0] > 4.0)
                if over and _safe_break(line_words[-1][2], w.text):
                    flush()
            line_words.append((tl_sec, tl_end, w.text))
        flush()   # a caption never straddles a timeline cut

    with out_path.open("w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(entries, 1):
            f.write(f"{i}\n{_format_srt_time(start)} --> "
                    f"{_format_srt_time(end)}\n{text}\n\n")
    return len(entries)
