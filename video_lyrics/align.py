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

Two last passes put each line back on the note it is actually sung on: `catch_attacks`
hands it back the attack the aligner was late for, and `hold_tails` the tail its singer
is still holding.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

from .audio import ENVELOPE_RESOLUTION
from .util import log

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
    energy: list[float] | None = None,
    tail_extend: float = 0.0,
    tail_level: float = 0.45,
    attack_reach: float = 0.0,
    attack_level: float = 0.45,
    rescue: bool = True,
) -> list[dict[str, Any]]:
    """Return time-ordered cues: [{start, end, text, line_index, alignment_confidence}].

    `energy` is the song's loudness envelope (`audio.envelope`); given one, a line
    starts where its first note does and is held for as long as its last one is - see
    `catch_attacks` and `hold_tails`.

    `rescue` is the second pass, and belongs to transcripts.  Words that were forced
    onto the lyrics are already in the lyrics' own order and spelling, so the first
    pass has nothing left to get wrong - while the second pass, which is free to
    reach anywhere in the song for a line the first pass missed, will happily take a
    line the singer never sang and pin it on some far-off repeat of the same words.
    """
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
    if rescue:
        cues.extend(
            _rescue(
                lyric_lines, tokens_by_line, asr_tokens, asr_times,
                claimed=cues,
                min_confidence=min_confidence,
                min_matched_words=min_matched_words,
            )
        )

    cues.sort(key=lambda cue: (cue["start"], cue["line_index"]))
    catch_attacks(cues, energy, reach=attack_reach, level=attack_level)
    hold_tails(cues, energy, limit=tail_extend, level=tail_level)
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


def catch_attacks(
    cues: list[dict[str, Any]],
    energy: list[float] | None,
    *,
    resolution: int = ENVELOPE_RESOLUTION,
    reach: float = 0.0,
    level: float = 0.45,
) -> int:
    """Start a line where the voice comes in, not where the aligner first heard it.
    Returns how many moved.

    An aligner is late to a note it is perfectly sure of.  A CTC model has to hear
    enough of a letter before it will commit to it, and on a word opening with a vowel
    or a soft consonant - "In the end", "our God" - it commits a third of a second
    after the singer opened his mouth.  Whisper is later still.  The line then goes up
    on a phrase that is already under way, which is exactly the lag that reads as being
    out of time; the lead-in (`video.lyric_lead`) is spent absorbing it instead of
    giving the reader a head start.

    The envelope knows the attack to a hundredth of a second, so walk *back* from the
    aligner's start while the sound is still within `level` of how loud the line is,
    and stop where it falls away: the first bucket after that quiet is where the line
    begins.  A start already sitting on the note steps back a bucket or two and stops.

    Finding the quiet is the whole safety of this, and a line that never does is left
    alone - whether it ran out of `reach` or straight back into the line before it.  On
    a full mix that is nearly every line: the band plays through the breath before each
    one and there is no attack there to see.  Like `hold_tails`, this reads on an
    isolated vocal (`alignment.vocals`).
    """
    if not energy or reach <= 0:
        return 0

    caught = 0
    previous_end = 0
    for cue in cues:
        first = int(cue["start"] * resolution)
        last = int(cue["end"] * resolution)
        span = energy[first:last]
        # The median, for the same reason `hold_tails` uses one: the level the line
        # sits at, not the loudest moment in it.
        floor = sorted(span)[len(span) // 2] * level if span else 0.0
        # How far back the walk may go: the reach, and never over the line before.
        limit = max(previous_end, first - int(reach * resolution), 0)
        previous_end = last
        if floor <= 0:
            continue

        edge = first
        while edge > limit and energy[edge - 1] >= floor:
            edge -= 1
        if edge <= limit < first:      # never went quiet: no attack to be found here
            continue

        start = round(edge / resolution, 3)
        if start < cue["start"]:
            cue["start"] = start
            caught += 1

    if caught:
        log.info("Moved %d line%s back onto the note it starts on.",
                 caught, "" if caught == 1 else "s")
    return caught


def hold_tails(
    cues: list[dict[str, Any]],
    energy: list[float] | None,
    *,
    resolution: int = ENVELOPE_RESOLUTION,
    limit: float = 0.0,
    level: float = 0.45,
) -> int:
    """Give a line back the tail its singer is still holding.  Returns how many.

    A word is over, as far as the transcript is concerned, once its consonant is: a
    line sung out on a long vowel is marked finished while the note is still ringing,
    and the words leave the screen early.  The loudness envelope still knows the note
    is there, so walk on from the end of the cue while the sound stays within `level`
    of how loud the line itself was, and stop the moment it falls away.

    That test is the whole safety of this: a line that really did stop where the
    transcript says drops below the threshold at once and is left exactly as it was.
    It reads a great deal better on an isolated vocal (`alignment.vocals`), where the
    band is not there to hold the level up on its own.
    """
    if not energy or limit <= 0:
        return 0

    held = 0
    for cue in cues:
        first = int(cue["start"] * resolution)
        last = int(cue["end"] * resolution)
        span = energy[first:last]
        if not span:
            continue
        # The median, not the peak: what matters is the level the line sat at, and a
        # single drum hit inside it should not set the bar for the note that follows.
        floor = sorted(span)[len(span) // 2] * level
        if floor <= 0:
            continue

        ceiling = min(last + int(limit * resolution), len(energy))
        edge = last
        while edge < ceiling and energy[edge] >= floor:
            edge += 1
        end = max(cue["end"], round(edge / resolution, 3))
        if end > cue["end"]:
            cue["end"] = end
            held += 1

    if held:
        log.info("Held %d line%s open while the note was still sounding.",
                 held, "" if held == 1 else "s")
    return held


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
