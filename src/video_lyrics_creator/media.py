from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .errors import VideoLyricsError


def require_program(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise VideoLyricsError(f"Required executable is not on PATH: {name}")
    return path


def probe_media(path: str | Path) -> dict:
    source = Path(path)
    if not source.is_file():
        raise VideoLyricsError(f"Media file does not exist: {source}")
    command = [
        require_program("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        payload["duration"] = float(payload.get("format", {}).get("duration", 0.0))
        if payload["duration"] <= 0:
            raise ValueError("duration is not positive")
        return payload
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise VideoLyricsError(f"ffprobe could not inspect {source}: {detail.strip()}") from exc


def verify_video(path: str | Path, expected_duration: float, tolerance: float = 0.25) -> dict:
    payload = probe_media(path)
    types = {stream.get("codec_type") for stream in payload.get("streams", [])}
    missing = {"video", "audio"} - types
    if missing:
        raise VideoLyricsError(f"Rendered file is missing stream(s): {', '.join(sorted(missing))}")
    delta = abs(payload["duration"] - expected_duration)
    if delta > tolerance:
        raise VideoLyricsError(
            f"Rendered duration {payload['duration']:.3f}s differs from audio "
            f"duration {expected_duration:.3f}s by {delta:.3f}s"
        )
    return payload

