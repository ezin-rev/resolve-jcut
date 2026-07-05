"""
FCPXML 1.9 export of the cut list.

The interchange-format route: instead of driving Resolve's API call by call,
write the entire edit — cuts, punch-in transforms AND J-cut audio leads — as
FCPXML and let Resolve (or Premiere, or FCP) import it in one shot. This is
how the commercial auto-editors support several NLEs with one codebase.

Structure: one <gap> spans the timeline; video segments connect on lane 1
(srcEnable="video"), audio on lane -1 (srcEnable="audio"). Splitting A from V
is what makes real J-cuts possible: every internal audio cut point shifts
jcut_frames EARLIER than the video cut, so you hear the next segment before
you see it. Safe because the cuts sit in removed silence — the lead-in audio
is quiet by construction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Tuple
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger("autocut.fcpxml")

# NTSC-family frame rates need 1001-based rational times
_NTSC = {23.976: (1001, 24000), 29.97: (1001, 30000), 59.94: (1001, 60000)}


def _fps_rational(fps: float) -> Tuple[int, int]:
    for rate, frac in _NTSC.items():
        if abs(fps - rate) < 0.01:
            return frac
    return (1, int(round(fps)))


def write_fcpxml(
    out_path: Path,
    project_name: str,
    segments: List,          # autocut.Segment, ordered by timeline_offset
    source_paths: Dict[int, str],   # clip_index -> absolute media path
    fps: float,
    width: int,
    height: int,
    punch_in: bool = False,
    zoom: float = 1.15,
    jcut_frames: int = 0,
) -> Path:
    num, den = _fps_rational(fps)

    def t(frames: int) -> str:
        return f"{frames * num}/{den}s"

    segments = [s for s in segments if source_paths.get(s.clip_index)]

    # TWO assets per source file: video-only and audio-only. Resolve's
    # importer ignores srcEnable and would give every clip both A and V —
    # split assets are the only way to keep the lanes clean (verified on
    # Resolve 21 free/MAS).
    asset_ids: Dict[str, Tuple[str, str]] = {}
    max_frame: Dict[str, int] = {}
    for seg in segments:
        path = source_paths[seg.clip_index]
        if path not in asset_ids:
            n = len(asset_ids)
            asset_ids[path] = (f"r{2 + 2 * n}", f"r{3 + 2 * n}")
        max_frame[path] = max(max_frame.get(path, 0), seg.src_out)

    # J-cut: the voice is never moved — the previous shot's VIDEO holds over
    # the start of the next line. Video tail extends into its own removed
    # silence; the next video head is trimmed by the same amount. Only
    # between segments of the same source clip with enough silence gap.
    j = max(int(jcut_frames), 0)
    n = len(segments)
    holds = [0] * n
    for i in range(n - 1):
        a, b = segments[i], segments[i + 1]
        if j > 0 and a.clip_index == b.clip_index:
            gap = b.src_in - a.src_out
            holds[i] = max(0, min(j, gap, (b.src_out - b.src_in) - 1))

    total = 0
    if segments:
        last = segments[-1]
        total = last.timeline_offset + (last.src_out - last.src_in)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE fcpxml>',
        '<fcpxml version="1.9">',
        '  <resources>',
        f'    <format id="r1" name="FFVideoFormatRateUndefined" '
        f'frameDuration="{num}/{den}s" width="{width}" height="{height}"/>',
    ]
    for path, (vid, aid) in asset_ids.items():
        src = "file://" + escape(str(Path(path).resolve()))
        name = escape(Path(path).name)
        common = (f'src={quoteattr(src)} start="0s" '
                  f'duration="{t(max_frame[path])}" format="r1"')
        lines.append(f'    <asset id="{vid}" name="{name}" {common} '
                     f'hasVideo="1" hasAudio="0"/>')
        lines.append(f'    <asset id="{aid}" name="{name}" {common} '
                     f'hasVideo="0" hasAudio="1"/>')
    lines += [
        '  </resources>',
        '  <library>',
        '    <event name="AutoCut">',
        f'      <project name={quoteattr(project_name)}>',
        '        <sequence format="r1">',
        '          <spine>',
        f'            <gap name="AutoCut" offset="0s" start="0s" '
        f'duration="{t(total)}">',
    ]

    for i, seg in enumerate(segments):
        path = source_paths[seg.clip_index]
        vid, aid = asset_ids[path]
        dur = seg.src_out - seg.src_in
        name = escape(Path(path).stem)

        prev_hold = holds[i - 1] if i > 0 else 0
        v_dur = dur - prev_hold + holds[i]
        video_open = (
            f'              <asset-clip ref="{vid}" lane="1" name="{name}" '
            f'offset="{t(seg.timeline_offset + prev_hold)}" '
            f'start="{t(seg.src_in + prev_hold)}" '
            f'duration="{t(v_dur)}" format="r1"'
        )
        if punch_in and i % 2 == 1:
            lines.append(video_open + ">")
            lines.append(
                f'                <adjust-transform scale="{zoom:.4f} {zoom:.4f}"/>'
            )
            lines.append('              </asset-clip>')
        else:
            lines.append(video_open + "/>")

        lines.append(
            f'              <asset-clip ref="{aid}" lane="-1" name="{name}" '
            f'offset="{t(seg.timeline_offset)}" start="{t(seg.src_in)}" '
            f'duration="{t(dur)}" format="r1"/>'
        )

    lines += [
        '            </gap>',
        '          </spine>',
        '        </sequence>',
        '      </project>',
        '    </event>',
        '  </library>',
        '</fcpxml>',
    ]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("FCPXML written: %s%s", out_path,
             f" (J-cut lead {j}f)" if j else "")
    return out_path
