# Video Lyrics Creator

Turns a song plus its lyrics into a finished lyric video: the words are timed from
the actual recording, each couplet gets its own generated image, the images drift
with a Ken Burns move and cross dissolve into one another, and ffmpeg assembles and
exports the result (DaVinci Resolve is available as an alternative render engine).

```
audio ─┐
       ├─ transcribe → align → tune → plan → images → overlays → bed → render → video.mp4
lyrics ┘
```

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
source .venv/bin/activate
```

`source .venv/bin/activate` puts `video-lyrics` on your PATH for the current shell
session — run it once per new terminal. Without it, call the script directly as
`.venv/bin/video-lyrics ...`.

Needs **ffmpeg** on the PATH and the **codex** CLI (image generation). That is
enough for the default render engine; **DaVinci Resolve 18+** (the free edition is
enough) is only needed if you choose `--engine resolve`.

### Rendering with ffmpeg (default)

`video-lyrics render` and `video-lyrics run` use ffmpeg unless told otherwise, so
nothing beyond the install above is required — the video is assembled and exported
straight from the CLI.

### Rendering with DaVinci Resolve (optional)

Pass `--engine resolve` (or `video-lyrics set render.engine resolve`) to assemble
and export through Resolve instead — useful if you want to open the timeline
afterwards and keep editing by hand. There are two routes, and the tool picks
whichever is open:

**Free edition — from inside Resolve.** The free build has no "External scripting
using" preference, so nothing outside Resolve can drive it. `video-lyrics render
--engine resolve` notices, prepares every frame of media, and installs a launcher
into Resolve's script menu. You finish with one click:

> **Workspace → Scripts → Video Lyrics Creator**

It builds the timeline and renders, showing progress in a small window (and in
`work/resolve-launcher.log`). Restart Resolve once after the first install so the
menu picks the script up.

**Studio, or free builds that do expose the preference — straight from the CLI.**
Set **Preferences → System → General → "External scripting using" → Local**, and
`video-lyrics render --engine resolve` does the whole thing without touching
Resolve's UI.

```bash
video-lyrics resolve-check                    # which route is open on this machine
video-lyrics resolve-install                  # (re)install the menu launcher
video-lyrics render --engine resolve --handoff  # always finish from the Resolve menu
```

### Google Docs lyrics (optional)

Copy `.env.example` to `.env`, paste a Google Cloud **Desktop app** OAuth client id
and secret, then log in once:

```bash
video-lyrics google-auth      # writes GOOGLE_DRIVE_REFRESH_TOKEN back into .env
```

A Drive-for-desktop `.gdoc` file then works anywhere a `.txt` file does.

## Use

```bash
video-lyrics init \
  --audio ~/Music/album/"08 my-song.wav" \
  --lyrics ~/"Google Drive"/Songs/my-song.gdoc \
  --title "My Song"

video-lyrics run                 # everything, end to end
video-lyrics status              # what is done so far
video-lyrics cues                # the timed lyrics
video-lyrics tune                # listen, and fix any line that sits wrong
```

Right after aligning, `video-lyrics run` pauses to ask whether you want to
fine-tune lyric timing by ear (`video-lyrics tune`) before it moves on to
planning scenes; answering no just continues to `plan`. `--skip-tune` skips
straight past the question, for scripted or unattended runs.

Every stage is also its own command, so you can iterate on one part without
redoing the rest:

```bash
video-lyrics lyrics              # load the reference lyrics, measure the audio
video-lyrics transcribe          # cached in work/<song>/transcript.json
video-lyrics align               # re-time after changing alignment settings
video-lyrics tune                # hear the song and adjust the timing by hand
video-lyrics plan                # regroup lines into images
video-lyrics images --jobs 3     # generate the stills with codex
video-lyrics overlays            # title card, lyric PNGs, lyrics.srt
video-lyrics bed                 # bake Ken Burns motion + cross dissolves
video-lyrics render              # assemble with ffmpeg and export
```

| flag | meaning |
| --- | --- |
| `--force` | redo a stage even though its output is cached |
| `--skip-tune` | (`run`) don't ask about fine-tuning lyric timing after align |
| `--engine resolve` | render through DaVinci Resolve instead of ffmpeg |
| `--handoff` | (Resolve) prepare everything and finish from Resolve's Scripts menu |
| `--launch` | (Resolve) start Resolve first and wait until it answers |
| `--images-dir DIR` | use your own images instead of generating them |
| `--limit N` | (`images`) generate only N of the missing images, then stop |
| `--from` / `--to` | run part of the pipeline (`run --from plan --to bed`) |

### Generating images without codex

`image_generation.provider` controls how `video-lyrics images` fills in each scene:

* `codex` (default) — calls the Codex CLI's image_gen tool automatically.
* `manual` — no generator at all. It writes every scene's prompt, and the exact
  filename stem it expects, to `work/<song>/images/prompts.txt`. Paste each prompt
  into whatever you use by hand (ChatGPT, Midjourney, ...), save the result under
  that stem in `work/<song>/images/` — png, jpg, or webp, whichever the tool gives
  you — then run `video-lyrics images` again; it picks up the files you made
  (converting them to PNG) and reports anything still missing.
* `meta` — drives [meta.ai](https://www.meta.ai/) in a real, visible browser
  (Playwright) and downloads the result automatically, one prompt at a time.
  Unofficial: there is no API for this, it depends on meta.ai's current page and
  can break if that changes, and running it is your own call to make against
  meta.ai's terms of service. Needs the extra installed once:
  `pip install -e ".[meta]"` and `playwright install chromium`. The first run
  opens a browser and waits for you to log in by hand (nothing is automated
  about that); the session is then remembered in `image_generation.meta_profile_dir`
  (default `~/.video-lyrics/meta-ai-profile`) so later runs do not ask again. The
  next prompt is only submitted once the current scene's image has actually
  finished generating and been written to disk, plus a random delay between
  `image_generation.meta_min_delay` and `meta_max_delay` (1-4s by default) on top
  of that, so requests do not land in an obvious, throttle-inviting pattern.
  ("Finished" is not as obvious as it sounds: while it is still working, meta.ai
  already shows a preview at the final resolution, so this waits for the finished
  image's own Download control to appear rather than for pixels to exist.) Use
  `--limit N` to try a handful before committing to a whole song. Raw downloads
  are kept in
  `work/<song>/images/images.src/`, whatever format meta.ai served, and each is
  also converted into the canonical PNG in `work/<song>/images/` itself; a run
  interrupted partway through only asks the browser for what is still missing.
  If meta.ai's page changes and the composer or the generated image can no longer
  be found, override `image_generation.meta_composer_selector` /
  `meta_image_selector` with a CSS selector for the right element (defaults:
  `textarea:visible, [contenteditable='true']:visible` and `main img`).
* `supplied` — same idea, but for images you already have; see `--images-dir`
  above, they're adopted in filename order instead of matched by name.

```bash
video-lyrics set image_generation.provider manual
video-lyrics images
# ... create work/<song>/images/scene-001-xxxx.png etc by hand ...
video-lyrics images
```

## The project file

`project.yaml` is a **pointer**, not the project — it holds just enough to find the
rest: the title, and where the work directory is. Everything else — every setting
and every stage's results — lives in `work/<song>/project.yaml`, right next to that
song's own images, transcript, and clips. `<song>` is the title, lower-cased and
hyphenated (`Immeasurable Grace` → `immeasurable-grace`).

That split is what keeps songs from colliding: starting a new song with
`video-lyrics init` repoints `project.yaml` at it, but the previous song's folder
under `work/` — and everything in it — is left exactly as it was. To go back to an
earlier song, either point `-p` at its folder directly, or edit `project.yaml`'s
`title:` back to match it — its work is still there, so the pipeline picks up
where it left off rather than starting over:

```bash
video-lyrics -p work/an-older-song/project.yaml status
```

Edit settings by hand in either file, or from the CLI — this always edits the
current song's data file, never the pointer:

```bash
video-lyrics set video.zoom 1.3
video-lyrics set video.font "Optima Bold"
video-lyrics set alignment.min_confidence 0.4
video-lyrics set image_generation.lines_per_image 1
```

JSON works just as well — the suffix decides the format:

```bash
video-lyrics init --format json ...    # start out as project.json
video-lyrics convert --to yaml         # export the current song as one merged file
video-lyrics -p songs/grace.yaml run   # any path, any of the two formats
```

Commands with no `-p` pick up `project.yaml`, then `project.yml`, then
`project.json` from the current directory. A project file from before this split
existed is migrated automatically the first time it is opened: its flat `work/`
directory is moved into `work/<song>/` for you, logged as it happens.

## How the timing works

The rule is that **the audio decides and the lyrics file spells**:

* the recording is transcribed with word-level timestamps (faster-whisper);
* reference lines are matched to that transcript with a monotonic diff, so a chorus
  that repeats stays in the order it was actually sung;
* a line that is heard becomes a cue, timed from its words but displayed with your
  wording — spelling, punctuation and capitalisation come from your file;
* a line the audio never confirms (a heading, a scripture reference, an early draft,
  a verse that was cut) produces **no cue at all**.

A second pass revisits any stretch of audio no cue claimed and offers it to the
lines that are still unused, picking the best fit. That matters for real lyric
documents, which often carry drafts and prose above the final words: without it a
half-matching draft line can swallow the audio that belonged to the real one.

`alignment.min_confidence` is the share of a line's words that must be heard before
it counts, `alignment.min_matched_words` guards against one-word coincidences, and
`video-lyrics align` prints every line it could not confirm.

Two transcription settings are deliberately off, because on sung, fully mixed audio
they each destroy the transcript: `alignment.vad` (voice-activity filtering drops
most of a vocal) and `alignment.prompt_hint` (priming Whisper with the lyrics makes
it recite them over the intro).

## Fixing the timing by hand

The aligner gets most lines right and some of them slightly wrong — and a line that
lands a third of a second late is obvious the moment you hear it and invisible in a
column of numbers. So there is an editor for exactly that:

```bash
video-lyrics tune
```

It draws the song's waveform with the lyric lines laid over it, plays any part of it
on demand, and writes the adjusted `start`/`end` back into the project file. The loop
it is built around is: pick a line, press <kbd>⏎</kbd> to hear it, nudge an edge,
hear it again. <kbd>space</kbd> plays straight through from wherever the playhead
is, and as it runs each line highlights - in the waveform and in the list - the
moment it starts and clears the moment it ends, so a whole pass through the song
makes a line that comes in early or lingers too long obvious without touching
another key.

| | |
| --- | --- |
| <kbd>space</kbd> | play / pause, following whichever line is sounding |
| <kbd>←</kbd> <kbd>→</kbd> | seek 0.1s (<kbd>⇧</kbd> for 1s) |
| <kbd>⏎</kbd> | play just the selected line, with a run-up |
| <kbd>\\</kbd> | play just the edge being edited |
| <kbd>↑</kbd> <kbd>↓</kbd> | pick a line |
| <kbd>tab</kbd> | edit its start, its end, or the whole line |
| <kbd>,</kbd> <kbd>.</kbd> | nudge by one step (<kbd>&lt;</kbd> <kbd>&gt;</kbd> by five), <kbd>-</kbd> <kbd>=</kbd> resize the step |
| <kbd>[</kbd> <kbd>]</kbd> | set start / end to where the playhead is |
| <kbd>a</kbd> | add a line — one the audio never confirmed, or type one fresh |
| <kbd>e</kbd> <kbd>d</kbd> | edit a line's text / remove it |
| <kbd>u</kbd> <kbd>y</kbd> | undo / redo — <kbd>w</kbd> save, <kbd>?</kbd> all the keys |

Editing a line never moves its neighbours — it only ever stops at them, so two lines
can never end up overlapping. Lines that sit end to end — which is most of them,
since the aligner closes small gaps — can instead be dragged together as one shared
boundary by turning on <kbd>l</kbd> first, for the rarer case where both really need
to shift together.

An adjusted line is marked `tuned` in the project file and `*` in `video-lyrics
cues`. `video-lyrics align` will not overwrite tuned cues, so a later
`video-lyrics run` cannot quietly undo the work; `align --force` re-times everything
from the transcript again. After tuning, rebuild from the plan onwards:

```bash
video-lyrics run --from plan
```

Needs **ffplay** to hear anything, which comes with ffmpeg.

## What ends up on the timeline

```
V3  Title      one clip: song title and author, faded out before the first lyric
V2  Lyrics     one clip per confirmed line, fading in and out
V1  Images     scene / dissolve / scene / dissolve ... covering the whole song
A1  Music      the song, faded in and out, from frame 0
```

The video is exactly as long as the audio — no silence at either end, and the
audio fade does not trim it either: `video.audio_fade` (default 1.0s) is a gain
ramp baked into a copy of the song, not a cut.

ffmpeg has no keyframe-based motion or transition primitives to hand off to, and
Resolve's scripting API can neither add a transition nor keyframe a clip's gain
any more than it can add a transition, so all three - the motion, the overlay
fades, and now the audio fade - work the same way: baked into the media up
front, so assembly is just laying finished clips end to end.

* `work/clips/` — the image bed. Each scene is rendered with its Ken Burns move -
  `zoom_in` or `zoom_out` only; a pan crops the same margin off the top and bottom
  for its whole span rather than relaxing to the full frame the way a zoom does at
  one end, so it can permanently cut off a subject sitting near the top or bottom
  of the image, and is left out of the automatic rotation for that reason. Every
  scene boundary gets a real cross-dissolve clip whose two halves continue the
  neighbouring moves, so the clips can sit end to end on one track and still
  dissolve. `video.zoom` scales with how long a scene is actually on screen, so a
  quick cut and a long instrumental hold both move at a similar, steady rate
  rather than one crawling and the other racing.

  `video-lyrics plan` decides how many lines share each image by how long they
  take to sing, not a fixed count: short lines pair up, a line long enough to
  carry an image alone gets one, and either way an image never sits on screen for
  under `alignment.min_scene_duration` (4s) or over `alignment.max_scene_duration`
  (15s). A transition only ever falls between lines, never inside one, and never
  between two lines from different sections either - the last line of a verse and
  the first line of the chorus that follows it never share an image. A section
  break is either an explicit `[Verse]` / `[Chorus]` / ... marker in the lyrics
  source, or - since not every verse is labelled - a blank line, the way a stanza
  is always set off from the one after it. A long instrumental stretch with no
  lyrics in it becomes a few evenly-sized images instead of one held far too long.
  Every prompt also asks for generous margin around the main subject, since the
  zoom will crop in on it, and - for a scene whose lines (or, for an instrumental
  break, the title or surrounding lines) mention God, Jesus, or Christ - an extra
  instruction to keep any divine figure's face blurred, veiled, or turned away
  rather than sharply detailed.
* `work/overlay-clips/` — the lyric and title clips as QuickTime Animation movies
  with an alpha channel and their fades already in the pixels.

By default ffmpeg then does the edit, the audio, and the export, straight from the
CLI. `--engine resolve` does the same assembly inside Resolve instead — either
driven directly from the CLI, or by the launcher script running inside Resolve,
which builds the very same timeline through the same code path. `work/lyrics.srt`
is written too, so the lyrics can also be loaded as a subtitle track
(`video-lyrics set render.lyrics_mode subtitle`) or uploaded to YouTube.

## Work directory

Every song gets its own folder, `work/<song>/`, holding that song's data file and
all of its intermediate files:

```
work/<song>/project.yaml        the full project - settings and every stage's results
work/<song>/transcript.json     cached transcription
work/<song>/lyrics.txt          the reference lines as loaded
work/<song>/lyrics.srt          timed lyrics
work/<song>/audio-faded.wav     the song with its fade in/out baked in
work/<song>/images/             one still per scene
work/<song>/overlays/           title and lyric PNGs (transparent)
work/<song>/overlay-clips/      the same, as alpha movie clips with fades
work/<song>/clips/              the image bed: scene and dissolve clips
```

Every artefact is named after a hash of its inputs, so re-running a stage only
redoes what actually changed.

## Tests

```bash
.venv/bin/python -m pytest                  # units + a full ffmpeg render
.venv/bin/python -m pytest -m "not slow"    # units only
```
