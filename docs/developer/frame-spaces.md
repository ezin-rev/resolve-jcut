# Frame Spaces

Three distinct frame spaces flow through this codebase, plus seconds from the
audio side. **Mixing them up is the classic bug here** — everything looks
right in code review and the cuts land an hour off, or shifted by exactly the
padding amount.

## The three spaces

**Timeline frames, absolute**
:   What Resolve's timeline items report: `ClipInfo.start` / `ClipInfo.end`
    include the timeline start offset — usually 86400, because timelines
    start at 01:00:00:00 at 24 fps. `session.timeline_start_frame()` returns
    that offset.

**Source frames**
:   Positions within a source media file. `Segment.src_in` / `src_out`,
    `PlannedCut.start_frame` / `end_frame`, silence-detection output, and the
    `start` / `end` you pass to `append_segments` are all source frames.
    `GetLeftOffset()` on a timeline item is the clip's source in-point — the
    bridge exposes it as `left_offset`, and
    `clip_in = left_offset`, `clip_out = left_offset + duration` bound every
    per-clip computation in the pipeline.

**Marker frames (timeline-relative)**
:   `AddMarker` wants frames *relative to the timeline start* — the one
    Resolve call that doesn't take absolute timeline frames. Both session
    classes subtract `timeline_start_frame()` internally, so **callers pass
    absolute frames** and the session does the conversion. Don't subtract it
    yourself; that's a double-subtraction bug.

## Seconds

librosa and Whisper produce **seconds in source-file time**. Convert with the
*timeline* fps:

```python
source_frame = int(seconds * fps)
seconds = source_frame / fps
```

The SRT generator works in yet another unit — seconds on the **new, cut
timeline** — computed as `segment.timeline_offset / fps + (word.start -
segment_src_in_sec)`. Timeline offsets start at 0 for the new timeline;
the record position handed to Resolve is `timeline_start_frame() +
timeline_offset`.

## Rules of thumb

- Crossing the session boundary into Resolve? `append_segments` takes source
  frames; `recordFrame` takes absolute timeline frames; `set_marker` takes
  absolute timeline frames (converted for you).
- Comparing anything against silence regions or transcript words: convert to
  source frames or source seconds first, and clamp to
  `[left_offset, left_offset + duration]` — the clip usually uses only part
  of its source file.
- If an edit lands exactly one hour late, you added the timeline start offset
  twice; exactly one hour early, you forgot it.
