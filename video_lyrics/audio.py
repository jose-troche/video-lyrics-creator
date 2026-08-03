"""Audio probing helpers (ffprobe)."""

from __future__ import annotations

import array
import json
from pathlib import Path

from .util import VideoLyricsError, ensure_dir, log, run, which

ENVELOPE_RESOLUTION = 100   # buckets per second - 10ms, finer than anything visible
ENVELOPE_RATE = 8000        # decoding rate; only the loudness shape matters here


def duration(path: Path | str) -> float:
    """Exact media duration in seconds."""
    path = Path(path)
    if not path.is_file():
        raise VideoLyricsError(f"Audio file not found: {path}")
    proc = run(
        [
            which("ffprobe"), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ]
    )
    payload = json.loads(proc.stdout or "{}")
    value = payload.get("format", {}).get("duration")
    if value is None:
        raise VideoLyricsError(f"ffprobe could not read a duration from {path}")
    return float(value)


def envelope(
    path: Path | str,
    *,
    resolution: int = ENVELOPE_RESOLUTION,
    rate: int = ENVELOPE_RATE,
) -> list[float]:
    """The song's loudness shape: peak amplitude (0..1) per bucket, `resolution` a second.

    Drawn as a waveform by `video-lyrics tune`, so that a lyric's start can be seen
    landing on the phrase it belongs to. Decoding a whole song this coarsely takes a
    fraction of a second, so nothing is cached.
    """
    path = Path(path)
    if not path.is_file():
        raise VideoLyricsError(f"Audio file not found: {path}")
    proc = run(
        [
            which("ffmpeg"), "-v", "error", "-i", str(path),
            "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
        ],
        binary=True,
    )
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    if not samples:
        return []

    width = rate / resolution
    peaks: list[float] = []
    for index in range(int(len(samples) / width)):
        chunk = samples[int(index * width) : int((index + 1) * width)]
        peaks.append(max(max(chunk), -min(chunk)))

    loudest = max(peaks) or 1
    return [peak / loudest for peak in peaks]


def bake_fades(
    source: Path | str,
    output: Path | str,
    *,
    duration: float,
    fade: float = 1.0,
    force: bool = False,
) -> Path:
    """A copy of `source`, exactly as long, with a fade-in and fade-out baked in.

    Baked once into a plain file rather than applied live, so every consumer - the
    ffmpeg engine, Resolve's import (which cannot keyframe a clip's gain any more
    than it can a transition), and the Resolve handoff script running later in its
    own process - all just play the same already-faded audio.
    """
    source = Path(source)
    output = Path(output)
    if output.is_file() and not force:
        return output
    if not source.is_file():
        raise VideoLyricsError(f"Audio file not found: {source}")
    ensure_dir(output.parent)

    fade = max(0.0, min(fade, duration / 2))
    args = [which("ffmpeg"), "-y", "-loglevel", "error", "-i", str(source)]
    if fade > 0:
        fade_out_start = max(0.0, duration - fade)
        args += [
            "-af",
            f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}",
        ]
    args += ["-t", f"{duration:.3f}", str(output)]
    run(args, capture=False)
    log.info("Audio ready: %.2gs fade in/out, %.2fs total.", fade, duration)
    return output
