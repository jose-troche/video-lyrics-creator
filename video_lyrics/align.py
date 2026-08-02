"""Align reference lyric lines against the audio transcript.

The synchronization rule this module implements:

  * the transcript decides **which** cues exist and **when** they happen;
  * the lyrics file only confirms the **wording** that gets displayed;
  * a reference line the audio never confirms produces no cue at all.

Matching is a monotonic diff (difflib) over normalised word tokens, so repeated
choruses stay in the order they were actually sung.  A second pass then revisits
the stretches of audio that ended up unclaimed: lyric documents often hold drafts,
prose and headings alongside the final words, and in the first pass a half-matching
draft line can swallow the words that belonged to the line that was really sung.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
DEFAULT_WORD_DURATION = 0.32


def normalize(word: str) -> str:
    """Fold a word to its comparison form: lowercase letters and digits only."""
    word = unicodedata.normalize("NFKD", word)
    word = word.replace("’", "'").replace("‘", "'")
    word = "".join(ch for ch in word if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", word.lower())


def tokenize(text: str) -> list[str]:
    return [t for t in (normalize(w) for w in WORD_RE.findall(text)) if t]


def _flatten(lines: Iterable[str]) -> tuple[list[str], list[int]]:
    tokens: list[str] = []
    owners: list[int] = []
    for index, line in enumerate(lines):
        for token in tokenize(line):
            tokens.append(token)
            owners.append(index)
    return tokens, owners


def align(
    lyric_lines: list[str],
    asr_words: list[dict[str, Any]],
    *,
    duration: float | None = None,
    min_confidence: float = 0.5,
    min_matched_words: int = 2,
    min_duration: float = 1.0,
    max_gap_fill: float = 0.7,
) -> list[dict[str, Any]]:
    """Return time-ordered cues: [{start, end, text, line_index, alignment_confidence}]."""
    lyric_tokens, owners = _flatten(lyric_lines)
    asr_tokens_all = [normalize(word["word"]) for word in asr_words]
    keep = [index for index, token in enumerate(asr_tokens_all) if token]
    asr_tokens = [asr_tokens_all[index] for index in keep]
    asr_times = [(float(asr_words[i]["start"]), float(asr_words[i]["end"])) for i in keep]

    if not lyric_tokens or not asr_tokens:
        return []

    tokens_by_line: dict[int, list[str]] = {}
    for token, owner in zip(lyric_tokens, owners):
        tokens_by_line.setdefault(owner, []).append(token)

    # ---- pass 1: one monotonic diff over the whole document -----------------
    matcher = SequenceMatcher(None, lyric_tokens, asr_tokens, autojunk=False)
    line_starts: dict[int, int] = {}
    for index, owner in enumerate(owners):
        line_starts.setdefault(owner, index)

    hits: dict[int, list[int]] = {}
    positions: dict[int, list[int]] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            owner = owners[block.a + offset]
            hits.setdefault(owner, []).append(block.b + offset)
            positions.setdefault(owner, []).append(block.a + offset - line_starts[owner])

    cues: list[dict[str, Any]] = []
    for line_index, indices in hits.items():
        total = len(tokens_by_line[line_index])
        if not _accepts(len(indices), total, min_confidence, min_matched_words):
            continue
        cues.append(
            _build_cue(
                line_index, lyric_lines[line_index], total, positions[line_index],
                indices, asr_times,
            )
        )

    # ---- pass 2: give the unclaimed audio to the lines that fit it ----------
    cues.extend(
        _rescue(
            lyric_lines, tokens_by_line, asr_tokens, asr_times,
            claimed=cues,
            min_confidence=min_confidence,
            min_matched_words=min_matched_words,
        )
    )

    cues.sort(key=lambda cue: (cue["start"], cue["line_index"]))
    return tidy(cues, duration=duration, min_duration=min_duration, max_gap_fill=max_gap_fill)


def _accepts(matched: int, total: int, min_confidence: float, min_matched_words: int) -> bool:
    if total == 0 or matched == 0:
        return False
    if matched < min(min_matched_words, total):
        return False
    return matched / total >= min_confidence


def _build_cue(
    line_index: int,
    text: str,
    total: int,
    matched_positions: list[int],
    asr_indices: list[int],
    asr_times: list[tuple[float, float]],
) -> dict[str, Any]:
    first, last = min(asr_indices), max(asr_indices)
    start = asr_times[first][0]
    end = asr_times[last][1]

    # If the opening or closing words of the line were not heard, the sung line ran a
    # little wider than the confirmed words; estimate that overhang.
    word_duration = (end - start) / max(1, len(asr_indices))
    if not 0.05 <= word_duration <= 1.5:
        word_duration = DEFAULT_WORD_DURATION
    start -= min(matched_positions) * word_duration
    end += (total - 1 - max(matched_positions)) * word_duration

    return {
        "start": round(max(0.0, start), 3),
        "end": round(end, 3),
        "text": text,
        "line_index": line_index,
        "alignment_confidence": round(len(asr_indices) / total, 3),
        "first_word": first,
        "last_word": last,
    }


def _rescue(
    lyric_lines: list[str],
    tokens_by_line: dict[int, list[str]],
    asr_tokens: list[str],
    asr_times: list[tuple[float, float]],
    *,
    claimed: list[dict[str, Any]],
    min_confidence: float,
    min_matched_words: int,
) -> list[dict[str, Any]]:
    """Match still-unused lines against the runs of audio no cue covers yet."""
    used_lines = {cue["line_index"] for cue in claimed}
    spans = sorted((cue["first_word"], cue["last_word"]) for cue in claimed)

    pending: list[tuple[int, int]] = []
    cursor = 0
    for first, last in spans:
        if first > cursor:
            pending.append((cursor, first))
        cursor = max(cursor, last + 1)
    if cursor < len(asr_tokens):
        pending.append((cursor, len(asr_tokens)))

    rescued: list[dict[str, Any]] = []
    while pending:
        low, high = pending.pop()
        if high - low < min_matched_words:
            continue
        window = asr_tokens[low:high]
        best: tuple[tuple[float, int], int, list[int], list[int]] | None = None
        for line_index, tokens in tokens_by_line.items():
            if line_index in used_lines:
                continue
            matcher = SequenceMatcher(None, tokens, window, autojunk=False)
            blocks = [block for block in matcher.get_matching_blocks() if block.size]
            matched = sum(block.size for block in blocks)
            if not _accepts(matched, len(tokens), min_confidence, min_matched_words):
                continue
            indices: list[int] = []
            offsets: list[int] = []
            for block in blocks:
                for step in range(block.size):
                    indices.append(low + block.b + step)
                    offsets.append(block.a + step)
            score = (matched / len(tokens), matched)
            if best is None or score > best[0]:
                best = (score, line_index, offsets, indices)
        if best is None:
            continue
        _score, line_index, offsets, indices = best
        used_lines.add(line_index)
        cue = _build_cue(
            line_index, lyric_lines[line_index], len(tokens_by_line[line_index]),
            offsets, indices, asr_times,
        )
        rescued.append(cue)
        pending.append((low, cue["first_word"]))
        pending.append((cue["last_word"] + 1, high))
    return rescued


def tidy(
    cues: list[dict[str, Any]],
    *,
    duration: float | None = None,
    min_duration: float = 1.0,
    max_gap_fill: float = 0.7,
) -> list[dict[str, Any]]:
    """Remove overlaps, enforce a minimum on-screen time, close small gaps."""
    if not cues:
        return []

    for index, cue in enumerate(cues):
        cue.pop("first_word", None)
        cue.pop("last_word", None)
        following = cues[index + 1]["start"] if index + 1 < len(cues) else duration
        ceiling = following if following is not None else cue["end"]

        if cue["end"] > ceiling:
            cue["end"] = ceiling
        if cue["end"] - cue["start"] < min_duration:
            cue["end"] = min(cue["start"] + min_duration, ceiling)
        gap = ceiling - cue["end"]
        if 0 < gap <= max_gap_fill:
            cue["end"] = ceiling
        if duration is not None:
            cue["start"] = max(0.0, min(cue["start"], duration))
            cue["end"] = max(cue["start"], min(cue["end"], duration))
        cue["start"] = round(cue["start"], 3)
        cue["end"] = round(cue["end"], 3)

    return [cue for cue in cues if cue["end"] > cue["start"]]


def report(lyric_lines: list[str], cues: list[dict[str, Any]]) -> str:
    matched = {cue["line_index"] for cue in cues}
    missing = [line for index, line in enumerate(lyric_lines) if index not in matched]
    lines = [f"{len(cues)} of {len(lyric_lines)} reference lines confirmed by the audio."]
    if missing:
        lines.append("Not confirmed (no cue created):")
        lines.extend(f"  - {line}" for line in missing[:20])
        if len(missing) > 20:
            lines.append(f"  ... and {len(missing) - 20} more")
    return "\n".join(lines)
