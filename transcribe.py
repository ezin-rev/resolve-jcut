"""
Offline transcription via faster-whisper with word-level timestamps.

Models download automatically on first use (~75 MB for 'base') and are
cached in ~/.cache/huggingface. Everything runs locally — no API keys.
"""

from __future__ import annotations

import logging
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("autocut.transcribe")

_PUNCT = str.maketrans("", "", string.punctuation + "…。，、")


class Cancelled(RuntimeError):
    """User aborted the run."""


@dataclass
class Word:
    start: float      # seconds in source file
    end: float
    text: str         # raw text as transcribed

    @property
    def normalized(self) -> str:
        return self.text.strip().translate(_PUNCT).lower()


@dataclass
class Transcript:
    words: List[Word] = field(default_factory=list)
    language: str = ""

    def words_between(self, start_sec: float, end_sec: float) -> List[Word]:
        return [w for w in self.words if start_sec <= w.start < end_sec]


_model_cache: dict = {}
_transcript_cache: Dict[str, Transcript] = {}

# Friendly names → model repos. "mlx:" prefix = Apple-GPU engine via
# mlx-whisper; anything else goes to faster-whisper (CPU) as-is — built-in
# size names or a HuggingFace CTranslate2 repo id both work.
_LOCAL_MODELS = Path.home() / ".autocut" / "models"

MODEL_ALIASES = {
    # FULL Thonburian Whisper (biodatlab whisper-th-large-v3-combined) on the
    # Apple GPU — best Thai accuracy. Converted locally to MLX fp16 by
    # convert_whisper.py (mlx-examples); no published conversion exists.
    "thai-large-v3-mlx": f"mlx:{_LOCAL_MODELS / 'thai-large-v3-mlx'}",
    # Distilled Thai fine-tune — faster, slightly less accurate
    "thai-distill-mlx": "mlx:tawankri/distill-thonburian-whisper-large-v3-mlx",
    # Full Thai fine-tune on CPU — same weights as thai-large-v3-mlx, ~10x slower
    "thai-large-v3": "Vinxscribe/biodatlab-whisper-th-large-v3-faster",
    "large-v3-turbo-mlx": "mlx:mlx-community/whisper-large-v3-turbo",
}


def _resolve_model(model_size: str):
    repo = MODEL_ALIASES.get(model_size, model_size)
    if repo.startswith("mlx:"):
        return "mlx", repo[4:]
    return "ct2", repo


def _get_model(repo: str):
    if repo not in _model_cache:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper model '%s' (downloads on first run)…", repo)
        _model_cache[repo] = WhisperModel(repo, device="cpu", compute_type="int8")
    return _model_cache[repo]


def _audio_16k(path: str):
    """mlx-whisper shells out to an ffmpeg binary for file input (not
    installed here) — feed it 16 kHz mono samples decoded via PyAV instead."""
    import librosa
    from audio_analyzer import _load_audio
    y, sr = _load_audio(path)
    if sr != 16000:
        y = librosa.resample(y, orig_sr=sr, target_sr=16000)
    return y


def _transcribe_mlx(path, repo, language, progress, cancel):
    import mlx_whisper
    if repo.startswith("/") and not Path(repo).is_dir():
        fallback = MODEL_ALIASES["thai-distill-mlx"][4:]
        log.warning(
            "Local MLX model missing at %s — falling back to '%s'. "
            "See README for converting the full Thai model.", repo, fallback)
        repo = fallback
    log.info("Transcribing %s on the GPU (mlx, '%s')…",
             path.rsplit("/", 1)[-1], repo)
    if cancel is not None and cancel.is_set():
        raise Cancelled("Transcription cancelled")
    result = mlx_whisper.transcribe(
        _audio_16k(path),
        path_or_hf_repo=repo,
        language=language,
        word_timestamps=True,
    )
    transcript = Transcript(language=result.get("language", language or ""))
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            transcript.words.append(
                Word(start=float(w["start"]), end=float(w["end"]),
                     text=w["word"]))
    if progress is not None:
        progress(1.0)
    return transcript


def transcribe(
    path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    progress=None,          # progress(frac: float) called as segments stream in
    cancel=None,            # threading.Event — set() aborts with Cancelled
) -> Transcript:
    """Transcribe an audio/video file. Results cached per path for the session."""
    cache_key = f"{path}::{model_size}::{language}"
    if cache_key in _transcript_cache:
        return _transcript_cache[cache_key]

    engine, repo = _resolve_model(model_size)
    if engine == "mlx":
        transcript = _transcribe_mlx(path, repo, language, progress, cancel)
        _transcript_cache[cache_key] = transcript
        log.info("Transcribed: %d words, language=%s",
                 len(transcript.words), transcript.language)
        return transcript

    model = _get_model(repo)
    log.info("Transcribing %s …", path.rsplit("/", 1)[-1])
    segments, info = model.transcribe(
        path,
        language=language,
        word_timestamps=True,
        vad_filter=True,
    )
    duration = float(getattr(info, "duration", 0.0) or 0.0)

    transcript = Transcript(language=info.language)
    for seg in segments:   # generator — segments arrive as they are decoded
        if cancel is not None and cancel.is_set():
            raise Cancelled("Transcription cancelled")
        if progress is not None and duration:
            progress(min(seg.end / duration, 1.0))
        for w in seg.words or []:
            transcript.words.append(Word(start=w.start, end=w.end, text=w.word))

    log.info(
        "Transcribed: %d words, language=%s", len(transcript.words), info.language
    )
    _transcript_cache[cache_key] = transcript
    return transcript


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False
