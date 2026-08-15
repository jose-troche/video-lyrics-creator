"""The parts of forced alignment that can be checked without a neural network.

The acoustic model is not on trial here - a made-up emission matrix stands in for
it, saying exactly which letter is being sung in which frame.  What is on trial is
everything around it: spelling the lyrics into the model's alphabet, and finding
the one path through those frames that lays the lyrics onto the audio in order.
"""

import pytest

numpy = pytest.importorskip("numpy")

from video_lyrics import forced
from video_lyrics.util import VideoLyricsError

VOCAB = {"<pad>": 0, "|": 1, "H": 2, "I": 3, "O": 4}
BLANK = 0


class FakeTokenizer:
    """Just enough of a wav2vec2 tokenizer for `_spell`."""

    word_delimiter_token = "|"

    def get_vocab(self):
        return VOCAB


def emission(frames, *, width=len(VOCAB)):
    """A confident model: `frames` names the letter sung in each 100ms frame."""
    scores = numpy.full((len(frames), width), -10.0, dtype="float32")
    for index, letter in enumerate(frames):
        scores[index, VOCAB[letter]] = 0.0     # log(1)
    return scores


def test_lyrics_are_spelled_into_the_alphabet_the_model_knows():
    tokens, owners, words = forced._spell(["Hi, ho!", "Oh"], VOCAB, FakeTokenizer())
    assert tokens == [
        VOCAB["H"], VOCAB["I"],
        VOCAB["|"],
        VOCAB["H"], VOCAB["O"],
        VOCAB["|"],
        VOCAB["O"], VOCAB["H"],
    ]
    # The word delimiters belong to no word, so they never claim any of its time.
    assert owners == [0, 0, -1, 1, 1, -1, 2, 2]
    assert [word["word"] for word in words] == ["Hi,", "ho!", "Oh"]
    assert [word["line_index"] for word in words] == [0, 0, 1]


def test_letters_the_model_has_no_symbol_for_are_dropped():
    tokens, owners, words = forced._spell(["Hi 42"], VOCAB, FakeTokenizer())
    assert [word["word"] for word in words] == ["Hi"]      # "42" spells nothing at all
    assert tokens == [VOCAB["H"], VOCAB["I"]]
    assert owners == [0, 0]


def test_a_word_is_timed_to_the_frames_that_sing_it():
    scores = emission(["<pad>"] * 4 + ["H"] * 4 + ["<pad>"] * 2 + ["I"] * 4 + ["<pad>"] * 6)
    tokens, owners, words = forced._spell(["Hi"], VOCAB, FakeTokenizer())
    path, states = forced._viterbi(scores, tokens, blank=BLANK, numpy=numpy)
    timed = forced._time_words(
        scores, path, states, owners, words, seconds_per_frame=0.1, numpy=numpy,
    )
    assert len(timed) == 1
    assert timed[0]["word"] == "Hi"
    assert timed[0]["start"] == pytest.approx(0.4)   # the first frame of the H
    assert timed[0]["end"] == pytest.approx(1.4)     # ... through the last of the I
    assert timed[0]["probability"] == pytest.approx(1.0)


def test_words_come_back_in_the_order_the_lyrics_have_them():
    scores = emission(
        ["<pad>"] * 2 + ["H"] * 2 + ["|"] + ["H", "O"] + ["<pad>"] * 2 + ["O", "H"] + ["<pad>"] * 3
    )
    tokens, owners, words = forced._spell(["Hi", "ho", "oh"], VOCAB, FakeTokenizer())
    # "Hi" has no I in this audio at all; forced alignment still has to place it, in
    # order, and its score is what gives that away.
    path, states = forced._viterbi(scores, tokens, blank=BLANK, numpy=numpy)
    timed = forced._time_words(
        scores, path, states, owners, words, seconds_per_frame=0.1, numpy=numpy,
    )
    assert [word["word"] for word in timed] == ["Hi", "ho", "oh"]
    starts = [word["start"] for word in timed]
    assert starts == sorted(starts)
    assert timed[0]["probability"] < timed[1]["probability"]


def test_a_doubled_letter_is_kept_apart_by_a_blank():
    # "OO" must not collapse onto one run of O: CTC needs a blank between the two, and
    # the path is only legal if it finds one.
    scores = emission(["O"] * 3 + ["<pad>"] * 2 + ["O"] * 3)
    tokens, owners, words = forced._spell(["Oo"], VOCAB, FakeTokenizer())
    path, states = forced._viterbi(scores, tokens, blank=BLANK, numpy=numpy)
    letters = [int(states[state]) for state in path]
    assert letters.count(VOCAB["O"]) >= 2
    assert BLANK in letters[letters.index(VOCAB["O"]) :]


def test_more_text_than_there_is_audio_is_refused():
    with pytest.raises(VideoLyricsError, match="right lyrics"):
        forced._viterbi(emission(["H"] * 4), [VOCAB["H"]] * 20, blank=BLANK, numpy=numpy)
