# Video Lyrics Creator

Automates the creation of a lyric video: a video that pairs a song's audio with
lyrics and corresponding images, synchronized to the audio.

## Inputs

- **Audio**: the song.
- **Lyrics**: either a plain text file or a Google Doc (`.gdoc`).
  - Google Docs are accessed via OAuth. `GOOGLE_DRIVE_CLIENT_ID` and
    `GOOGLE_DRIVE_CLIENT_SECRET` are provided; `GOOGLE_DRIVE_REFRESH_TOKEN` is
    obtained through a one-time interactive login.
  - A Google Doc may contain multiple tabs. If the lyrics document has more than
    one tab, use only the content of the first tab.

## Synchronization

The audio transcription determines which lyric cues exist and their timing; the
lyrics source confirms the displayed wording for matched lines.

- Do not create a cue for reference text that the audio does not confirm.
- Create one cue per line in the lyrics file.

Automatic timing is a starting point, not the last word: it must be possible to
review the result against the audio and correct it.

- Play any part of the song on demand, with play/pause and seek controls, and see
  where each line's start and end fall relative to what is being heard.
- Adjust a line's start and end, add a cue for a line the audio never confirmed,
  and remove one that should not be shown.
- Corrections are recorded in the project file and survive a later re-run of the
  pipeline; only an explicit re-alignment discards them.

## Image Generation

Generate one static image per one or two lyric lines, synchronized with the
audio.

- Images are generated with the Codex CLI, using its built-in `image_gen` tool in
  full-auto mode (no API key required).
- Alternatively, images may be supplied instead of generated.

## Video Output

Produce a video containing:

- The song's audio, with a short fade-in and fade-out, its length unchanged.
- A title card at the beginning, showing the song title and author (José
  Troche).
- The generated (or supplied) images, stitched together with cross-dissolve
  transitions and a zoom in/out (Ken Burns) effect for a dynamic look. Other
  dynamic effects may also be used.
- Lyrics on a text/subtitles track, shown one line at a time where possible,
  with a fade in/out effect on each line.

Requirements:

- The video's duration must equal the audio's duration exactly — no silence at
  the beginning or end.
- The title appears as the song starts and disappears before the first line of
  lyrics is shown.
- Use a separate track for each element: one track for the images (with the
  cross-dissolve and Ken Burns effects), one track for the lyrics/subtitles,
  and one track for the audio.

## Automation

Automate the video assembly and export end to end, with no manual step required
by default.

- ffmpeg is the default render engine: it assembles and exports straight from
  the CLI.
- DaVinci Resolve is an optional alternative engine (`--engine resolve`), driven
  through the free version of Resolve 21's scripting/automation API, for anyone
  who wants the result as an editable Resolve timeline.
