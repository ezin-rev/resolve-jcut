"""
Reads the active Resolve timeline into plain Python dataclasses.

TimelineItem objects from the Resolve API are not serialisable and can
become stale after any timeline edit, so we snapshot everything we need
up front and keep the raw item reference only for write-back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Any

from resolve_bridge import ResolveSession


@dataclass
class ClipInfo:
    name: str
    track_type: str           # "video" | "audio"
    track_index: int
    start: int                # timeline frame (absolute, from timeline start)
    end: int                  # exclusive: start + duration
    duration: int             # frames
    left_offset: int          # frames already trimmed from head (used handle)
    right_offset: int         # frames already trimmed from tail
    source_path: str          # absolute path to source media file
    _item: Any = field(repr=False, compare=False)  # raw TimelineItem


@dataclass
class CutPoint:
    frame: int                # the exact frame where the cut occurs
    pre_clip: ClipInfo        # clip that ends here
    post_clip: ClipInfo       # clip that starts here


@dataclass
class TrackSnapshot:
    track_type: str
    track_index: int
    clips: List[ClipInfo] = field(default_factory=list)

    def cut_points(self) -> List[CutPoint]:
        sorted_clips = sorted(self.clips, key=lambda c: c.start)
        cuts: List[CutPoint] = []
        for i in range(len(sorted_clips) - 1):
            a = sorted_clips[i]
            b = sorted_clips[i + 1]
            if a.end == b.start:
                cuts.append(CutPoint(frame=a.end, pre_clip=a, post_clip=b))
        return cuts


@dataclass
class TimelineSnapshot:
    fps: float
    start_frame: int
    video_tracks: List[TrackSnapshot] = field(default_factory=list)
    audio_tracks: List[TrackSnapshot] = field(default_factory=list)


def _read_track(
    session: ResolveSession, track_type: str, index: int
) -> TrackSnapshot:
    snapshot = TrackSnapshot(track_type=track_type, track_index=index)
    raw_clips = session.clips_on_track(track_type, index)
    for item in raw_clips:
        try:
            source_path = _get_source_path(item)
        except Exception:
            source_path = ""
        clip = ClipInfo(
            name=item.GetName(),
            track_type=track_type,
            track_index=index,
            start=item.GetStart(),
            end=item.GetEnd(),
            duration=item.GetDuration(),
            left_offset=item.GetLeftOffset(),
            right_offset=item.GetRightOffset(),
            source_path=source_path,
            _item=item,
        )
        snapshot.clips.append(clip)
    return snapshot


def _get_source_path(item: Any) -> str:
    pool_item = item.GetMediaPoolItem()
    if pool_item is None:
        return ""
    props = pool_item.GetClipProperty()
    if isinstance(props, dict):
        return props.get("File Path", "")
    return ""


def read_timeline(
    session: ResolveSession,
    video_track: int,
    audio_track: int,
) -> TimelineSnapshot:
    snapshot = TimelineSnapshot(
        fps=session.fps(),
        start_frame=session.timeline_start_frame(),
    )
    snapshot.video_tracks.append(_read_track(session, "video", video_track))
    snapshot.audio_tracks.append(_read_track(session, "audio", audio_track))
    return snapshot
