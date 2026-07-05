from dataclasses import dataclass, field


@dataclass
class JCutConfig:
    # J-cut: audio from next clip leads the video cut
    jcut_frames: int = 24
    # L-cut: video from next clip leads the audio (set 0 to disable)
    lcut_frames: int = 0

    video_track: int = 1
    audio_track: int = 1

    # Skip clips shorter than this (avoids touching b-roll flash cuts)
    min_clip_duration_frames: int = 12

    # Audio analysis settings
    use_audio_analysis: bool = True
    silence_threshold_db: float = -42.0
    # Min silence duration to count as a cut (seconds)
    min_silence_sec: float = 0.05

    # Dry-run: compute plan but do not modify the timeline
    dry_run: bool = True

    # Marker placed at each J-cut point when dry_run=True
    marker_color: str = "Blue"

    # Resolve frame rate (updated at runtime from project)
    fps: float = 24.0

    @property
    def jcut_seconds(self) -> float:
        return self.jcut_frames / self.fps if self.fps else 1.0
