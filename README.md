# Video Lyrics Creator

Video Lyrics Creator automates a complete synchronized lyric video with **DaVinci Resolve 21 Free**. It uses the song audio to determine which lyric lines are present and when they are sung, confirms their wording against a reviewed lyrics source, generates lyric-inspired still images, prepares transparent title and lyric overlays, builds a Resolve timeline, adds cross-dissolves and alternating Ken Burns motion, and renders the finished video with the original audio.

The opening title includes `José Troche` unless `author` is explicitly changed in the project manifest.

## How the free-edition workflow works

Resolve Free blocks external scripts from attaching to the running application. This project therefore uses two stages:

1. The terminal CLI aligns lyrics, generates images and overlays, validates the project, and copies everything Resolve needs into a sandbox-safe handoff folder.
2. An internal script launched from **Workspace > Scripts** reads the latest handoff job and builds or renders the timeline from inside Resolve.

No **External scripting using: Local** preference is required or expected.

## Requirements

- DaVinci Resolve 21 Free
- Python 3.9 or newer for terminal-side preparation
- On macOS, a universal Python.org framework installation that Resolve can embed; Apple's
  `/usr/bin/python3`, Homebrew Python, and the project virtual environment are not sufficient
  by themselves for Resolve's internal Py3 host
- `ffmpeg` and `ffprobe` on the terminal `PATH`
- Codex CLI signed in with ChatGPT for the default image provider, or an OpenAI API/local generator alternative
- `faster-whisper` for automatic vocal alignment, unless reviewed SRT/LRC timings are supplied
- A Google Cloud Desktop OAuth client and the Google Docs API when lyrics are supplied as private `.gdoc` files

Resolve’s embedded script only uses Python’s standard library and the Resolve API. Terminal-only packages are never loaded inside Resolve.

## One-time installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[align]'
codex login
codex login status
cp .env.example .env
# Edit .env for Google OAuth or the optional OpenAI API provider.
video-lyrics install-resolve
```

`codex login status` must report `Logged in using ChatGPT` for the default image provider. The optional, separately billed OpenAI API backend also requires `python -m pip install -e '.[openai]'` and `OPENAI_API_KEY` in `.env`.

Upgrading the packaging tools is required on Python installations whose bundled
`pip` predates editable installs defined by `pyproject.toml`. If the editable
install reports that `setup.py` or `setup.cfg` is missing, run the upgrade line
above inside the active virtual environment and retry the install.

The last command installs:

- `Video Lyrics Creator.py` in the user Resolve `Fusion/Scripts/Utility` directory.
- A private copy of the required Python modules in `Fusion/Scripts/Modules/video_lyrics_creator`.

On macOS, the installer first checks for a Resolve-embeddable runtime at
`/Library/Frameworks/Python.framework/Versions/<version>/Python`. If it is missing, install a
universal macOS build from [python.org](https://www.python.org/downloads/macos/), fully quit
Resolve, and rerun `video-lyrics install-resolve`. Installing Python into the project virtual
environment does not satisfy this Resolve requirement. `--skip-python-check` is available only
for machines where Python already works in Resolve's **Workspace > Console > Py3** despite using
a nonstandard runtime layout.

Restart Resolve after installation. The menu entry will appear under **Workspace > Scripts** as
**Video Lyrics Creator**, sometimes nested under **Utility** depending on the platform/menu
layout. The Free edition does not expose **External scripting using: Local**; that setting is not
needed because this launcher runs inside Resolve.

To preview the installation paths without writing anything:

```bash
video-lyrics install-resolve --dry-run
```

## Create and render a lyric video

Create a UTF-8 text file with one lyric line per display cue. For both text files and Google Docs, blank lines, underscore-only separators such as `________`, and text inside square brackets are ignored. For example, `[Verse 1]` is discarded and `Amazing [quietly] grace` becomes `Amazing grace`.

### 1. Prepare and stage the job in Terminal

```bash
video-lyrics init project.json \
  --title "Song Title" \
  --audio /absolute/path/song.wav \
  --lyrics /absolute/path/lyrics.txt \
  --style "cinematic photographic realism, warm amber and deep blue palette"

video-lyrics run project.json
```

The default Codex image provider uses the saved ChatGPT login from Codex CLI and does not use `OPENAI_API_KEY`. The optional OpenAI API provider reads `OPENAI_API_KEY` from `.env` beside the project’s default `work` directory or from the current directory. A key already exported in the environment takes precedence. The real `.env` is ignored by Git.

## Google Docs lyrics

`--lyrics` accepts both ordinary UTF-8 text files and Google Drive Desktop `.gdoc` pointer files. A `.gdoc` contains only a document ID, so private documents require a one-time Google OAuth authorization.

### One-time Google Cloud setup

1. Create or select a Google Cloud project.
2. Enable the **Google Docs API**.
3. Configure the OAuth consent screen and add your Google account as a test user if the app is in Testing status.
4. Create an OAuth client with application type **Desktop app**.
5. Copy `.env.example` to `.env`, then add the client values:

```dotenv
GOOGLE_DRIVE_CLIENT_ID=your-desktop-client-id
GOOGLE_DRIVE_CLIENT_SECRET=your-desktop-client-secret
GOOGLE_DRIVE_REFRESH_TOKEN=
```

Authorize the application:

```bash
video-lyrics google-auth
```

The command opens Google’s consent page, uses a PKCE-protected localhost callback, and writes the returned refresh token to `.env` without displaying it. The application requests `drive.readonly`; tokens and client credentials are never copied into the Resolve handoff job.

Google classifies `drive.readonly` as a restricted scope because it can read all Drive files. Keep this as a private personal OAuth app. If an external consent screen remains in **Testing**, Google currently expires its refresh token after seven days; rerun `video-lyrics google-auth` when needed.

You can now use a `.gdoc` exactly like a text lyrics file:

```bash
video-lyrics init project.json \
  --title "Song Title" \
  --audio /absolute/path/song.wav \
  --lyrics "/absolute/path/lyrics.gdoc" \
  --style "cinematic photographic realism"
```

During `prepare` or `run`, the CLI refreshes a short-lived access token and reads only the body of the Google Doc's first/main tab. The tab name (for example, `Main`), the first remaining body line used as the document/song title, and all other tabs are ignored. The same cleanup rules used for text files remove square-bracket annotations, empty lines, and underscore-only separators. See [Google Drive OAuth setup](docs/google-drive-oauth.md) for troubleshooting and security details.

`run` performs alignment, scene planning, image generation, overlay generation, validation, and job staging. It does **not** attempt to connect to Resolve.

By default, the staged job and all Resolve-readable media are written beneath:

```text
~/Movies/Video Lyrics Creator/          # macOS
~/Videos/Video Lyrics Creator/          # Windows and Linux
```

The final MP4 is configured under that folder’s `Output` directory. The project manifest is updated with the exact output path.

### 2. Build and render inside Resolve

1. Open DaVinci Resolve 21 Free.
2. Choose **Workspace > Scripts > Video Lyrics Creator**. If Resolve displays script categories, choose **Workspace > Scripts > Utility > Video Lyrics Creator**.
3. The script loads `latest-job.json`, creates the Resolve project and timeline, then waits for the render to finish.

Progress and errors are printed in Resolve’s Console. A machine-readable `resolve-result.json` is also written beside `latest-job.json`.

The same staged job cannot run twice accidentally. To rebuild an existing timeline, stage a fresh job and authorize replacement:

```bash
video-lyrics build project.json --render --replace-timeline
```

Then launch **Video Lyrics Creator** from Resolve again.

### 3. Verify the rendered deliverable

After Resolve finishes:

```bash
video-lyrics verify project.json
```

Verification confirms that the output contains audio and video streams and that its duration matches the source song.

## Reviewed timings

For the most reliable lyric synchronization, provide an SRT or LRC containing exactly one cue per non-empty canonical lyric line:

```bash
video-lyrics run project.json --timings reviewed.srt
```

The timing file contributes timestamps only. Displayed words always come from the reviewed lyrics file.

With automatic alignment, the audio transcription is the source of truth for cue presence and timing. The lyrics file confirms the displayed wording of matching lines; reference lines with no transcription match are skipped instead of receiving interpolated timings. Raw, unreviewed transcription text is never displayed. Song transcription analyzes the complete track without speech-oriented voice-activity filtering, which can otherwise suppress sung vocals. On CPU-only systems, the CLI selects efficient `int8` inference instead of allowing a float16 model to fall back to float32. Review `alignment_confidence` in `project.json`; Resolve adds red markers for cues below 60% confidence.

## Staged commands

Each terminal stage can run independently:

```bash
video-lyrics prepare project.json --whisper-model small
video-lyrics images project.json
video-lyrics overlays project.json
video-lyrics validate project.json
video-lyrics build project.json --dry-run
video-lyrics build project.json --render
```

`build --dry-run` prints the exact frame-level timeline plan. A normal `build` copies the validated inputs into the handoff directory and writes a new internal Resolve job.

During staging, each scene still is converted to a high-quality, exact-frame-count H.264 source
clip. This avoids Resolve's user-configured default still duration truncating long scenes and
creating black gaps. Resolve still applies the Ken Burns motion and transitions to those clips.

To prepare an editable timeline without rendering:

```bash
video-lyrics build project.json --timeline-only
```

## Image size and quality

The default Codex backend invokes built-in `$imagegen` through your ChatGPT-managed Codex login. Built-in image generation uses `gpt-image-2`; the project requests **medium quality** because images must survive the 1920×1080 frame and an animated zoom. Set `image_generation.quality` to `high` in `project.json` when maximum detail is worth the additional Codex usage.

For the default 1920×1080 video, every generated or reused scene image is center-cropped and resized to exactly 1920×1080, using a cover resize with no letterboxing or pillarboxing. The optional OpenAI API backend requests 1920×1088—the nearest valid GPT Image size that fully covers the frame—before that crop. Resolve also applies fill scaling as a final safeguard.

Codex outputs include prompt-fingerprint sidecars. Matching completed scenes are reused, while changed prompts or dimensions regenerate automatically. If a batch reaches a Codex usage limit or otherwise stops, rerun without `--force-images` to resume at the first incomplete scene. `--force-images` intentionally regenerates every scene. Reused images are still refitted to the current video dimensions.

## Image providers

`codex` is the default and uses built-in image generation under the ChatGPT account reported by `codex login status`:

```bash
video-lyrics images project.json --provider codex
```

No API key is passed to the child Codex process. Each missing scene runs in an isolated, ephemeral `codex exec` session rooted in the project work directory. Built-in image generations count against general Codex usage limits and can consume included usage substantially faster than ordinary turns, so a large scene batch may pause at the plan limit. See [Codex image generation](https://learn.chatgpt.com/docs/image-generation) and [Codex authentication](https://learn.chatgpt.com/docs/auth).

To use the separately billed OpenAI Images API instead:

```bash
video-lyrics images project.json --provider openai
```

To use a local generator, provide an argument template. It is executed directly, not through a shell:

```bash
video-lyrics images project.json \
  --provider command \
  --image-command 'my-generator --prompt-file {prompt_file} --output {output} --width {width} --height {height}'
```

For a no-cost process test, use `--provider placeholder`. Placeholder scenes are visibly labeled and are not final artwork.

## Resolve timeline layout

- V1/V2 alternate scene stills so adjacent clips overlap.
- Fusion nodes animate scene zoom and opacity, producing Ken Burns motion and cross-dissolves.
- V3 contains synchronized transparent lyric overlays.
- V4 contains the opening song title and author.
- A1 contains the original, unprocessed song audio.
- Default output is 1920×1080, 30 fps, H.264/AAC MP4.
- Resolve renders the picture without compressed audio; after rendering, the internal launcher
  muxes a fresh 320 kbps AAC track directly from the staged, byte-identical source WAV. This
  avoids Resolve AAC artifacts and preserves the original timing.

See [the free Resolve workflow](docs/free-resolve-workflow.md) for handoff and troubleshooting details and [the manifest reference](docs/manifest.md) for configuration fields.

## Testing

```bash
python -m unittest discover -s tests -v
```

The tests validate alignment, staging, installation, internal one-shot execution, timeline planning, overlays, and media invariants without launching Resolve.
