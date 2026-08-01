# Project manifest

The terminal CLI owns this JSON document and rewrites it atomically after successful stages. Paths may be relative when first authored; loaded paths are normalized before the free-edition handoff job is created.

## Input fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Must be `1`. |
| `title` | Song title shown from time zero. |
| `author` | Opening credit; defaults to `José Troche`. |
| `audio` | Local song audio path. |
| `lyrics_source` | Reviewed lyric wording from UTF-8 text or an authenticated `.gdoc` pointer. Both remove square-bracket annotations, empty lines, and underscore-only separators. Google Docs use only the first/main tab and omit its tab name and first body/title line. |
| `visual_style` | Repeated visual anchor for scene-generation continuity. |
| `work_dir` | Generated images and overlays. |
| `video` | Resolution, frame rate, transition, typography, title/fade duration, lyric lead/fade timing, and zoom settings. |
| `image_generation` | Provider, model, quality, optional API size, Codex timeout, or command template. `codex` with medium quality is the default. |
| `render` | Resolve output path, format, video codec, audio codec, and replacement policy. Job staging relocates the output into the handoff root. |

## Generated fields

`duration` is probed from the audio. With automatic alignment, the complete track is transcribed without speech-oriented VAD; `lyrics` contains only reference lines confirmed by that transcription, preserving reviewed wording plus audio-derived `start`/`end` seconds and `alignment_confidence`. Unmatched reference lines are omitted rather than interpolated. `scenes` normally contains one contiguous visual range per confirmed lyric line, with image prompts, generated image paths, and alternating `zoom_in`/`zoom_out` motion. Exceptionally short lines are grouped only when needed to keep the cross-dissolve valid. `overlays` identifies the transparent PNGs used for title and lyrics.

The staged `resolve-job.json` embeds a copy of this manifest with sandbox-safe media paths and adds `resolve_job`, containing `project_name`, `timeline_name`, `replace_timeline`, and `render`. The original manifest remains the canonical editable project description.

`video-lyrics replan project.json` rebuilds scenes from the existing lyric cues without running
transcription again. When a grouped scene is split, one prior artwork is archived and reused while
the other line receives a new prompt for the next `video-lyrics images` run.

## Invariants

- Scene one starts at `0`; the final scene ends within 0.10 seconds of the audio duration.
- Scenes are ordered, contiguous, and non-overlapping in the manifest. Resolve creates transition overlaps internally.
- `video.transition` is less than half the shortest scene duration.
- Each end time is greater than its start time.
- Lyric cues are ordered and non-overlapping.
- Audio, scene images, title overlay, and all lyric overlays exist before job staging.
- The title begins at time zero and is capped at the audio duration.
- Resolve Free receives only paths copied beneath the configured Movies/Videos handoff directory.
- Every scene image is center-cropped and resized to the exact configured video dimensions before staging.

## Minimal pre-preparation example

```json
{
  "schema_version": 1,
  "title": "Song Title",
  "author": "José Troche",
  "audio": "/absolute/path/song.wav",
  "lyrics_source": "/absolute/path/lyrics.txt",
  "visual_style": "cinematic photographic realism",
  "work_dir": "work",
  "video": {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "transition": 0.75,
    "title_duration": 12.0,
    "title_fade": 0.75,
    "font": "Avenir Next Demi Bold",
    "font_size": 58,
    "margin_v": 72,
    "zoom": 1.08,
    "lyric_lead": 0.35,
    "lyric_fade": 0.2
  },
  "image_generation": {
    "provider": "codex",
    "model": "gpt-image-2",
    "quality": "medium",
    "codex_timeout": 900
  },
  "render": {
    "output": "output/song-title.mp4",
    "format": "mp4",
    "codec": "H264",
    "audio_codec": "aac",
    "replace_existing": true
  },
  "lyrics": [],
  "scenes": [],
  "overlays": {"title": "", "lyrics": []}
}
```
