from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .errors import VideoLyricsError


@dataclass(frozen=True)
class TimedWord:
    text: str
    start: float
    end: float


def read_lyrics(path: str | Path, *, env_dir: str | Path | None = None) -> list[str]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise VideoLyricsError(f"Lyrics file does not exist: {source}")
    is_google_doc = source.suffix.lower() == ".gdoc"
    if is_google_doc:
        from .google_drive import export_gdoc_text

        text = export_gdoc_text(source, env_dir=env_dir)
    else:
        text = source.read_text(encoding="utf-8-sig")
    lines = [_clean_lyric_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line is not None]
    if not lines:
        raise VideoLyricsError(f"Lyrics source is empty: {source}")
    return lines


def _clean_lyric_line(line: str) -> str | None:
    without_annotations = re.sub(r"\[[^\]]*\]", "", line)
    collapsed = re.sub(r"[ \t]+", " ", without_annotations).strip()
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", collapsed)
    if not cleaned or re.fullmatch(r"[_\s]+", cleaned):
        return None
    return cleaned


def parse_timing_file(path: str | Path) -> list[dict]:
    source = Path(path)
    text = source.read_text(encoding="utf-8-sig")
    suffix = source.suffix.lower()
    if suffix == ".srt":
        return _parse_srt(text)
    if suffix == ".lrc":
        return _parse_lrc(text)
    raise VideoLyricsError("Timing file must be .srt or .lrc")


def apply_canonical_lines(cues: list[dict], lines: list[str], duration: float) -> list[dict]:
    if len(cues) != len(lines):
        raise VideoLyricsError(
            f"Timing file has {len(cues)} cues but lyrics file has {len(lines)} non-empty lines"
        )
    result = []
    for cue, line in zip(cues, lines):
        result.append({"start": float(cue["start"]), "end": float(cue["end"]), "text": line})
    return _clean_cues(result, duration)


def transcribe_words(audio: str | Path, model: str = "small", device: str = "auto") -> list[TimedWord]:
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VideoLyricsError(
            "Automatic alignment requires faster-whisper. Install with "
            "`python -m pip install -e '.[align]'`, or pass --timings with an SRT/LRC file."
        ) from exc

    selected_device = device
    if selected_device == "auto":
        selected_device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
    compute_type = "float16" if selected_device == "cuda" else "int8"
    whisper = WhisperModel(model, device=selected_device, compute_type=compute_type)
    segments, _ = whisper.transcribe(
        str(audio), word_timestamps=True, vad_filter=False, condition_on_previous_text=True
    )
    words: list[TimedWord] = []
    for segment in segments:
        for word in segment.words or []:
            if word.start is not None and word.end is not None and word.word.strip():
                words.append(TimedWord(word.word.strip(), float(word.start), float(word.end)))
    if not words:
        raise VideoLyricsError("Speech recognition produced no timed words")
    return words


def align_lines(lines: list[str], words: list[TimedWord], duration: float) -> list[dict]:
    """Use ASR evidence to select lines, then retain their reviewed wording.

    The dynamic-programming pass tolerates insertions and recognition errors while
    retaining the exact user-supplied lyric text in the final cues. Reference lines
    with no matching audio words are omitted instead of receiving invented timings.
    """
    canonical: list[str] = []
    line_ranges: list[tuple[int, int]] = []
    for line in lines:
        start = len(canonical)
        canonical.extend(_tokens(line))
        line_ranges.append((start, len(canonical)))
    observed = [_normalize(word.text) for word in words]
    if not canonical or not any(observed):
        raise VideoLyricsError("Lyrics or transcription contained no alignable words")

    matches = _global_alignment(canonical, observed)
    cues = []
    for line, (token_start, token_end) in zip(lines, line_ranges):
        mapped = [matches.get(index) for index in range(token_start, token_end)]
        mapped = [index for index in mapped if index is not None]
        if not mapped:
            continue
        start = words[min(mapped)].start
        end = words[max(mapped)].end
        confidence = len(mapped) / max(1, token_end - token_start)
        cues.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": line,
                "alignment_confidence": round(confidence, 3),
            }
        )
    if not cues:
        raise VideoLyricsError(
            "The audio transcription did not confirm any lines from the lyrics source"
        )
    return _clean_cues(cues, duration)


def _tokens(text: str) -> list[str]:
    return [token for token in (_normalize(item) for item in text.split()) if token]


def _normalize(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.casefold(), flags=re.UNICODE)


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _global_alignment(canonical: list[str], observed: list[str]) -> dict[int, int]:
    rows, cols = len(canonical) + 1, len(observed) + 1
    gap = -0.65
    scores = [[0.0] * cols for _ in range(rows)]
    moves = [[0] * cols for _ in range(rows)]  # 1 diagonal, 2 up, 3 left
    for i in range(1, rows):
        scores[i][0] = i * gap
        moves[i][0] = 2
    for j in range(1, cols):
        scores[0][j] = j * gap
        moves[0][j] = 3
    for i in range(1, rows):
        for j in range(1, cols):
            ratio = _similarity(canonical[i - 1], observed[j - 1])
            pair_score = 2.0 if ratio == 1.0 else (0.8 if ratio >= 0.72 else -1.0)
            choices = (
                (scores[i - 1][j - 1] + pair_score, 1),
                (scores[i - 1][j] + gap, 2),
                (scores[i][j - 1] + gap, 3),
            )
            scores[i][j], moves[i][j] = max(choices, key=lambda item: item[0])

    mapping: dict[int, int] = {}
    i, j = rows - 1, cols - 1
    while i or j:
        move = moves[i][j]
        if move == 1:
            if _similarity(canonical[i - 1], observed[j - 1]) >= 0.72:
                mapping[i - 1] = j - 1
            i -= 1
            j -= 1
        elif move == 2:
            i -= 1
        else:
            j -= 1
    return mapping


def _clean_cues(cues: list[dict], duration: float) -> list[dict]:
    cues.sort(key=lambda item: float(item["start"]))
    merged: list[dict] = []
    for cue in cues:
        if merged and abs(float(cue["start"]) - float(merged[-1]["start"])) <= 0.01:
            previous = merged[-1]
            previous["text"] = f"{previous['text']} / {cue['text']}"
            previous["end"] = max(float(previous["end"]), float(cue["end"]))
            if "alignment_confidence" in previous or "alignment_confidence" in cue:
                previous["alignment_confidence"] = round(
                    min(
                        float(previous.get("alignment_confidence", 1.0)),
                        float(cue.get("alignment_confidence", 1.0)),
                    ),
                    3,
                )
            continue
        merged.append(dict(cue))

    result: list[dict] = []
    for index, cue in enumerate(merged):
        start = max(0.0, float(cue["start"]))
        end = min(duration, float(cue["end"]))
        if index + 1 < len(merged):
            next_start = max(0.0, float(merged[index + 1]["start"]))
            end = min(end, next_start)
        if end <= start:
            end = min(duration, start + 0.1)
        cleaned = {**cue, "start": round(start, 3), "end": round(end, 3)}
        result.append(cleaned)
    return result


def _parse_clock(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = "0", *parts
    else:
        raise ValueError(value)
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_srt(text: str) -> list[dict]:
    blocks = re.split(r"\n\s*\n", text.strip())
    cues = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start_text, end_text = [part.strip() for part in lines[timing_index].split("-->", 1)]
        cues.append(
            {
                "start": _parse_clock(start_text),
                "end": _parse_clock(end_text.split()[0]),
                "text": " ".join(lines[timing_index + 1 :]),
            }
        )
    if not cues:
        raise VideoLyricsError("SRT file contained no cues")
    return cues


def _parse_lrc(text: str) -> list[dict]:
    starts: list[tuple[float, str]] = []
    pattern = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            starts.append((int(match.group(1)) * 60 + float(match.group(2)), match.group(3).strip()))
    if not starts:
        raise VideoLyricsError("LRC file contained no timed lines")
    cues = []
    for index, (start, lyric) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else start + 5.0
        cues.append({"start": start, "end": end, "text": lyric})
    return cues
