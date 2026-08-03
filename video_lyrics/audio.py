"""Audio probing helpers (ffprobe)."""

from __future__ import annotations

import array
import json
from pathlib import Path

from .util import VideoLyricsError, run, which

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
