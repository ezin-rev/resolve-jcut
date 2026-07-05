"""
Detects actual cut points inside source audio files.

Strategy: load the source audio, find silence regions (below threshold),
and mark their midpoints as cut candidates. These are compared against
the timeline clip boundaries to decide whether a cut is audio-driven or
picture-driven — which informs whether a J-cut makes editorial sense.

librosa is optional. If not installed we fall back to timeline boundaries only.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from timeline_reader import ClipInfo

log = logging.getLogger("autocut.audio")


@dataclass
class AudioCutCandidate:
    source_path: str
    # Frame offset within the source file (NOT the timeline)
    source_frame: int
    silence_db: float
    duration_frames: int


def _has_librosa() -> bool:
    try:
        import librosa  # noqa: F401
        return True
    except ImportError:
        return False


def _load_audio(path: str):
    """Mono float samples + sample rate. librosa/soundfile can't decode
    MP4/AAC without an ffmpeg CLI; PyAV (bundled with faster-whisper) can."""
    import numpy as np
    try:
        import librosa
        return librosa.load(path, sr=None, mono=True)
    except Exception:
        pass

    import av
    chunks = []
    with av.open(path) as container:
        stream = container.streams.audio[0]
        sr = stream.rate or 48000
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for frame in container.decode(stream):
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
        try:
            for rf in resampler.resample(None):   # flush
                chunks.append(rf.to_ndarray().reshape(-1))
        except Exception:
            pass
    if not chunks:
        raise RuntimeError(f"No audio decoded from {path}")
    y = np.concatenate(chunks).astype("float32") / 32768.0
    return y, sr


def _adaptive_threshold(rms_db) -> float:
    """Split point between this file's noise floor and its speech level.
    Camera auto-gain keeps 'silence' hot (−20-ish dB, not −40) — a fixed
    threshold fails on real footage."""
    import numpy as np
    floor = float(np.percentile(rms_db, 5))
    speech = float(np.percentile(rms_db, 75))
    if speech - floor < 6:          # flat audio, no usable separation
        return floor + 3.0
    return floor + (speech - floor) * 0.35


def _detect_silence_regions(
    path: str,
    threshold_db: Optional[float],
    min_silence_sec: float,
    fps: float,
) -> List[Tuple[int, int, float]]:
    """
    Returns list of (start_frame, end_frame, mean_db) within the source file.
    Frames are in source-file space, not timeline space.
    threshold_db=None picks a per-file adaptive threshold.
    """
    import librosa
    import numpy as np

    y, sr = _load_audio(path)
    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    rms_db = librosa.amplitude_to_db(rms, ref=np.max)

    if threshold_db is None:
        threshold_db = _adaptive_threshold(rms_db)
        log.info("Auto threshold for %s: %.1f dB", Path(path).name, threshold_db)

    min_frames_audio = int(min_silence_sec * sr / hop_length)
    silent_mask = rms_db < threshold_db

    regions: List[Tuple[int, int, float]] = []
    i = 0
    while i < len(silent_mask):
        if silent_mask[i]:
            j = i
            while j < len(silent_mask) and silent_mask[j]:
                j += 1
            if (j - i) >= min_frames_audio:
                # Convert hop-frames → source video frames
                sec_start = librosa.frames_to_time(i, sr=sr, hop_length=hop_length)
                sec_end = librosa.frames_to_time(j, sr=sr, hop_length=hop_length)
                mean_db = float(rms_db[i:j].mean())
                regions.append(
                    (int(sec_start * fps), int(sec_end * fps), mean_db)
                )
            i = j
        else:
            i += 1
    return regions


def analyze_clip(
    clip: ClipInfo,
    threshold_db: float,
    min_silence_sec: float,
    fps: float,
) -> List[AudioCutCandidate]:
    if not clip.source_path or not Path(clip.source_path).exists():
        return []
    if not _has_librosa():
        return []

    regions = _detect_silence_regions(
        clip.source_path, threshold_db, min_silence_sec, fps
    )
    candidates: List[AudioCutCandidate] = []
    for start_f, end_f, mean_db in regions:
        midpoint = (start_f + end_f) // 2
        # Only consider candidates that fall within the clip's source range
        clip_in = clip.left_offset
        clip_out = clip_in + clip.duration
        if clip_in <= midpoint <= clip_out:
            candidates.append(
                AudioCutCandidate(
                    source_path=clip.source_path,
                    source_frame=midpoint,
                    silence_db=mean_db,
                    duration_frames=end_f - start_f,
                )
            )
    return candidates


def librosa_available() -> bool:
    return _has_librosa()
