"""
Core J-cut / L-cut computation and application.

J-cut:  audio from the NEXT clip starts BEFORE the video cut.
         [Video A --------][Video B --------]
         [Audio A ----][Audio B --------]
              overlap ^

L-cut:  audio from the PREVIOUS clip continues PAST the video cut.
         [Video A --------][Video B --------]
         [Audio A --------][Audio B ----]
                    overlap ^

Both require available handle frames on the adjacent clips. We check handles
before planning and mark plans infeasible rather than silently clamping.

Resolve API limitations
-----------------------
Resolve's Python API does not expose SetLeftOffset / SetRightOffset on
TimelineItem as of version 18.x. The apply_jcuts() function uses the available
trim approach (SetProperty on some builds) and falls back to marker placement
so the editor can complete the trims manually. In dry_run mode only markers are
placed — the timeline is never modified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from config import JCutConfig
from resolve_bridge import ResolveSession
from timeline_reader import ClipInfo, CutPoint, TimelineSnapshot, read_timeline

log = logging.getLogger("autocut.engine")


@dataclass
class JCutPlan:
    cut_frame: int
    jcut_frames: int           # audio leads by this many frames
    lcut_frames: int           # audio trails by this many frames (L-cut)
    pre_video: ClipInfo
    post_video: ClipInfo
    pre_audio: Optional[ClipInfo]   # audio clip ending at cut
    post_audio: Optional[ClipInfo]  # audio clip starting at cut
    feasible: bool = True
    skip_reason: str = ""

    def describe(self) -> str:
        if not self.feasible:
            return f"frame {self.cut_frame}: SKIP — {self.skip_reason}"
        parts = []
        if self.jcut_frames:
            parts.append(f"J-cut {self.jcut_frames}f")
        if self.lcut_frames:
            parts.append(f"L-cut {self.lcut_frames}f")
        return f"frame {self.cut_frame}: {' + '.join(parts)}"


def _find_audio_clip_at(
    audio_clips: List[ClipInfo], frame: int, side: str
) -> Optional[ClipInfo]:
    """
    side='pre'  → clip whose end == frame
    side='post' → clip whose start == frame
    """
    for clip in audio_clips:
        if side == "pre" and clip.end == frame:
            return clip
        if side == "post" and clip.start == frame:
            return clip
    return None


def build_plan(
    snapshot: TimelineSnapshot,
    config: JCutConfig,
) -> List[JCutPlan]:
    video_track = snapshot.video_tracks[0]
    audio_track = snapshot.audio_tracks[0]
    audio_clips = audio_track.clips

    plans: List[JCutPlan] = []

    for cut in video_track.cut_points():
        pre_v = cut.pre_clip
        post_v = cut.post_clip

        # Skip very short clips — likely flash cuts or handles
        if (
            pre_v.duration < config.min_clip_duration_frames
            or post_v.duration < config.min_clip_duration_frames
        ):
            plans.append(
                JCutPlan(
                    cut_frame=cut.frame,
                    jcut_frames=config.jcut_frames,
                    lcut_frames=config.lcut_frames,
                    pre_video=pre_v,
                    post_video=post_v,
                    pre_audio=None,
                    post_audio=None,
                    feasible=False,
                    skip_reason="clip too short",
                )
            )
            continue

        pre_a = _find_audio_clip_at(audio_clips, cut.frame, "pre")
        post_a = _find_audio_clip_at(audio_clips, cut.frame, "post")

        if pre_a is None or post_a is None:
            plans.append(
                JCutPlan(
                    cut_frame=cut.frame,
                    jcut_frames=config.jcut_frames,
                    lcut_frames=config.lcut_frames,
                    pre_video=pre_v,
                    post_video=post_v,
                    pre_audio=pre_a,
                    post_audio=post_a,
                    feasible=False,
                    skip_reason="audio clips not aligned with video cut",
                )
            )
            continue

        # Check handle availability
        # J-cut: post_audio needs left_offset >= jcut_frames
        #        pre_audio needs right_offset >= jcut_frames
        j_ok = (
            post_a.left_offset >= config.jcut_frames
            and pre_a.right_offset >= config.jcut_frames
        )
        # L-cut: pre_audio needs right_offset >= lcut_frames
        #        post_audio needs left_offset >= lcut_frames
        l_ok = config.lcut_frames == 0 or (
            pre_a.right_offset >= config.lcut_frames
            and post_a.left_offset >= config.lcut_frames
        )

        feasible = j_ok and l_ok
        reason = ""
        if not j_ok:
            avail_pre = pre_a.right_offset
            avail_post = post_a.left_offset
            reason = (
                f"insufficient handles for J-cut "
                f"(need {config.jcut_frames}f, "
                f"pre has {avail_pre}f right / post has {avail_post}f left)"
            )
        elif not l_ok:
            reason = f"insufficient handles for L-cut (need {config.lcut_frames}f)"

        plans.append(
            JCutPlan(
                cut_frame=cut.frame,
                jcut_frames=config.jcut_frames,
                lcut_frames=config.lcut_frames,
                pre_video=pre_v,
                post_video=post_v,
                pre_audio=pre_a,
                post_audio=post_a,
                feasible=feasible,
                skip_reason=reason,
            )
        )

    return plans


def _try_api_trim(plan: JCutPlan) -> bool:
    """
    Attempt direct trim via Resolve API. Returns True if successful.

    Resolve 18.x does not publicly expose SetLeftOffset/SetRightOffset.
    We probe SetProperty as a best-effort attempt; this may work on newer builds.
    """
    if plan.pre_audio is None or plan.post_audio is None:
        return False
    try:
        pre_item = plan.pre_audio._item
        post_item = plan.post_audio._item
        if plan.jcut_frames > 0:
            # Trim post_audio left handle earlier (audio starts sooner)
            # Trim pre_audio right handle earlier (audio ends sooner)
            new_post_left = post_item.GetLeftOffset() - plan.jcut_frames
            new_pre_right = pre_item.GetRightOffset() - plan.jcut_frames
            ok1 = post_item.SetProperty("clipIn", post_item.GetProperty("clipIn") - plan.jcut_frames)
            ok2 = pre_item.SetProperty("clipOut", pre_item.GetProperty("clipOut") - plan.jcut_frames)
            return bool(ok1 and ok2)
    except Exception as exc:
        log.debug("API trim failed: %s", exc)
    return False


def apply_jcuts(
    plans: List[JCutPlan],
    session: ResolveSession,
    config: JCutConfig,
) -> dict:
    applied = 0
    skipped = 0
    marked = 0
    errors: List[str] = []

    feasible_plans = [p for p in plans if p.feasible]

    for plan in feasible_plans:
        if config.dry_run:
            # Dry run: place a marker so the editor sees the proposed cut
            label = f"J-cut {plan.jcut_frames}f"
            if plan.lcut_frames:
                label += f" / L-cut {plan.lcut_frames}f"
            try:
                session.set_marker(
                    plan.cut_frame,
                    config.marker_color,
                    label,
                    plan.describe(),
                )
                marked += 1
                log.info("Marker placed at %d — %s", plan.cut_frame, label)
            except Exception as exc:
                errors.append(f"Marker at {plan.cut_frame}: {exc}")
            continue

        # Live apply: try API trim, fall back to marker
        if _try_api_trim(plan):
            applied += 1
            log.info("Applied %s", plan.describe())
        else:
            # API trim not available on this Resolve build — place marker
            try:
                session.set_marker(
                    plan.cut_frame,
                    "Yellow",
                    "J-cut (manual)",
                    f"{plan.describe()} — manual trim required",
                )
                marked += 1
            except Exception as exc:
                errors.append(f"Fallback marker at {plan.cut_frame}: {exc}")
            log.warning(
                "API trim unavailable for frame %d; marker placed for manual edit",
                plan.cut_frame,
            )

    for plan in plans:
        if not plan.feasible:
            skipped += 1
            log.info("Skipped %s", plan.describe())

    return {
        "total": len(plans),
        "feasible": len(feasible_plans),
        "applied": applied,
        "marked": marked,
        "skipped": skipped,
        "errors": errors,
    }


def run(session: ResolveSession, config: JCutConfig) -> dict:
    snapshot = read_timeline(session, config.video_track, config.audio_track)
    config.fps = snapshot.fps
    plans = build_plan(snapshot, config)
    results = apply_jcuts(plans, session, config)
    results["plans"] = plans
    return results
