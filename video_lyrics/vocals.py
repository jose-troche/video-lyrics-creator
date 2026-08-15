"""Lift the singer out of the mix, so the aligner hears the words and not the band.

Every timing question this project asks is about one instrument: the voice.  The
drums, the pads and the guitars are, for that purpose, noise loud enough to bury the
answer - they are what makes a transcript come back half empty, and what keeps the
loudness envelope high long after a note has actually died away.  Demucs separates
them out, and everything downstream gets a stem with nothing on it but the singing.

Only ever used for *listening*: the render still uses the real song.  The stem is
cached beside the transcript, because it costs minutes to make and never changes.

Needs the optional extra:  pip install -e ".[vocals]"
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from .util import VideoLyricsError, log, run

MODEL = "htdemucs"


def available() -> bool:
    from importlib.util import find_spec

    try:
        return find_spec("demucs") is not None
    except (ImportError, ValueError):  # pragma: no cover - a broken install
        return False


def isolate(audio: Path, destination: Path, *, model: str = MODEL, force: bool = False) -> Path:
    """Write the vocal stem of `audio` to `destination` and return it."""
    audio, destination = Path(audio), Path(destination)
    if destination.is_file() and not force:
        log.info("Reusing the vocal stem at %s.", destination.name)
        return destination
    if not available():
        raise VideoLyricsError(
            "Isolating the vocal needs demucs. Run: pip install -e '.[vocals]' "
            "(or listen to the whole mix again with: video-lyrics set alignment.vocals false)"
        )

    log.info("Separating the vocal from %s with demucs; this takes a few minutes ...", audio.name)
    with tempfile.TemporaryDirectory(prefix="video-lyrics-demucs-") as workspace:
        run(
            [
                sys.executable, "-m", "demucs",
                "--two-stems", "vocals",     # voice and everything-else; the rest is waste
                "-n", model,
                "--filename", "{stem}.{ext}",
                "-o", workspace,
                str(audio),
            ],
            capture=False,
            timeout=3600,
        )
        stem = Path(workspace) / model / "vocals.wav"
        if not stem.is_file():
            raise VideoLyricsError(f"demucs produced no vocal stem for {audio.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stem), destination)

    log.info("Vocal stem: %s", destination)
    return destination
