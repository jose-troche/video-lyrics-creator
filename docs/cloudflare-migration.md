# Moving video-lyrics-creator to a free Cloudflare tier

## Context

Today this is a machine-bound macOS CLI: a Python pipeline that shells out to ffmpeg and drives a
visible Chrome window to get images. The goal is to run it from anywhere, shared with a few people,
at **$0/month**, deferring the `tune` TUI.

**Verdict: feasible, but not on Cloudflare alone — and the hard part is not image generation.**

Research findings that set the shape of this plan:

| Constraint | Number | Consequence |
|---|---|---|
| Cloudflare Containers | **Workers Paid ($5/mo) only** | No containers at all on free. Hard blocker. |
| Workers free CPU | **10 ms** / invocation, 128 MB | Cannot run ffmpeg, PIL, or whisper. |
| Workflows free CPU | **10 ms** / step | Orchestration only; I/O wait is unmetered. |
| R2 free | 10 GB, egress free | Working set is ~1 GB/song → must not store intermediates. |
| Workers AI free | 10,000 neurons/day | flux-1-schnell ≈ 19 neurons/image → ~500 images/day. |
| GitHub Actions free | 2,000 min/mo private, **6 h** job cap, 4 vCPU/16 GB | The ffmpeg host. ffmpeg preinstalled. |

So: **image generation is the cheap part; ffmpeg is the expensive part** — though "expensive" turned
out to mean minutes, not hours. Measured on this machine (8 cores, 4 performance):

| Work | Measured |
|---|---|
| Bed, 144 s song, 20 scenes → 39 clips | **278 s** sequential (1.94× realtime), 109 MB |
| One 15 s scene clip at supersample 3 / 2 / 1 | 14.1 s / 11.9 s / 10.3 s |

Extrapolating to the worst song on disk (`dinner-for-5k`, 305 s, 111 clips) puts a full bed around
10 min and stage C as a whole well under half an hour. The 7200 s timeout at
[render_ffmpeg.py:137](../video_lyrics/render_ffmpeg.py#L137) is a defensive ceiling, not a measurement —
do not read it as a runtime estimate, as an earlier draft of this document did.

The consequence: there is **no compute pressure to trade image quality against**. Every render fits
inside GitHub Actions' 6-hour cap many times over.

**The core design decision: do not port `video_lyrics/` to TypeScript.** Containerize it unchanged.
Cloudflare becomes the front door, the state store, and the artifact store; the existing Python runs
in a GitHub Actions job against a work dir synced from R2. Near-zero rewrite of ~5,000 lines.

## Answering the browser/creds question

Yes — and it is the right answer, not a compromise. Two viable shapes; this plan builds both, in order.

**Why it works at all:** `images` is already a pluggable stage. `PROVIDERS` at
[images.py:36](../video_lyrics/images.py#L36) dispatches by name, and `_generate_manual`
([images.py:141](../video_lyrics/images.py#L141)) already writes every scene's prompt **and its expected
output filename**, then picks up whatever lands in `images/`. The cloud image step is therefore
`manual` + transport. The runner never needs a browser.

**Phase 2 — local agent shim (reuses `browser_ai.py` verbatim).** A ~100-line poller on your Mac:
ask the Worker for pending prompts, call the existing [chatgpt.py](../video_lyrics/chatgpt.py) /
[meta_ai.py](../video_lyrics/meta_ai.py) drivers through the existing persistent profile at
`~/.video-lyrics/chatgpt-profile`, PUT results to R2. All 1,064 lines of
[browser_ai.py](../video_lyrics/browser_ai.py) — busy/refusal detection, the three rewordings, the
backoff — keep working untouched.

**Phase 4 — MV3 browser extension (the distributable end state).** A content script on
`chatgpt.com` / `meta.ai` uses the tab's own logged-in session: types the prompt, watches the DOM for
the image, fetches the blob, PUTs it to a presigned R2 URL, asks for the next one. No Python, no
Playwright, no 2 GB Chrome profile per user, and no credential ever leaves the browser. Cloudflare's
bot challenge — the thing that blocks headless today ([README.md:188](../README.md#L188)) — is a
non-issue in a real user's tab. The selector knowledge in the `Site` dataclasses ports directly to
JS; budget ~500 lines. Prompt-queue extensions for chatgpt.com already ship on the Chrome Web Store,
so nothing here is unproven.

**Explicitly rejected: exporting session cookies to a Worker.** Datacenter IPs get challenged,
sessions rotate, and it puts a bearer credential for your account in cloud storage. Don't.

**Honest note:** driving the consumer chat UI is against OpenAI's terms on automated access, same as
today. The extension arguably narrows the exposure (real browser, real session, user present) but
does not eliminate it. Ban risk lands on the account holder. Workers AI flux stays wired in as a
per-project alternative for anyone who doesn't want that.

## Architecture

```
Browser (Cloudflare Access)
    │
    ▼
Worker  ── UI + API ──┬── R2      audio, images, project.json, output MP4
  │                   ├── D1      users, projects, job index
  │                   └── DO      one per project: stage state, WebSocket progress
  │
  ├── Workflow ── orchestrates stages, waits on I/O (10 ms CPU/step is enough)
  │
  ├── workflow_dispatch ──► GitHub Actions runner  (Docker: python + ffmpeg)
  │                            └── lyrics/transcribe/align/plan, overlays/bed/render
  │
  └── Queue ──► browser agent or MV3 extension  (images, on the user's own machine)
```

**Stage split**, cutting exactly along the existing boundaries in
[pipeline.py:25](../video_lyrics/pipeline.py#L25):

- **A — runner, unattended (~5-10 min):** `lyrics → transcribe → align → plan`
- **B — user's browser, attended:** `images`
- **C — runner, unattended (~15-25 min, measured):** `overlays → bed → render` → upload MP4

A→B is a natural pause: the UI lists prompts, the extension drains them, the Workflow resumes on the
last upload.

## Implementation

### Phase 0 — retracted

An earlier draft proposed trimming the render settings to fit a runner. **Measurement killed it.**
Recorded here so it does not get re-proposed:

- **Supersample 3× → 2× — do not.** [motion.py:38-45](../video_lyrics/motion.py#L38-L45) documents that
  the 3× exists to give the zoompan crop sub-pixel precision, "which is what actually removes the
  stepping" on slow motion. It is a deliberate fix for a real artifact. And it is not even where the
  time goes: 14.1 s vs 11.9 s on a 15 s clip, ~15% of the bed, because the 1080p x264 encode
  dominates, not the 5760×3240 scale. A quality regression to buy almost nothing.
- **`-crf 16` → `-crf 20` on intermediates — do not.** These feed a `-crf 18` final pass; making the
  intermediate worse than the output makes it the weakest link, and slow pans over detailed AI stills
  are the banding-prone case.
- **Dropping the `qtrle` overlay track — lossless, but low value.** qtrle is lossless RLE and the
  concat is `-c copy`, so compositing the PNGs directly in the final graph is genuinely identical
  output and saves 592 MB/song of disk. But it saves little *time*, `bake_alpha_clip` must stay for
  the Resolve path ([overlays.py:198-202](../video_lyrics/overlays.py#L198-L202)), and doing it needs
  per-input `-itsoffset` and alpha fades across ~80 inputs. Worth it only if R2 storage gets tight.

**What was implemented instead** (done, verified byte-identical on real song data): the bed clip loop
in [motion.py](../video_lyrics/motion.py) now bakes several clips at once via `default_jobs()`, and each
clip renders to a `.part` sibling that is moved into place only on success — which also fixes a
latent bug where a failed or interrupted ffmpeg left a partial file that the fingerprint cache would
trust forever. The speedup is modest (279 s → 229 s on a 144 s song) because ffmpeg already saturates
the performance cores; the crash-safety is the durable part.

**Sizing note for the runner:** two workers peak at **1.4 GB** of resident ffmpeg memory, four at
2.5 GB. Two is the default and four measured no faster. This rules out Cloudflare Containers'
`standard-1` (4 GiB) as comfortable and argues for `standard-2` (6 GiB) if the paid path is ever
taken; a GitHub Actions runner's 16 GB is ample.

### Phase 1 — containerize + R2 sync

- `Dockerfile`: `python:3.13-slim` + ffmpeg + the package. No Playwright, no Chrome.
- New `video_lyrics/remote.py`: `pull(project_id)` / `push(project_id)` against R2's S3 API using
  `requests` (already a dep). Mirrors the path layout in
  [config.py:435-486](../video_lyrics/config.py#L435-L486); reuses the atomic-write pattern at
  [config.py:162](../video_lyrics/config.py#L162).
- **Sync allowlist — never round-trip intermediates.** Push/pull only `project.yaml`,
  `transcript.json`, `lyrics.txt`, `lyrics.srt`, `images/`, source audio, `output/*.mp4`.
  `clips/`, `overlay-clips/`, `overlay-track.mov`, `bed.mp4`, `audio-faded.wav`, `images.src/` stay
  ephemeral on the runner. Store scene images as WebP (~300 KB vs 2.5 MB PNG); the pipeline already
  normalizes on read.
- New CLI subcommand `video-lyrics remote-run --project <id> --stages <a,b,c>` in
  [cli.py](../video_lyrics/cli.py) — pull, run, push, report status back.
- `.github/workflows/render.yml`: `workflow_dispatch` with a project-id input, `ubuntu-latest`,
  `actions/cache` for the ~1.5 GB faster-whisper `medium.en` weights, R2 creds as secrets.

**Whisper stays on the runner.** Workers AI is not a substitute: the base `whisper` model fails
above ~1-2 MB of audio and the `whisper-large-v3-turbo` output is VTT segments, not the word-level
timestamps that [align.py](../video_lyrics/align.py) depends on.

### Phase 2 — Cloudflare control plane

- **Worker** (TS, Workers static assets for the UI): project CRUD, audio/lyrics upload to R2 via
  presigned PUT, prompt queue endpoints, GitHub `workflow_dispatch` via a fine-grained PAT in a
  Worker secret, and a completion webhook the runner calls.
- **Durable Object per project** as the state authority — serializes writes so the runner and the
  browser agent can't race, and gives WebSocket progress to the UI for free. Free plan is
  SQLite-backed DOs only, which is what this needs. **Keep `transcript.json` in R2, not in the DO** —
  parsing 48 KB of JSON risks the 10 ms budget.
- **Workflow** drives A → wait-for-images → C. Every step must be pure I/O.
- **Watch the 50-subrequests-per-invocation free limit** — no fan-out over 56 scenes in a Worker;
  list-and-batch, or let the runner do it.
- **Auth: Cloudflare Access** in front of the Worker — an email allowlist for your few users, zero
  auth code. (Free Zero Trust seat count: confirm in the dashboard; I could not verify the current
  number from the docs.)
- **Local browser agent shim** as described above.

### Phase 3 — Workers AI as the alternative provider

Add `workers-ai` to `PROVIDERS` ([images.py:36](../video_lyrics/images.py#L36)). The Worker calls
flux-1-schnell and writes straight to R2. Caveats: it takes no width/height (square output — crop to
16:9, or use `stable-diffusion-xl-lightning` which does take dimensions), `steps` maxes at 8, and
the aesthetic differs from ChatGPT's, so `PROMPT_TEMPLATE`
([scenes.py:60](../video_lyrics/scenes.py#L60)) will want retuning. At ~19 neurons/image against
10,000/day this is effectively free and fully unattended — the fallback when nobody wants to babysit
a tab.

### Phase 4 — MV3 extension

Port the `Site` selector configs and the busy/refusal/reword loop from
[browser_ai.py:103-125](../video_lyrics/browser_ai.py#L103-L125) to a content script. Load unpacked for
a handful of users. **Write images under the existing `chatgpt` stem** (`_stem_for(scene, provider)`,
[images.py:212](../video_lyrics/images.py#L212)) so every existing `work/` directory carries over
without regenerating.

## Storage budget (R2 free = 10 GB)

Durable per song: ~10 MB source audio + ~20 MB WebP images + ~1 MB text/JSON + ~150 MB MP4.

The MP4 dominates and is **regenerable** — the hash-named artifact system already guarantees that.
Set an R2 lifecycle rule expiring `output/` after 30 days and keep the recipe forever at ~30 MB/song.
That is ~300 songs of recipes, and re-render on demand. Without expiry you get ~55 songs.

## What does not move

- **`tune`** ([tune.py](../video_lyrics/tune.py), 846 lines of curses + `ffplay`) — deferred per your
  call. Cues are still editable through the API; a web waveform editor is a later phase.
- **DaVinci Resolve rendering** ([render_resolve.py](../video_lyrics/render_resolve.py),
  [handoff.py](../video_lyrics/handoff.py)) — desktop-only, stays local. ffmpeg is already the default.
- **macOS specifics** — default font `Avenir Next Demi Bold` ([text.py:14-23](../video_lyrics/text.py#L14-L23))
  does not exist on `ubuntu-latest`. Bundle a font in the image and change the default, or overlays
  will silently fall back and shift your typography.

## Verification

1. **Phase 0 (done):** `pytest -q` (246 pass) plus a real-data check that the parallel bed is
   byte-identical to the sequential one — render the same scenes twice at `jobs=1` and `jobs=4` into
   separate directories and compare sha256 per clip. Confirmed identical, no `.part` files left.
2. **Phase 1:** `docker run` the image against a real R2 bucket for one existing song
   (`work/victorious`), stages A then C, with images pre-seeded. Byte-compare the output against the
   local render — the hash-named artifacts make this a real equality check.
3. **Phase 1:** trigger the workflow by hand from the Actions tab; confirm it finishes inside the
   6-hour cap and that the runner's disk never exceeds ~25 GB.
4. **Phase 2:** end-to-end from the browser — upload audio + lyrics, watch stage A complete, drain
   prompts with the local agent, watch C produce a playable MP4. Confirm Access blocks a
   non-allowlisted email.
5. **Cost check:** after one full song, read the Workers/R2/Workers AI dashboards and the Actions
   minutes counter. Confirm every meter is inside free.

## Main risks

- **A long song overruns the 6-hour runner cap.** Measurement says this is not close — a 305 s song
  should land near 20 min. If it ever binds, stage C chunks into per-scene-range jobs across
  sequential runs: the clip system is content-addressed, so partial work resumes cleanly.
- **The extension breaks on a chatgpt.com redesign.** Same fragility you have today; the mitigation
  is that Workers AI (Phase 3) is a config flip away.
- **2,000 Actions min/mo across a few users** ≈ 30–60 songs/month. If that binds, making the repo
  public gives unlimited minutes (media lives in R2, not in the repo), or Cloud Run Jobs' free tier
  (180k vCPU-s/mo, 24 h task cap) — though that needs a card on file, which fails "strictly free".
