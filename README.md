# Video Lyrics Creator

Turns a song and its lyrics into a finished lyric video. The words are timed from
the actual recording, every lyric line gets its own generated image, the images
drift with a Ken Burns move and cross dissolve into one another, and ffmpeg
assembles and exports the result.

```
audio ─┐
       ├─ lyrics → transcribe → align → tune → plan → images → overlays → bed → render → mp4
lyrics ┘
```

Every stage reads and writes the project file, so any stage can be re-run on its
own and the ones after it pick up the change.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,browser]"
.venv/bin/playwright install chromium
source .venv/bin/activate          # puts `video-lyrics` on PATH, once per shell
```

Also needs **ffmpeg** (and its `ffplay`, for the tuning editor) on the PATH. The
`browser` extra is only needed for the `chatgpt` / `meta` image providers, and
**DaVinci Resolve 18+** only if you choose `--engine resolve`. Two more extras are
worth having for the timing — `align` for [forced
alignment](#forced-alignment-optional) and `vocals` to isolate the singer first —
but both pull in torch, so they are opt-in.

Lyrics in a Google Doc need a one-time login: copy `.env.example` to `.env`, paste
a Google Cloud **Desktop app** OAuth client id and secret, then run
`video-lyrics google-auth` (it writes `GOOGLE_DRIVE_REFRESH_TOKEN` back into
`.env`). A Drive-for-desktop `.gdoc` file then works anywhere a `.txt` does.

## Quick start

```bash
video-lyrics init \
  --audio ~/Music/album/"08 my-song.wav" \
  --lyrics ~/"Google Drive"/Songs/my-song.gdoc \
  --title "My Song" \
  --context "a song after the crossing of the Red Sea in Exodus"

video-lyrics run       # everything, end to end
video-lyrics status    # what is done so far
video-lyrics cues      # the timed lyrics
```

`--context` says, in your own words, what the whole song is about. Each image
prompt is generated on its own, from one or two lyric lines that rarely name the
story they belong to, so without it twenty scenes can end up in twenty different
worlds. With it, every prompt opens with the same frame:

> *Context for the whole video: a song after the crossing of the Red Sea in
> Exodus. Every scene belongs to that same story, setting and world - keep them
> consistent with it.*

Change it later with `video-lyrics set context "..."` then `video-lyrics plan`.
That rewrites every prompt, and a changed prompt means a new image, so run
`images` afterwards. Leaving it empty says nothing at all and leaves existing
prompts byte-for-byte as they were — which is what keeps already-finished songs
from asking to be redrawn.

## Commands

Each stage is also its own command, so you can iterate on one part without
redoing the rest:

```bash
video-lyrics lyrics        # load the reference lyrics, measure the audio
video-lyrics transcribe    # time the words; cached in work/<song>/transcript.json
video-lyrics align         # confirm lines against the transcript and time them
video-lyrics tune          # hear the song and fix any line that sits wrong
video-lyrics plan          # group cues into image scenes and write their prompts
video-lyrics images        # generate the stills (a browser window opens)
video-lyrics overlays      # title card, lyric PNGs, lyrics.srt
video-lyrics bed           # bake Ken Burns motion + cross dissolves
video-lyrics render        # assemble and export
```

| flag | meaning |
| --- | --- |
| `--force` | redo a stage even though its output is cached |
| `--from` / `--to` | (`run`) run part of the pipeline: `run --from plan --to bed` |
| `--tune` | (`run`) pause after align to ask about fine-tuning the timing |
| `--lines-per-image N` | (`init`, `plan`, `run`) 1 for an image per line (default), 2 to pair them up |
| `--images-dir DIR` | use your own images instead of generating them |
| `--limit N` | (`images`) generate only N of the missing images, then stop |
| `--engine resolve` | render through DaVinci Resolve instead of ffmpeg |
| `--handoff` | (Resolve) prepare everything, finish from Resolve's Scripts menu |
| `--launch` | (Resolve) start Resolve first and wait until it answers |

Plus the utilities: `status`, `cues [--json]`, `set KEY VALUE`,
`convert --to yaml|json`, `google-auth`, `browser-login`, and the Resolve helpers
`resolve-check`, `resolve-install`, `resolve-uninstall`, `resolve-formats`.
Global flags: `-p/--project PATH`, `-v/--verbose`, `--env PATH`.

`video-lyrics init` also takes `--author`, `--style`, `--work-dir`, `--output`,
`--width`, `--height`, `--fps`, `--font`, `--font-size`, `--whisper-model`,
`--engine`, `--format yaml|json` and `--force`.

**How many lines share an image.** One by default — every lyric line gets its own
picture. Ask for `2` and `plan` bundles consecutive lines up to that many, still
splitting a pair that would hold one picture past `alignment.max_scene_duration`
and never joining across a section break. So `2` means *up to* two: half the
pictures, half the generating, half the visual variety.

```bash
video-lyrics plan --lines-per-image 2                  # regroup an existing song
video-lyrics set image_generation.lines_per_image 2    # same, without replanning
```

Passing the flag saves it, so later runs keep grouping the same way. Regrouping
changes each scene's prompt, so run `plan` then `images`.

## Where the images come from

`image_generation.provider` decides how `video-lyrics images` fills in each scene:

* `chatgpt` (default) — drives [chatgpt.com](https://chatgpt.com/) in a real,
  visible browser and downloads each picture.
* `meta` — the same, against [meta.ai](https://www.meta.ai/).
* `manual` — no generator. Every outstanding scene's prompt and the exact filename
  stem it expects are written to `work/<song>/images/prompts.txt`. Create each
  image however you like, save it under that stem in `work/<song>/images/` (png,
  jpg or webp), and run `images` again — it picks the files up, converts them to
  PNG, and reports anything still missing.
* `supplied` — for images you already have; see `--images-dir`. They are adopted
  in filename order rather than matched by name.

```bash
video-lyrics set image_generation.provider meta
video-lyrics images --limit 3    # try a few before committing to a whole song
```

Raw downloads are kept in `work/<song>/images.src/` in whatever format the site
served; each is also converted into the canonical PNG in `work/<song>/images/`.
An interrupted run only asks for what is still missing, and a scene that already
has a usable image is never redrawn without `--force`.

**Regenerating one scene.** `--force` redoes every scene, so to redraw a single
one delete its two cached files — same stem, one in each folder — and run `images`
again:

```bash
rm work/<song>/images/scene-010-*.png work/<song>/images.src/scene-010-*.png
video-lyrics images
```

Editing that scene's `prompt:` in the project file works too and needs no delete:
a changed prompt hashes to a new stem, so the old files are simply orphaned.

### The browser providers, in more detail

Neither `chatgpt` nor `meta` is an API: they type into a consumer web page and
read the answer back off it. They depend on that page's current markup and can
break when it changes, and running them is your own call to make against the
site's terms of service — your own account, your own project.

**Login is never automated.** Sign in once, by hand:

```bash
video-lyrics browser-login                    # chatgpt.com, in Google Chrome
video-lyrics browser-login --provider meta    # meta.ai
```

That opens an ordinary browser window — not one the tool is driving — on the
profile the provider will use later (`~/.video-lyrics/chatgpt-profile`,
`~/.video-lyrics/meta-ai-profile`, or wherever `<provider>_profile_dir` points).
Log in, come back to the terminal and press Enter: the command closes the browser
itself and verifies the session was saved, because a browser only writes one out
when it shuts down cleanly. (Which is also why a run that force-quits its browser
can lose the login — let it close itself, or Ctrl-C once and wait.)

Why a separate command: signing in through Google inside an automated browser
trips *"This browser or app may not be secure"*, a check on the browser that no
amount of retrying gets past. A window nothing is driving isn't subject to it.

A profile belongs to the browser that created it, and to how that browser locks
it — Chrome encrypts its cookie database with a key from the OS keychain, and two
browsers that disagree about the key read each other's session as no session at
all. So both commands derive it from one place, and both read the same
`<provider>_channel` setting: `"chrome"` (the installed Google Chrome, the default
for ChatGPT because Google is markedly happier with a browser it recognises),
`"msedge"`, or `null` for Playwright's bundled browser (the default for meta.ai).
On the command line, `--channel bundled` means `null`.

Leave `<provider>_headless` off: signed in or not, chatgpt.com serves a headless
browser a Cloudflare challenge instead of the page.

One prompt at a time. The next is submitted only once the current image has
finished and been written to disk, plus a random `<provider>_min_delay` to
`<provider>_max_delay` (1–4s) on top, so requests don't land in an obvious,
throttle-inviting pattern. "Finished" is the hard part: both sites show something
that looks like the answer well before it is one, so the meta driver waits for the
finished image's own Download control and the ChatGPT driver waits for the Stop
button to go away. ChatGPT also gets a fresh chat per scene — asked for a second
picture in a conversation that already has one, it edits the first instead.

**When a scene doesn't come back**, the driver reads what the page actually said
rather than just timing out, because the two causes want opposite answers:

* *It won't draw this prompt* — a refusal or content-policy line. The prompt is
  the only thing worth changing, so the scene is asked again with a slightly
  reworded prompt (up to three wordings, each keeping the scene's own description
  and only asking for it less literally). If none land, that scene is skipped and
  the song carries on; rewrite its `prompt:` and run `images` again.
* *The site is busy* — capacity, a rate limit, a quota, a download that failed. No
  wording helps, so the scene is left alone, the run waits longer before the next
  one, and it stops after three in a row rather than grinding through the rest.

Either way the run keeps going and reports what it left behind, and `images` then
asks only for the scenes still missing — retrying later is just running the same
command again. A failure with *nothing* on the page to explain it usually means
markup that has moved on: one is survived, a second in a row stops the run.

If a site's markup does move on, override
`image_generation.<provider>_composer_selector` / `<provider>_image_selector` with
a CSS selector for the right element. The defaults live in
[`chatgpt.py`](video_lyrics/chatgpt.py) and [`meta_ai.py`](video_lyrics/meta_ai.py),
next to the ready/busy selectors that decide when an image is done — everything
site-specific is in those two files, and the driver they share is
[`browser_ai.py`](video_lyrics/browser_ai.py).

> The old `codex` provider (the Codex CLI's `image_gen` tool) was replaced by
> `chatgpt`: same account, one fewer CLI to install and keep logged in. A project
> file that still says `codex` runs as `chatgpt` and keeps every image it already
> has; make it permanent with `video-lyrics set image_generation.provider chatgpt`.

## How the timing works

There are two ways to work out when a word is sung, and the project can use either.
The default **transcribes** the song and matches your lines to what it heard. The
alternative is **forced alignment**, which skips the guessing: it already has the
words, and only has to find them. See [Forced alignment](#forced-alignment-optional)
below — measured against the same song, it is roughly three times closer on where a
line begins.

### Transcribing (`alignment.engine = whisper`, the default)

The rule is that **the audio decides and the lyrics file spells**:

* the recording is transcribed with word-level timestamps (faster-whisper);
* reference lines are matched to that transcript with a monotonic diff, so a
  chorus that repeats stays in the order it was actually sung;
* a line that is heard becomes a cue, timed from its words but displayed with your
  wording — spelling, punctuation and capitalisation come from your file;
* a line the audio never confirms (a heading, a scripture reference, an early
  draft, a verse that was cut) produces **no cue at all**.

A second pass revisits any stretch of audio no cue claimed and offers it to the
lines still unused, picking the best fit. That matters for real lyric documents,
which often carry drafts and prose above the final words: without it a
half-matching draft line can swallow the audio that belonged to the real one.

`alignment.min_confidence` is the share of a line's words that must be heard
before it counts, `alignment.min_matched_words` guards against one-word
coincidences, and `video-lyrics align` prints every line it could not confirm.

Two transcription settings are deliberately off, because on sung, fully mixed
audio they each destroy the transcript: `alignment.vad` (voice-activity filtering
drops most of a vocal) and `alignment.prompt_hint` (priming Whisper with the
lyrics makes it recite them over the intro).

### Held notes

A word is over, as far as a transcript is concerned, the moment its last consonant
is — so a line sung out on a long vowel leaves the screen while the note is still
ringing. The loudness envelope knows better, and after the cues are built each one
walks on from its end for as long as the sound stays near the level that line
itself sat at, stopping the moment it falls away. A line that really did stop where
the transcript says drops below that threshold at once and is left alone.

`alignment.tail_extend` is the most a line may be held (1.2s; `0` turns it off) and
`alignment.tail_level` how loud the sound must stay to keep holding it, measured
against the line itself. It reads the isolated vocal when there is one — over a
full mix the band keeps the level up on its own, and most lines simply run to the
cap.

### Forced alignment (optional)

Transcription asks the recording what was sung and then has to be argued with. But
the words were never in doubt — they are sitting in the lyrics file. Forced
alignment asks only the question that is actually ours: *given* that this is what
was sung, when was each word? A CTC acoustic model scores every 20ms frame against
every letter, and a Viterbi pass walks the lyrics through those scores along the one
best path that spells them in order.

```bash
pip install -e ".[align,vocals]"
video-lyrics set alignment.engine forced
video-lyrics set alignment.vocals true      # strongly recommended - see below
video-lyrics transcribe --force && video-lyrics align --force
```

On one 4:45 song, scored against where the vocal actually starts after each silence:

| | lines placed | median error | starts |
| --- | --- | --- | --- |
| transcript (whisper) | 33 of 34 | 1.02s | ~1.0s early |
| forced, over the mix | half unusable | — | one line landed in the wrong verse |
| forced, over the vocal | 33 of 34 | **0.28s** | 0.27s late |

That last row is the point, and so is the middle one: **isolate the vocal.** The
acoustic model is listening for consonants and a band plays straight over them.
`alignment.vocals true` runs [demucs](https://github.com/adefossez/demucs) once (a
minute or so) and caches the stem in the work directory; everything that *listens* —
both engines, and the held-note pass — then uses the singer alone. The render never
does: the video always carries the real mix.

Forced alignment is *forced*: hand it a heading or a discarded draft and it will
place that too, somewhere, because it is not allowed to refuse. What it cannot do is
make the audio agree, so those words come back with poor scores.
`alignment.forced_min_score` (0.05) drops them, and a line that loses its words that
way falls below `min_confidence` and produces no cue — the same rule as before,
arrived at from the other direction. The second, rescuing pass is skipped for this
engine: with the words already in the lyrics' own order there is nothing left for it
to fix, and reaching across the song for an unmatched line only pins it on a distant
repeat of the same words.

`alignment.forced_model` chooses the acoustic model (default
`facebook/wav2vec2-base-960h`, English; any CTC model on Hugging Face works — a
multilingual one such as `facebook/mms-300m-1130-forced-aligner` for other
languages). Both weights and stem are cached, so only the first run pays for them.

To go back, at any point: `video-lyrics set alignment.engine whisper`.

### Fixing the timing by hand

The aligner gets most lines right and some slightly wrong — and a line that lands
a third of a second late is obvious the moment you hear it and invisible in a
column of numbers. So there is an editor for exactly that:

```bash
video-lyrics tune
```

It draws the waveform with the lyric lines laid over it, plays any part on demand,
and writes the adjusted `start`/`end` back into the project file. The loop it is
built around: pick a line, press <kbd>⏎</kbd> to hear it, nudge an edge, hear it
again. <kbd>space</kbd> plays straight through, highlighting each line the moment
it starts and clearing it the moment it ends, so one pass through the song makes a
line that comes in early or lingers too long obvious without touching another key.

| | |
| --- | --- |
| <kbd>space</kbd> | play / pause, following whichever line is sounding |
| <kbd>←</kbd> <kbd>→</kbd> | seek 0.1s (<kbd>⇧</kbd> for 1s) |
| <kbd>⏎</kbd> | play just the selected line, with a run-up |
| <kbd>\\</kbd> | play just the edge being edited |
| <kbd>g</kbd> <kbd>f</kbd> | playhead to the line's start / follow the song |
| <kbd>↑</kbd> <kbd>↓</kbd> | pick a line |
| <kbd>tab</kbd> | edit its start, its end, or the whole line |
| <kbd>,</kbd> <kbd>.</kbd> | nudge by one step (<kbd>&lt;</kbd> <kbd>&gt;</kbd> by five), <kbd>-</kbd> <kbd>=</kbd> resize the step |
| <kbd>[</kbd> <kbd>]</kbd> | set start / end to where the playhead is |
| <kbd>a</kbd> | add a line — one the audio never confirmed, or type one fresh |
| <kbd>e</kbd> <kbd>d</kbd> | edit a line's text / remove it |
| <kbd>z</kbd> <kbd>Z</kbd> | zoom the waveform out / in |
| <kbd>u</kbd> <kbd>y</kbd> | undo / redo — <kbd>w</kbd> save, <kbd>?</kbd> all the keys |

Editing a line never moves its neighbours — it only ever stops at them, so two
lines can never overlap. Lines that sit end to end (most of them, since the
aligner closes small gaps) can instead be dragged as one shared boundary by
turning on <kbd>l</kbd> first.

An adjusted line is marked `tuned` in the project file and `*` in `video-lyrics
cues`. `align` will not overwrite tuned cues, so a later `run` cannot quietly undo
the work; `align --force` re-times everything from the transcript again. After
tuning, rebuild with `video-lyrics run --from plan`.

## What ends up on the timeline

```
V3  Title      one clip: song title and author, faded out before the first lyric
V2  Lyrics     one clip per confirmed line, fading in and out
V1  Images     scene / dissolve / scene / dissolve ... covering the whole song
A1  Music      the song, faded in and out, from frame 0
```

The video is exactly as long as the audio. The audio fade doesn't trim it either:
`video.audio_fade` (1.0s) is a gain ramp baked into a copy of the song, not a cut.

ffmpeg has no keyframe-based motion or transition primitives to hand off to, and
Resolve's scripting API can neither add a transition nor keyframe a clip's gain,
so all three — the motion, the overlay fades, the audio fade — work the same way:
baked into the media up front, so assembly is just laying finished clips end to
end.

**`work/<song>/clips/` — the image bed.** Each scene is rendered with its Ken
Burns move, alternating `zoom_in` and `zoom_out` (a pan crops the same margin off
the top and bottom for its whole span rather than relaxing to the full frame the
way a zoom does at one end, so it can permanently cut off a subject near an edge —
which is why pans are out of the rotation). Every scene boundary gets a real
cross-dissolve clip whose two halves continue the neighbouring moves, so the clips
sit end to end on one track and still dissolve. `video.zoom` scales with how long
a scene is actually on screen, so a quick cut and a long instrumental hold move at
a similar, steady rate.

Two clips bake at a time, and each one is written to a `.part` file that is moved
into place only once ffmpeg exits cleanly — so an interrupted bake leaves finished
clips and nothing half-written, and re-running picks up where it stopped. Two is
worth about 279s → 229s on a 144s song; four measured no faster and needs 2.5GB of
memory instead of 1.4GB. If the concurrency ever causes trouble, `default_jobs` in
`video_lyrics/motion.py` turns it off by returning 1, and the comment beside it
says how to strip it out entirely. The `.part` staging is worth keeping either
way — it is what stops a failed render leaving a partial clip that the next run
mistakes for a finished one.

**How `plan` cuts the song into scenes.** Consecutive lines are bundled up to
`image_generation.lines_per_image` (1 by default). A transition only ever falls
between lines, never inside one, and never between two lines from different
sections either — the last line of a verse and the first line of the chorus after
it never share an image. A section break is either an explicit marker in the
lyrics source (`[Chorus]`, `Verse 2:`, `Bridge`, ...) or, since not every verse is
labelled, a blank line. A gap wider than `alignment.scene_gap` also starts a new
image, and a long instrumental stretch becomes a few evenly-sized images rather
than one held far too long. `alignment.min_scene_duration` (4s) and
`max_scene_duration` (15s) are what those decisions aim at: a pair that would run
past the maximum splits back into two, a stretch longer than it is divided evenly,
and a short line before an instrumental break keeps enough of the break's front to
reach the minimum on its own.

Every prompt asks for generous margin around the main subject, since the zoom will
crop in on it, and carries the same note of reverence: *if God or Jesus is
portrayed in the image, keep his face naturally concealed, blurred, or turned away
— never a sharp, detailed face*. It goes on every scene, not only the ones whose
lines name him, because two lines like "he carried it all" can send a picture
toward a divine figure without naming one. Each scene is also given its own
**framing** (wide establishing shot, low angle, head-on symmetry, ...) cycled by
its position — without it a chorus that comes round again is a word-for-word
identical prompt, and a generator handed the same prompt twice quite reasonably
returns the same picture twice. The cycle is deterministic, so re-planning still
lines up with images already generated.

**`work/<song>/overlay-clips/`** holds the lyric and title clips as QuickTime
Animation movies with an alpha channel and their fades already in the pixels.

`work/<song>/lyrics.srt` is written too, so the lyrics can be uploaded to YouTube
as a subtitle track — or, with the Resolve engine, used instead of the burnt-in
overlays (`video-lyrics set render.lyrics_mode subtitle`).

### Rendering with DaVinci Resolve (optional)

Pass `--engine resolve` (or `video-lyrics set render.engine resolve`) to assemble
and export through Resolve instead — useful if you want to open the timeline
afterwards and keep editing by hand. There are two routes, and the tool picks
whichever is open:

**Free edition — from inside Resolve.** The free build has no "External scripting
using" preference, so nothing outside Resolve can drive it. `render --engine
resolve` notices, prepares every frame of media, and installs a launcher into
Resolve's script menu. You finish with one click:

> **Workspace → Scripts → Video Lyrics Creator**

It builds the timeline and renders, showing progress in a small window (and in
`work/<song>/resolve-launcher.log`). Restart Resolve once after the first install so the
menu picks the script up.

**Studio, or free builds that do expose the preference — straight from the CLI.**
Set **Preferences → System → General → "External scripting using" → Local**, and
`render --engine resolve` does the whole thing without touching Resolve's UI.

```bash
video-lyrics resolve-check                      # which route is open here
video-lyrics resolve-install                    # (re)install the menu launcher
video-lyrics render --engine resolve --handoff  # always finish from the menu
```

## The project file

`project.yaml` at the repo root is a **pointer**, not the project: it holds the
title and where the work directory is. Everything else — every setting and every
stage's results — lives in `work/<song>/project.yaml`, next to that song's own
images, transcript and clips. `<song>` is the title, lower-cased and hyphenated
(`Immeasurable Grace` → `immeasurable-grace`).

That split is what keeps songs from colliding: `video-lyrics init` repoints
`project.yaml` at a new song, but the previous song's folder under `work/` is left
exactly as it was. To go back to it, point `-p` at its folder directly (or edit
the pointer's `title:` back) — its work is still there, so the pipeline picks up
where it left off:

```bash
video-lyrics -p work/an-older-song/project.yaml status
```

Edit settings by hand in either file, or from the CLI — `set` always writes the
current song's data file, never the pointer:

```bash
video-lyrics set video.zoom 1.3
video-lyrics set video.font "Optima Bold"
video-lyrics set context "a song after the crossing of the Red Sea in Exodus"
video-lyrics set alignment.min_confidence 0.4
```

The defaults for every settings group — `video`, `image_generation`, `alignment`,
`render` — are listed with a comment each in
[`config.py`](video_lyrics/config.py).

JSON works just as well; the suffix decides the format:

```bash
video-lyrics init --format json ...    # start out as project.json
video-lyrics convert --to yaml         # export the current song as one merged file
video-lyrics -p songs/grace.yaml run   # any path, either format
```

Commands with no `-p` pick up `project.yaml`, then `project.yml`, then
`project.json` from the current directory. A project file from before the split
existed is migrated automatically the first time it is opened: its flat `work/`
directory is moved into `work/<song>/`, logged as it happens.

## Work directory

```
work/<song>/project.yaml        the full project - settings and every stage's results
work/<song>/transcript.json     cached word timings, from whichever engine made them
work/<song>/vocals.wav          the singer alone (alignment.vocals) - listened to, never rendered
work/<song>/lyrics.txt          the reference lines as loaded
work/<song>/lyrics.srt          timed lyrics
work/<song>/audio-faded.wav     the song with its fade in/out baked in
work/<song>/images/             one still per scene, normalised to PNG
work/<song>/images.src/         downloads as the generator served them
work/<song>/overlays/           title and lyric PNGs (transparent)
work/<song>/overlay-clips/      the same, as alpha movie clips with fades
work/<song>/clips/              the image bed: scene and dissolve clips
```

Every artefact is named after a hash of its inputs, so re-running a stage only
redoes what actually changed. The finished video lands in `output/<song>.mp4`,
beside `work/` rather than inside it — one folder for the things you publish.

### What is committed

Most of `work/` is intermediates and stays out of git. Three things per song do
not, because they are the parts that are slow or impossible to recreate exactly:

```
work/<song>/project.yaml        settings, cue timings, the scene plan
work/<song>/lyrics.srt          timed lyrics
work/<song>/images/             the generated stills
```

The images are held in **Git LFS** (`work/*/images/*.png` in `.gitattributes`), so
the history stays small as scenes are regenerated. A fresh clone needs git-lfs
before they arrive as real files rather than pointers:

```bash
brew install git-lfs && git lfs install     # once per machine
git clone <repo> && cd video-lyrics-creator
git lfs pull
```

Note that `project.yaml` records absolute paths to the audio and lyrics on the
machine that made it, so another machine will want those two repointed. The root
pointer is per-machine and is not committed at all; `video-lyrics init` writes a
new one.

## Tests

```bash
.venv/bin/python -m pytest                  # units + a full ffmpeg render
.venv/bin/python -m pytest -m "not slow"    # units only
```
