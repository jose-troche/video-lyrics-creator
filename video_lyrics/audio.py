"""Audio probing helpers (ffprobe)."""

from __future__ import annotations

import json
from pathlib import Path

from .util import VideoLyricsError, run, which


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
