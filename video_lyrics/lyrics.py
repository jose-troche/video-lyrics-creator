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
    lines, _sections = load_lines_with_sections(source)
    return lines


def load_lines_with_sections(source: Path) -> tuple[list[str], set[int]]:
    """As `load_lines`, plus the indices of lines that start a new section.

    A section starts at the first line of the song and at the first line after
    a section break: either an explicit marker such as `[Chorus]` or `Verse 2:`,
    or - since not every verse is labelled - a blank line, the way a stanza is
    always set off from the one after it. Used so an image is never built from
    lines either side of a section break.
    """
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
    return clean_lines_with_sections(raw)


def clean_lines(raw: str) -> list[str]:
    """Normalise a lyric document into displayable lines."""
    lines, _sections = clean_lines_with_sections(raw)
    return lines


def clean_lines_with_sections(raw: str) -> tuple[list[str], set[int]]:
    """As `clean_lines`, plus the indices of lines that start a new section."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("﻿", "").replace(" ", " ")
    lines: list[str] = []
    section_starts: set[int] = set()
    at_boundary = True
    for line in raw.split("\n"):
        line = unicodedata.normalize("NFC", line).strip()
        line = re.sub(r"\s+", " ", line)
        if not line:
            at_boundary = True
            continue
        if SECTION_RE.match(line):
            at_boundary = True
            continue
        if at_boundary:
            section_starts.add(len(lines))
            at_boundary = False
        lines.append(line)
    return lines, section_starts


def is_section_marker(line: str) -> bool:
    return bool(SECTION_RE.match(line))
