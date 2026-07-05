"""
Detection CLI — prints silence regions of a media file as JSON.

Called by AutoCut.py (running inside DaVinci Resolve) through this repo's
venv python, so the heavy deps (librosa) never need to exist in Resolve's
own Python. Frames are in source-file frame space at the given fps.

    .venv/bin/python cutlist.py clip.mp4 --fps 23.976 --threshold-db -42
"""

from __future__ import annotations

import argparse
import json
import sys

from audio_analyzer import _detect_silence_regions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--fps", type=float, required=True)
    ap.add_argument("--threshold-db", default="auto",
                    help='dB below peak, or "auto" for per-file adaptive')
    ap.add_argument("--min-silence", type=float, default=0.40)
    args = ap.parse_args()

    threshold = None if str(args.threshold_db).lower() == "auto" \
        else float(args.threshold_db)
    regions = _detect_silence_regions(
        args.file, threshold, args.min_silence, args.fps
    )
    json.dump(
        {"fps": args.fps,
         "silences": [[int(s), int(e), round(float(db), 1)] for s, e, db in regions]},
        sys.stdout,
    )


if __name__ == "__main__":
    main()
