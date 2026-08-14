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

Needs **ffmpeg** on the PATH. Image generation drives a real browser, so install
that extra once as well:

```bash
.venv/bin/pip install -e ".[browser]"
.venv/bin/playwright install chromium
```

That is enough for the default render engine; **DaVinci Resolve 18+** (the free
edition is enough) is only needed if you choose `--engine resolve`.

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
  --title "My Song" \
  --context "a song after the crossing of the Red Sea in Exodus"

video-lyrics run                 # everything, end to end
video-lyrics status              # what is done so far
video-lyrics cues                # the timed lyrics
video-lyrics tune                # listen, and fix any line that sits wrong
```

`--context` is optional and says, in your own words, what the whole song is
about. Every image prompt is generated on its own — a couple of lyric lines, in
their own chat — and lyrics rarely name the story they belong to, so without it
the generator picks a setting afresh each time and twenty scenes can end up in
twenty different worlds. With it, each prompt opens with that one frame:

> *Context for the whole video: a song after the crossing of the Red Sea in
> Exodus. Every scene belongs to that same story, setting and world — keep them
> consistent with it.*

It's kept in the project file as `context:`, so you can change your mind later —
`video-lyrics set context "..."` then `video-lyrics plan`. Doing that rewrites
every prompt, and a changed prompt means a new image, so run `images` afterwards
to redraw them. Leaving it empty says nothing at all and leaves prompts exactly
as they were, which is what keeps songs finished before this from asking to be
redrawn.

`video-lyrics run` goes straight from aligning to planning scenes without
stopping. Pass `--tune` and it pauses right after aligning to ask whether you
want to fine-tune lyric timing by ear first; answering no just continues to
`plan`. Tuning is always available on its own as `video-lyrics tune`.

Every stage is also its own command, so you can iterate on one part without
redoing the rest:

```bash
video-lyrics lyrics              # load the reference lyrics, measure the audio
video-lyrics transcribe          # cached in work/<song>/transcript.json
video-lyrics align               # re-time after changing alignment settings
video-lyrics tune                # hear the song and adjust the timing by hand
video-lyrics plan                # regroup lines into images
video-lyrics images              # generate the stills (a browser window opens)
video-lyrics overlays            # title card, lyric PNGs, lyrics.srt
video-lyrics bed                 # bake Ken Burns motion + cross dissolves
video-lyrics render              # assemble with ffmpeg and export
```

| flag | meaning |
| --- | --- |
| `--force` | redo a stage even though its output is cached |
| `--tune` | (`run`) pause after align to ask about fine-tuning lyric timing |
| `--engine resolve` | render through DaVinci Resolve instead of ffmpeg |
| `--handoff` | (Resolve) prepare everything and finish from Resolve's Scripts menu |
| `--launch` | (Resolve) start Resolve first and wait until it answers |
| `--images-dir DIR` | use your own images instead of generating them |
| `--limit N` | (`images`) generate only N of the missing images, then stop |
| `--lines-per-image N` | (`init`, `plan`, `run`) 1 for an image per lyric line, 2 to pair them up (default) |
| `--from` / `--to` | run part of the pipeline (`run --from plan --to bed`) |

**How many lines share an image.** Two by default: `plan` bundles consecutive
lyric lines up to that many, and still splits a pair that would hold one picture
past `alignment.max_scene_duration` or that crosses into a new section. So `2`
means *up to* two, while `1` means one image per line, always — twice the
pictures, twice the generating.

```bash
video-lyrics init --lines-per-image 1 --audio ... --lyrics ...
video-lyrics plan --lines-per-image 1      # regroup an existing song
video-lyrics set image_generation.lines_per_image 1   # same thing, without replanning
```

Passing the flag saves it, so later runs keep grouping the same way. Regrouping
changes each scene's prompt, and a changed prompt means a new image: run `plan`
then `images` to fill in the scenes that no longer have one.

### Where the images come from

`image_generation.provider` controls how `video-lyrics images` fills in each scene:

* `chatgpt` (default) — drives [chatgpt.com](https://chatgpt.com/) in a real,
  visible browser and downloads each picture automatically.
* `meta` — the same, against [meta.ai](https://www.meta.ai/).
* `manual` — no generator at all. It writes every scene's prompt, and the exact
  filename stem it expects, to `work/<song>/images/prompts.txt`. Paste each prompt
  into whatever you use by hand (Midjourney, an image site, ...), save the result
  under that stem in `work/<song>/images/` — png, jpg, or webp, whichever the tool
  gives you — then run `video-lyrics images` again; it picks up the files you made
  (converting them to PNG) and reports anything still missing.
* `supplied` — same idea, but for images you already have; see `--images-dir`
  above, they're adopted in filename order instead of matched by name.

```bash
video-lyrics set image_generation.provider meta
video-lyrics images --limit 3    # try a few before committing to a whole song
```

#### The browser providers, in more detail

Both `chatgpt` and `meta` work the same way, and neither is an API: they type
into a consumer web page and read the answer back off it. That means they depend
on that page's current markup and can break when it changes, and that running
them is your own call to make against the site's terms of service — your own
account, your own project.

Login is never automated. Sign in once, by hand:

```bash
video-lyrics browser-login                      # chatgpt.com, in Google Chrome
video-lyrics browser-login --provider meta      # meta.ai
```

That opens an ordinary browser window — not one the tool is driving — pointed at
the profile the image provider will use later (`~/.video-lyrics/chatgpt-profile`,
`~/.video-lyrics/meta-ai-profile`, or wherever `<provider>_profile_dir` points).
Log in there, then come back to the terminal and press Enter: the command closes
the browser itself and checks the session really was saved, because a browser
only writes one out when it shuts down cleanly. (Which is also why a run that
force-quits its browser can lose the login and ask for it again — let it close
itself, or Ctrl-C once and wait.)

**Why a separate command:** sign in through Google inside an automated browser
and Google stops you with *"This browser or app may not be secure"* — a check on
the browser, which no amount of waiting or retrying gets past. A window nothing
is driving isn't subject to it. `video-lyrics images` still offers to wait while
you log in, which is fine for meta.ai; for a Google sign-in, use `browser-login`.

A profile belongs to the browser that created it, and to how that browser locks
it: Chrome encrypts its cookie database with a key from the OS keychain, and two
browsers that disagree about which key cannot read each other's session at all —
it is dropped silently, which looks exactly like "the login was not saved". So
both commands derive that from one place: the installed Chrome uses the real
macOS keychain, Playwright's bundled browser keeps Playwright's own fixed key
(its path changes with every upgrade, and asking the keychain about it would
raise a dialog mid-run).

Both commands also read the same `<provider>_channel` setting: `"chrome"` (the Google Chrome already installed —
the default for ChatGPT, because Google is markedly happier with a browser it
recognises), `"msedge"`, or `null` for Playwright's own bundled browser (the
default for meta.ai). On the command line, `--channel bundled` means `null`.

Leave `<provider>_headless` off: signed in or not, chatgpt.com serves a headless
browser a Cloudflare challenge instead of the page.

One prompt at a time. The next is only submitted once the current scene's image
has actually finished and been written to disk, plus a random delay between
`<provider>_min_delay` and `<provider>_max_delay` (1–4s by default) on top of
that, so requests do not land in an obvious, throttle-inviting pattern.

"Finished" is the hard part, and each site says it differently. Both show
something that looks like the answer well before it is one — meta.ai a preview at
the final resolution, ChatGPT a series of increasingly sharp versions of the same
picture — so waiting for "an image appeared" would save a half-drawn frame. The
meta driver waits for the finished image's own Download control to appear; the
ChatGPT driver waits for the Stop button to go away. ChatGPT also gets a fresh
chat per scene: asked for a second picture in a conversation that already has
one, it tends to edit the first instead of drawing something new.

**When a scene doesn't come back.** A site fails to draw one for one of two
reasons, and they want opposite answers, so the driver reads what the page
actually said (`main` plus its alert regions) instead of just timing out:

* *It won't draw this prompt* — a refusal, a content-policy line. The prompt is
  the only thing worth changing, so the same scene is asked again with a slightly
  reworded prompt (up to three wordings, each keeping the scene's own
  description and only asking for it less literally), in a fresh chat where the
  site keeps one. If none of them land, that scene is skipped and the song
  carries on; rewrite its `prompt:` in the project file and run `images` again.
* *The site is busy* — capacity, a rate limit, a quota, an image that would not
  download. No wording helps, so the scene is left as it is, the run waits a
  little longer before the next one, and it stops altogether after three in a
  row rather than grinding through the rest.

Either way the run keeps going and reports what it left behind; `video-lyrics
images` then asks only for the scenes still missing, so retrying later is just
running the same command again. A failure with *nothing* on the page to explain
it is different — that is usually markup that has moved on — so one is survived
and a second in a row stops the run with the selector advice below.

Raw downloads are kept in `work/<song>/images.src/`, whatever format the site
served, and each is also converted into the canonical PNG in
`work/<song>/images/`. A run interrupted partway through only asks the browser
for what is still missing, and a scene that already has a usable image is never
redrawn unless you pass `--force`.

**Regenerating just one scene:** `--force` redoes every scene, not one, so to
redraw a single scene delete its two cached files - same stem, one in each
folder - and run `images` again; everything else is left alone because it
still has a usable image:

```bash
rm work/<song>/images/scene-010-*.png work/<song>/images.src/scene-010-*.png
video-lyrics images
```

Editing that scene's `prompt:` in the project file first works too, and
doesn't need the delete: a changed prompt hashes to a new stem, so the old
files are simply orphaned rather than picked up again.

If a site's markup moves on and the composer or the generated image can no longer
be found, override `image_generation.<provider>_composer_selector` /
`<provider>_image_selector` with a CSS selector for the right element. The
defaults live in [`chatgpt.py`](video_lyrics/chatgpt.py) and
[`meta_ai.py`](video_lyrics/meta_ai.py), next to the ready/busy selectors that
decide when an image is done — everything site-specific is in those two files,
and the driver they share is [`browser_ai.py`](video_lyrics/browser_ai.py).

> The `codex` provider (the Codex CLI's `image_gen` tool) was replaced by
> `chatgpt`: the same account, without a second CLI to install and keep logged
> in. A project file that still says `codex` runs as `chatgpt`, keeping every
> image it already has; make it permanent with
> `video-lyrics set image_generation.provider chatgpt`.

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
video-lyrics set context "a song after the crossing of the Red Sea in Exodus"
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

  Each scene is also given its own **framing** (wide establishing shot, low angle,
  head-on symmetry, ...), cycled by the scene's position. Without it neighbouring
  prompts can be nearly or exactly the same text - two halves of one instrumental
  passage differ only by "(part 1 of 2)", and a chorus that comes round again is
  word-for-word identical - and an image generator handed the same prompt twice
  quite reasonably returns the same picture twice. The cycle is deterministic, so
  re-planning still lines up with images already generated.
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
work/<song>/images/             one still per scene, normalised to PNG
work/<song>/images.src/         downloads as the generator served them (chatgpt/meta)
work/<song>/overlays/           title and lyric PNGs (transparent)
work/<song>/overlay-clips/      the same, as alpha movie clips with fades
work/<song>/clips/              the image bed: scene and dissolve clips
```

Every artefact is named after a hash of its inputs, so re-running a stage only
redoes what actually changed.

The finished video lands in `output/<song>.mp4`, beside `work/` rather than inside
it — one folder for the things you actually publish.

### What is committed

Most of `work/` is intermediates and stays out of git. Three things per song do
not, because they are the parts that are slow or impossible to recreate exactly:

```
work/<song>/project.yaml        settings, cue timings, the scene plan
work/<song>/lyrics.srt          timed lyrics
work/<song>/images/             the generated stills
```

The images are held in **Git LFS** (`work/*/images/*.png` in `.gitattributes`), so
the history stays small even as scenes are regenerated. A fresh clone therefore
needs git-lfs before the images arrive as real files rather than pointers:

```bash
brew install git-lfs && git lfs install     # once per machine
git clone <repo> && cd video-lyrics-creator
git lfs pull
```

Note that `project.yaml` records absolute paths to the audio and lyrics on the
machine that made it, so another machine will want those two repointed. The root
`project.yaml` — the pointer at the top of the repo — is per-machine and is not
committed at all; `video-lyrics init` writes a new one.

## Tests

```bash
.venv/bin/python -m pytest                  # units + a full ffmpeg render
.venv/bin/python -m pytest -m "not slow"    # units only
```
