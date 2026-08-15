"""Forced alignment: fitting the lyrics we already have onto the audio.

Transcription asks the recording what was sung, and then has to be argued with -
a diff against the lyrics sheet, a rescue pass, a confidence threshold - because
half of what it says is a guess about words we were never in doubt about.  Forced
alignment asks the narrower question that is actually ours: *given* that this is
what was sung, when was each word?

An acoustic model (wav2vec2, a CTC model) scores every 20ms frame of audio against
every letter it knows.  A Viterbi pass then walks the lyrics through those scores
along the single best path that spells them in order - never skipping a word, never
going back, letting a letter last as long as it is held.  What comes out is a list
of timed words in exactly the shape a transcript has, so nothing downstream can
tell the difference; the words are simply the lyrics, spelled the way the lyrics
spell them, each carrying a score for how well the audio bore it out.

That score is the one thing to watch.  Forced alignment is *forced*: give it a
heading, a stage direction or a discarded draft and it will place that too, somewhere,
because it is not allowed to refuse.  What it cannot do is make the audio agree, so
those words come back with poor scores; `min_score` drops them, and a line that
loses its words that way falls below `align`'s confidence bar and produces no cue -
which is the same rule the transcript engine lives by, arrived at differently.

Needs the optional extra:  pip install -e ".[align]"
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, log, run, which

SAMPLE_RATE = 16000       # what wav2vec2 was trained on; not negotiable
CHUNK = 30.0              # seconds of audio per forward pass - attention is quadratic,
                          # so a whole song at once is neither fast nor possible
DEFAULT_MODEL = "facebook/wav2vec2-base-960h"
NEGATIVE = -1e30          # "no path reaches this state"


def _require() -> tuple[Any, Any, Any]:
    try:
        import numpy
        import torch
        from transformers import AutoModelForCTC, AutoProcessor
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise VideoLyricsError(
            "Forced alignment needs torch and transformers. Run: pip install -e '.[align]' "
            "(or go back to the transcript with: video-lyrics set alignment.engine whisper)"
        ) from exc
    return numpy, torch, (AutoModelForCTC, AutoProcessor)


def align_words(
    audio: Path,
    lines: list[str],
    *,
    model: str = DEFAULT_MODEL,
    min_score: float = 0.05,
    chunk: float = CHUNK,
) -> dict[str, Any]:
    """Time every word of `lines` against `audio`; returns a transcript payload."""
    numpy, torch, (model_class, processor_class) = _require()

    processor = _load(processor_class, model)
    tokenizer = processor.tokenizer
    vocabulary = tokenizer.get_vocab()
    blank = tokenizer.pad_token_id or 0

    tokens, owners, spoken = _spell(lines, vocabulary, tokenizer)
    if not tokens:
        raise VideoLyricsError(
            "None of the lyrics could be spelled with the letters this model knows; "
            f"is {model} trained on the right language?"
        )

    samples = _decode(audio)
    emission, seconds_per_frame = _emissions(
        samples, model=model, model_class=model_class, processor=processor,
        chunk=chunk, torch=torch,
    )
    log.info(
        "Aligning %d words (%d letters) over %d frames of audio ...",
        len(spoken), len(tokens), len(emission),
    )
    path, states = _viterbi(emission, tokens, blank=blank, numpy=numpy)
    words = _time_words(
        emission, path, states, owners, spoken,
        seconds_per_frame=seconds_per_frame, numpy=numpy,
    )

    kept = [word for word in words if word["probability"] >= min_score]
    dropped = len(words) - len(kept)
    if dropped:
        log.info("The audio does not bear out %d of %d words; dropping them.",
                 dropped, len(words))
    log.info("Aligned %d words.", len(kept))
    return {
        "engine": "forced",
        "model": model,
        "language": None,
        "duration": round(len(samples) / SAMPLE_RATE, 3),
        "segments": _segments(kept, lines),
        "words": kept,
    }


# ---- the lyrics, as letters the model knows ---------------------------------

def _spell(
    lines: list[str], vocabulary: dict[str, int], tokenizer: Any
) -> tuple[list[int], list[int], list[dict[str, Any]]]:
    """Lyrics -> (token ids, the word each token belongs to, the words themselves).

    Word boundaries are a token of their own ("|" in every wav2vec2 vocabulary): the
    model was trained to emit one between words, and leaving them out blurs exactly
    the boundaries we are here to find.  They own no word, so they belong to no span.
    """
    # Which case the model spells in is its own business: wav2vec2's English models
    # are upper, the multilingual ones lower.  Ask the alphabet rather than assume.
    alphabet = [key for key in vocabulary if len(key) == 1 and key.isalpha()]
    upper = sum(key.isupper() for key in alphabet) >= sum(key.islower() for key in alphabet)
    delimiter = vocabulary.get(getattr(tokenizer, "word_delimiter_token", "|") or "|")

    tokens: list[int] = []
    owners: list[int] = []
    words: list[dict[str, Any]] = []
    missing: set[str] = set()

    for line_index, line in enumerate(lines):
        for match in re.finditer(r"\S+", line):
            text = match.group()
            letters = re.sub(r"[^\w']", "", text.replace("’", "'"), flags=re.UNICODE)
            letters = letters.upper() if upper else letters.lower()
            ids = [vocabulary[c] for c in letters if c in vocabulary]
            missing.update(c for c in letters if c not in vocabulary)
            if not ids:
                continue
            if tokens and delimiter is not None:
                tokens.append(delimiter)
                owners.append(-1)
            index = len(words)
            tokens.extend(ids)
            owners.extend([index] * len(ids))
            words.append({"word": text, "line_index": line_index})

    if missing:
        log.debug("Letters this model has no symbol for, skipped: %s",
                  " ".join(sorted(missing)))
    return tokens, owners, words


# ---- the audio, as frame scores ---------------------------------------------

def _decode(audio: Path) -> Any:
    """The song as 16kHz mono floats, straight out of ffmpeg."""
    import numpy

    audio = Path(audio)
    if not audio.is_file():
        raise VideoLyricsError(f"Audio file not found: {audio}")
    proc = run(
        [
            which("ffmpeg"), "-v", "error", "-i", str(audio),
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        ],
        binary=True,
    )
    raw = numpy.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype="<i2")
    if not len(raw):
        raise VideoLyricsError(f"ffmpeg decoded no audio from {audio}")
    return raw.astype("float32") / 32768.0


def _load(loader: Any, model: str) -> Any:
    """Load from the local cache first; see the same trick in `transcribe`."""
    try:
        return loader.from_pretrained(model, local_files_only=True)
    except Exception:  # not cached yet - a real problem surfaces on the retry
        log.info("Fetching the %s weights; this only happens once.", model)
        return loader.from_pretrained(model)


def _emissions(
    samples: Any, *, model: str, model_class: Any, processor: Any,
    chunk: float, torch: Any,
) -> tuple[Any, float]:
    """Per-frame log-probabilities for the whole song, and how long a frame is.

    The song goes through in windows because attention costs the square of its input,
    and the windows are simply concatenated afterwards: every frame is scored from a
    receptive field of well under a second, so the joins cost nothing that matters.
    """
    network = _load(model_class, model)
    network.eval()

    window = int(chunk * SAMPLE_RATE)
    parts = []
    with torch.inference_mode():
        for start in range(0, len(samples), window):
            block = samples[start : start + window]
            if len(block) < SAMPLE_RATE // 20:   # a scrap too short to score
                continue
            values = processor.feature_extractor(
                block, sampling_rate=SAMPLE_RATE, return_tensors="pt"
            ).input_values
            logits = network(values).logits[0]
            parts.append(torch.log_softmax(logits, dim=-1))
            log.debug("  ... scored %.0fs", (start + len(block)) / SAMPLE_RATE)

    emission = torch.cat(parts).numpy().astype("float32")
    # Measured, not assumed: the stride is the model's business, and this holds
    # whatever it happens to be.
    return emission, len(samples) / SAMPLE_RATE / len(emission)


# ---- the one path that spells the lyrics ------------------------------------

def _viterbi(emission: Any, tokens: list[int], *, blank: int, numpy: Any) -> tuple[Any, Any]:
    """The single best path through the audio that spells `tokens`, in order.

    CTC lets a letter last for many frames and allows - and between a doubled letter,
    requires - a blank in between, so the states are the letters with blanks woven
    through them.  From one frame to the next a path may stay where it is, step on
    one state, or skip a blank it does not need; that is the whole grammar, and it is
    what makes the result monotonic by construction.  Only the choice made at each
    state is kept, which is what lets a five minute song fit in memory.
    """
    states = numpy.empty(2 * len(tokens) + 1, dtype="int64")
    states[0::2] = blank
    states[1::2] = tokens
    total, width = len(emission), len(states)
    if total < width // 2:
        raise VideoLyricsError(
            "There is more text than there is audio to sing it in "
            f"({len(tokens)} letters, {total} frames); are these the right lyrics?"
        )

    # A blank may be skipped, but only into a letter that is not a repeat of the one
    # before it - otherwise "letter, letter" and "letter" would be the same path.
    skippable = numpy.zeros(width, dtype=bool)
    skippable[2:] = (states[2:] != blank) & (states[2:] != states[:-2])

    alpha = numpy.full(width, NEGATIVE, dtype="float32")
    alpha[0] = emission[0, states[0]]
    if width > 1:
        alpha[1] = emission[0, states[1]]
    back = numpy.zeros((total, width), dtype="int8")

    for frame in range(1, total):
        stepped = numpy.empty(width, dtype="float32")
        stepped[0] = NEGATIVE
        stepped[1:] = alpha[:-1]
        skipped = numpy.full(width, NEGATIVE, dtype="float32")
        skipped[2:] = numpy.where(skippable[2:], alpha[:-2], NEGATIVE)

        best = numpy.maximum(numpy.maximum(alpha, stepped), skipped)
        back[frame] = numpy.where(best == alpha, 0, numpy.where(best == stepped, 1, 2))
        alpha = best + emission[frame, states]

    # The song may end on the last letter or on the blank after it.
    state = width - 1 if width < 2 or alpha[width - 1] >= alpha[width - 2] else width - 2
    path = numpy.empty(total, dtype="int64")
    for frame in range(total - 1, -1, -1):
        path[frame] = state
        state -= int(back[frame, state])
    return path, states


def _time_words(
    emission: Any, path: Any, states: Any, owners: list[int], words: list[dict[str, Any]],
    *, seconds_per_frame: float, numpy: Any,
) -> list[dict[str, Any]]:
    """Turn the path into timed words: when each one starts, ends, and how sure it is."""
    letters = numpy.full(len(states), -1, dtype="int64")
    letters[1::2] = owners                      # blanks stay -1, owned by no word
    per_frame = letters[path]

    scores = numpy.exp(emission[numpy.arange(len(path)), states[path]])
    timed: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        frames = numpy.flatnonzero(per_frame == index)
        if not len(frames):                     # squeezed out entirely; nothing to show
            continue
        first, last = int(frames[0]), int(frames[-1])
        timed.append(
            {
                "word": word["word"],
                "start": round(first * seconds_per_frame, 3),
                "end": round((last + 1) * seconds_per_frame, 3),
                "probability": round(float(scores[frames].mean()), 3),
                "line_index": word["line_index"],
            }
        )
    return timed


def _segments(words: list[dict[str, Any]], lines: list[str]) -> list[dict[str, Any]]:
    """One segment per lyric line - only ever read by a human debugging a timing."""
    segments: list[dict[str, Any]] = []
    for word in words:
        line_index = word["line_index"]
        if segments and segments[-1]["line_index"] == line_index:
            segments[-1]["end"] = word["end"]
            continue
        segments.append(
            {
                "start": word["start"],
                "end": word["end"],
                "text": lines[line_index],
                "line_index": line_index,
            }
        )
    return segments
