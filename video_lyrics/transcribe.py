"""Audio transcription with word-level timestamps (faster-whisper).

The transcript is cached in the work directory; it is the expensive step and the
alignment stage is re-run often while tuning.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, log


def _load(model_class: Any, model: str, compute_type: str) -> Any:
    """Load the weights from the local cache, reaching for the network only on a miss.

    The weights are already cached under ~/.cache/huggingface, but a plain load still
    calls Hugging Face on every run to compare revisions: a wait, a wall of progress
    bars, and an outright failure offline, all for files that are sitting on disk.
    """
    try:
        return model_class(model, device="auto", compute_type=compute_type, local_files_only=True)
    except Exception:  # not in the cache yet - any other problem resurfaces below
        log.info("Fetching the %s weights; this only happens once.", model)
        return model_class(model, device="auto", compute_type=compute_type)


def transcribe(
    audio: Path,
    *,
    model: str = "medium.en",
    language: str | None = "en",
    initial_prompt: str | None = None,
    compute_type: str = "auto",
    vad: bool = False,
) -> dict[str, Any]:
    """Return {"words": [{word, start, end}], "segments": [...], "model": ...}.

    Voice activity detection is off by default: on a sung, fully mixed track it
    throws away most of the vocal and the transcript comes back nearly empty.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise VideoLyricsError(
            "faster-whisper is not installed. Run: pip install -e '.[dev]'"
        ) from exc

    log.info("Transcribing %s with faster-whisper %s ...", audio.name, model)
    whisper = _load(WhisperModel, model, compute_type)
    segments, info = whisper.transcribe(
        str(audio),
        language=language,
        word_timestamps=True,
        vad_filter=vad,
        vad_parameters={"min_silence_duration_ms": 500} if vad else None,
        beam_size=5,
        condition_on_previous_text=False,
        # Long instrumental stretches are where Whisper invents lines; this makes it
        # drop segments it only "heard" during silence.
        hallucination_silence_threshold=2.0,
        initial_prompt=initial_prompt,
    )

    words: list[dict[str, Any]] = []
    plain_segments: list[dict[str, Any]] = []
    for segment in segments:
        plain_segments.append(
            {"start": segment.start, "end": segment.end, "text": segment.text.strip()}
        )
        for word in segment.words or []:
            text = (word.word or "").strip()
            if not text:
                continue
            words.append(
                {
                    "word": text,
                    "start": float(word.start),
                    "end": float(word.end),
                    "probability": float(getattr(word, "probability", 1.0) or 0.0),
                }
            )
        if len(plain_segments) % 10 == 0:
            log.debug("  ... %d segments, %.1fs", len(plain_segments), segment.end)

    log.info("Transcribed %d words in %d segments.", len(words), len(plain_segments))
    return {
        "model": model,
        "language": getattr(info, "language", language),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
        "segments": plain_segments,
        "words": words,
    }


def load_or_create(
    audio: Path,
    cache: Path,
    *,
    model: str = "medium.en",
    language: str | None = "en",
    initial_prompt: str | None = None,
    vad: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    cache = Path(cache)
    if cache.is_file() and not force:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if payload.get("model") == model and payload.get("words"):
            log.info("Reusing cached transcript (%d words).", len(payload["words"]))
            return payload
    payload = transcribe(
        audio, model=model, language=language, initial_prompt=initial_prompt, vad=vad
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return payload
