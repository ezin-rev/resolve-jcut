"""Named parameter presets, stored as JSON in ~/.autocut/presets.json.

Built-in templates are always listed; a user preset saved under the same
name overrides the template (deleting it restores the template).
"""

from __future__ import annotations

import json
from pathlib import Path

PRESET_FILE = Path.home() / ".autocut" / "presets.json"

_BASE = {
    "remove_silence": True, "auto_threshold": True,
    "silence_threshold_db": -42.0, "min_silence_sec": 0.40,
    "padding_frames": 6, "remove_fillers": False,
    "filler_words": "um, uh, uhm, uhh, er, erm, hmm",
    "punch_in": False, "zoom_amount": 1.15,
    "captions": False, "chapters": False, "chapter_gap_sec": 2.0,
    "model_size": "base", "language": "auto",
    "build_mode": "stream", "jcut_frames": 0,
    "video_track": 1, "audio_track": 1,
}

TEMPLATES = {
    "Talking Head (J-cut)": {**_BASE, "jcut_frames": 4},
    "Podcast / Interview": {**_BASE, "min_silence_sec": 0.6,
                            "padding_frames": 8, "jcut_frames": 8,
                            "chapters": True, "chapter_gap_sec": 3.0},
    "Tutorial (tight cut)": {**_BASE, "min_silence_sec": 0.3,
                             "padding_frames": 4, "jcut_frames": 4,
                             "captions": True},
    "Social Clip (subtitles + zoom)": {**_BASE, "min_silence_sec": 0.3,
                                       "padding_frames": 4,
                                       "remove_fillers": True,
                                       "captions": True,
                                       "punch_in": True, "zoom_amount": 1.15},
}


def load_presets() -> dict:
    try:
        data = json.loads(PRESET_FILE.read_text())
        user = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        user = {}
    return {**TEMPLATES, **user}


def _load_user() -> dict:
    try:
        data = json.loads(PRESET_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preset(name: str, values: dict) -> None:
    user = _load_user()
    user[name] = values
    PRESET_FILE.parent.mkdir(exist_ok=True)
    PRESET_FILE.write_text(json.dumps(user, indent=2))


def delete_preset(name: str) -> None:
    user = _load_user()
    if user.pop(name, None) is not None:
        PRESET_FILE.parent.mkdir(exist_ok=True)
        PRESET_FILE.write_text(json.dumps(user, indent=2))
