"""Load the reference lyrics from a text file or a Google Doc.

The reference lyrics only ever *confirm wording*.  Which lines become cues, and
when they appear, is decided by the audio transcript (see align.py).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from . import google_drive
from .util import VideoLyricsError, log

SECTION_RE = re.compile(
    r"""^\s*(?:
        \[[^\]]*\]                                   # [Chorus], [Verse 2]
      | \{[^}]*\}
      | (?:pre-?\s*)?(?:verse|chorus|bridge|intro|outro|refrain|tag|hook|interlude|
         instrumental|solo|coda|vamp|ending)
        (?:\s*\d+)?\s*:?                             # Verse 2:
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

TEXT_SUFFIXES = {".txt", ".text", ".md", ".lrc"}


def load_lines(source: Path) -> list[str]:
    """Return the reference lyric lines, in order, one per displayed line."""
    source = Path(source)
    if source.suffix.lower() == ".gdoc":
        doc_id = google_drive.doc_id_from_gdoc(source)
        log.info("Exporting Google Doc %s", doc_id)
        raw = google_drive.export_document(doc_id)
    elif source.suffix.lower() in TEXT_SUFFIXES or source.suffix == "":
        raw = source.read_text(encoding="utf-8", errors="replace")
    else:
        raise VideoLyricsError(
            f"Unsupported lyrics source {source.suffix!r}; use a .txt file or a .gdoc file."
        )
    return clean_lines(raw)


def clean_lines(raw: str) -> list[str]:
    """Normalise a lyric document into displayable lines."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("﻿", "").replace(" ", " ")
    lines: list[str] = []
    for line in raw.split("\n"):
        line = unicodedata.normalize("NFC", line).strip()
        line = re.sub(r"\s+", " ", line)
        if not line:
            continue
        if SECTION_RE.match(line):
            continue
        lines.append(line)
    return lines


def is_section_marker(line: str) -> bool:
    return bool(SECTION_RE.match(line))
