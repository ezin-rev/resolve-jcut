# Parameter Reference

Every setting in the GUI maps 1:1 to a field on `AutoCutParams`
(`autocut.py`). Presets are snapshots of these values.

## Silence removal

`remove_silence` — *default: on*
:   The core feature. Detects below-threshold regions in each clip's source
    audio and plans a cut for each one.

`auto_threshold` — *default: on*
:   Pick a silence threshold **per file** by measuring its noise floor and
    speech level (see [How It Works](how-it-works.md#the-adaptive-threshold)).
    Recommended: camera auto-gain keeps "silence" around −20 dB on real
    footage, where any sane fixed threshold finds nothing.

`silence_threshold_db` — *default: −42.0*
:   Fixed threshold in dBFS, used only when `auto_threshold` is off. Audio
    below this level counts as silence. Only reach for this when the auto
    threshold misjudges a specific file (e.g. music beds under speech).

`min_silence_sec` — *default: 0.40*
:   A quiet region shorter than this is not cut. Lower values (0.3) give a
    tighter, faster-paced edit; higher values (0.6+) preserve natural
    breathing pauses — the podcast template uses 0.6.

`padding_frames` — *default: 6*
:   Frames kept on **each side** of every detected silence, so words are
    never clipped mid-syllable. At 24 fps the default keeps a quarter second
    around each cut. If cuts feel like they swallow word endings, raise
    this before touching anything else.

## Filler word removal

`remove_fillers` — *default: off*
:   Cut transcript words that match the filler list. Requires Whisper (a
    transcription run happens during Analyze).

`filler_words` — *default: `um, uh, uhm, uhh, er, erm, hmm`*
:   Comma-separated, case-insensitive; punctuation is stripped before
    matching, so "Um," matches "um". Each removed filler is cut with one
    frame of margin on each side.

## J-cut

`jcut_frames` — *default: 0 (off)*
:   How many frames the previous shot's **video** holds over the start of the
    next line at each cut. The audio is never moved — see
    [How It Works](how-it-works.md#stream-mode-default) for the exact hold
    math. 4–8 frames reads naturally; more starts to feel like a mistake.
    Holds only happen between segments of the same source clip.

## Punch-in zoom

`punch_in` — *default: off*
:   Alternate every second clip on the built timeline to a zoomed-in framing
    — the standard trick for hiding jump cuts in a single-camera setup.

`zoom_amount` — *default: 1.15*
:   Zoom factor for the punched-in clips. 1.1–1.2 is subtle; beyond 1.3 you
    will see the crop on 1080p footage.

## Captions

`captions` — *default: off*
:   Generate an SRT from the transcript, remapped onto the **cut** timeline
    so timings match the edit. Requires Whisper. The app tries to place it on
    a subtitle track; if your Resolve build refuses, it lands in the media
    pool for a manual drag. Thai text gets syllable-safe line breaking.

## Chapters

`chapters` — *default: off*
:   Add a purple marker at every segment preceded by a long removed pause,
    titled with the first words spoken there. Requires Whisper. Useful as
    YouTube chapter starting points.

`chapter_gap_sec` — *default: 2.0*
:   Minimum *removed* source time before a segment for it to count as a
    chapter boundary. This measures the pause you actually took while
    recording, not time on the finished timeline.

## Whisper

`model_size` — *default: `base`*
:   Which model to transcribe with. Accepts any
    [faster-whisper](https://github.com/SYSTRAN/faster-whisper) size name or
    Hugging Face CTranslate2 repo id (CPU), any `mlx:`-prefixed repo (Apple
    GPU), or one of the built-in aliases:

    | Alias | What it is | Engine |
    |---|---|---|
    | `base`, `medium`, … | vanilla Whisper sizes | CPU |
    | `large-v3-turbo-mlx` | fast multilingual | Apple GPU |
    | `thai-distill-mlx` | distilled [Thonburian](https://github.com/biodatlab/thonburian-whisper) Thai fine-tune | Apple GPU |
    | `thai-large-v3-mlx` | **full** Thonburian Thai model — best Thai accuracy, needs a one-time local conversion (below) | Apple GPU |
    | `thai-large-v3` | same weights on CPU, roughly 10× slower | CPU |

    Models download on first use and are cached in `~/.cache/huggingface`.
    Everything runs locally — no API keys.

`language` — *default: auto-detect*
:   ISO 639-1 code (`en`, `th`, `de`, …). Setting it explicitly skips
    detection and helps short or noisy clips.

### Converting the full Thai model

No published MLX build of the full Thonburian model exists, so it needs a
one-time local conversion:

```bash
curl -sLO https://raw.githubusercontent.com/ml-explore/mlx-examples/main/whisper/convert.py
uv run python convert.py --torch-name-or-path biodatlab/whisper-th-large-v3-combined \
    --mlx-path ~/.autocut/models/thai-large-v3-mlx --dtype float16
mv ~/.autocut/models/thai-large-v3-mlx/model.safetensors \
   ~/.autocut/models/thai-large-v3-mlx/weights.safetensors
```

(The converter writes `model.safetensors`; mlx-whisper loads
`weights.safetensors` — hence the rename.) If the conversion isn't present,
`thai-large-v3-mlx` falls back to the distilled model with a warning.

## Build

`build_mode` — *default: `stream`*
:   `stream` appends segments to the new timeline live inside Resolve.
    `fcpxml` writes the whole edit as an FCPXML file instead — **for
    Premiere Pro / Final Cut interchange**; the free Mac App Store Resolve
    imports FCPXML with offline media
    (see [How It Works](how-it-works.md#fcpxml-mode)).

## Tracks

`video_track`, `audio_track` — *default: 1*
:   Which timeline tracks to read. Only one video track is processed per
    run; clips on other tracks are ignored.

## Presets

Presets live at `~/.autocut/presets.json` — plain JSON, one object per
preset, keys matching the parameter names above. Built-in templates are
always listed; saving a user preset under a template's name overrides it,
and deleting that preset restores the template.

| Template | What it changes from the base defaults |
|---|---|
| Talking Head (J-cut) | J-cut 4f |
| Podcast / Interview | min silence 0.6 s, padding 8f, J-cut 8f, chapters on (3 s gap) |
| Tutorial (tight cut) | min silence 0.3 s, padding 4f, J-cut 4f, captions on |
| Social Clip (subtitles + zoom) | min silence 0.3 s, padding 4f, fillers removed, captions + punch-in on |
