"""Small shared helpers: logging, subprocess, paths, time math."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

log = logging.getLogger("video_lyrics")


class VideoLyricsError(RuntimeError):
    """Any expected, user-facing failure."""


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


def load_dotenv(path: Path | str = ".env") -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding)."""
    path = Path(path)
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def which(program: str) -> str:
    found = shutil.which(program)
    if not found:
        raise VideoLyricsError(f"Required executable not found on PATH: {program}")
    return found


def run(cmd: list[str], *, capture: bool = True, check: bool = True,
        cwd: Path | str | None = None, timeout: float | None = None,
        env: dict[str, str] | None = None,
        binary: bool = False) -> subprocess.CompletedProcess:
    """Run a command, logging it, raising VideoLyricsError on failure.

    `binary` keeps stdout as bytes, for the commands that pipe media back.
    """
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd],
        capture_output=capture,
        text=not binary,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        env=env,
        check=False,
    )
    if check and proc.returncode != 0:
        # With `binary`, stdout is the media itself - only stderr is worth quoting.
        detail = proc.stderr if binary else (proc.stderr or proc.stdout)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        tail = "\n".join((detail or "").strip().splitlines()[-25:])
        raise VideoLyricsError(
            f"Command failed ({proc.returncode}): {' '.join(str(c) for c in cmd[:4])} ...\n{tail}"
        )
    return proc


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "untitled"


def short_hash(*parts: object, length: int = 16) -> str:
    digest = hashlib.sha1("␟".join(str(p) for p in parts).encode("utf-8"))
    return digest.hexdigest()[:length]


# How much of the fingerprint a scene image's filename carries. Short, because
# these names are read and typed by hand - they are what `manual` mode asks for and
# what you delete to redraw one scene. 32 bits against a few dozen scenes is not a
# collision anyone will meet, and the images stage checks nothing but the stem.
STEM_HASH_LENGTH = 8


def scene_stem(scene: dict, tag: str) -> str:
    """The filename one scene's image is stored under, for one generator.

    Lives here because two layers have to agree on it exactly: the browser driver
    writes the download under this name, and the images stage looks for it under
    this name afterwards. `tag` is the provider, so switching provider asks for a
    new picture instead of adopting another site's.
    """
    fingerprint = short_hash(scene["prompt"], tag, length=STEM_HASH_LENGTH)
    return f"scene-{scene['index']:03d}-{fingerprint}"


def ensure_dir(path: Path | str) -> Path:
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def expand(path: str | Path) -> Path:
    return Path(str(path)).expanduser().resolve()


def snap(seconds: float, fps: float) -> float:
    """Snap a time to the nearest frame boundary."""
    return round(seconds * fps) / fps


def frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


def format_timecode(seconds: float, *, comma: bool = True) -> str:
    """SRT-style timestamp: HH:MM:SS,mmm."""
    seconds = max(0.0, seconds)
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    sep = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def human_time(seconds: float) -> str:
    minutes, secs = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):d}:{secs:05.2f}"
