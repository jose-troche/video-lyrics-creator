from video_lyrics import align


def words(*pairs):
    """(text, start, end) triples -> transcript word dicts."""
    return [{"word": text, "start": start, "end": end} for text, start, end in pairs]


def test_normalize_folds_case_punctuation_and_curly_apostrophes():
    assert align.normalize("World’s,") == "worlds"
    assert align.normalize("Héart!") == "heart"
    assert align.tokenize("I walked the world's ways") == [
        "i", "walked", "the", "worlds", "ways",
    ]


def test_only_lines_confirmed_by_audio_become_cues():
    lines = ["Once I was dead", "This line is never sung", "But Christ is rich in mercy"]
    transcript = words(
        ("Once", 1.0, 1.3), ("I", 1.3, 1.5), ("was", 1.5, 1.8), ("dead", 1.8, 2.2),
        ("But", 5.0, 5.2), ("Christ", 5.2, 5.6), ("is", 5.6, 5.8),
        ("rich", 5.8, 6.1), ("in", 6.1, 6.3), ("mercy", 6.3, 6.9),
    )
    cues = align.align(lines, transcript, duration=10.0)
    assert [cue["text"] for cue in cues] == [
        "Once I was dead", "But Christ is rich in mercy",
    ]
    assert cues[0]["start"] == 1.0
    assert cues[1]["end"] == 6.9
    assert all(cue["alignment_confidence"] == 1.0 for cue in cues)


def test_partially_heard_line_keeps_its_reference_wording():
    lines = ["My cravings ruled they were my guide"]
    transcript = words(
        ("my", 2.0, 2.2), ("cravings", 2.2, 2.6), ("ruled", 2.6, 3.0),
        ("they", 3.0, 3.2), ("were", 3.2, 3.4), ("mai", 3.4, 3.6), ("guide", 3.6, 4.0),
    )
    cues = align.align(lines, transcript, duration=10.0)
    assert len(cues) == 1
    assert cues[0]["text"] == "My cravings ruled they were my guide"
    assert 0.5 < cues[0]["alignment_confidence"] < 1.0


def test_repeated_chorus_lines_stay_in_sung_order():
    lines = ["Grace alone", "Grace alone"]
    transcript = words(
        ("grace", 1.0, 1.4), ("alone", 1.4, 2.0),
        ("grace", 8.0, 8.4), ("alone", 8.4, 9.0),
    )
    cues = align.align(lines, transcript, duration=12.0)
    assert len(cues) == 2
    assert cues[0]["start"] < cues[1]["start"]
    assert cues[0]["end"] <= cues[1]["start"]


def test_cues_never_overlap_and_respect_the_minimum_duration():
    lines = ["one", "two"]
    transcript = words(("one", 1.0, 1.1), ("two", 1.4, 1.5))
    cues = align.align(lines, transcript, duration=6.0, min_confidence=0.5, min_duration=2.0)
    assert cues[0]["end"] <= cues[1]["start"]
    assert cues[1]["end"] - cues[1]["start"] >= 2.0


def test_small_gaps_are_closed_but_long_ones_are_kept():
    cues = [
        {"start": 0.0, "end": 1.0, "text": "a", "line_index": 0},
        {"start": 1.4, "end": 2.0, "text": "b", "line_index": 1},
        {"start": 9.0, "end": 10.0, "text": "c", "line_index": 2},
    ]
    tidied = align.tidy(cues, duration=12.0, min_duration=0.5, max_gap_fill=0.7)
    assert tidied[0]["end"] == 1.4          # 0.4s gap closed
    assert tidied[1]["end"] == 2.0          # 7s gap left alone
    assert tidied[2]["end"] == 10.0


def envelope(*levels, resolution=100):
    """A loudness envelope from (seconds, level) runs."""
    peaks = []
    for seconds, level in levels:
        peaks.extend([level] * int(seconds * resolution))
    return peaks


def test_a_held_note_keeps_its_line_on_screen():
    # Sung 1.0-2.0s, but the singer holds the last vowel out to 2.8s.
    cues = [{"start": 1.0, "end": 2.0, "text": "held", "line_index": 0}]
    held = align.hold_tails(cues, envelope((1.0, 0.0), (1.8, 0.8), (5.0, 0.02)), limit=1.2)
    assert held == 1
    assert cues[0]["end"] == 2.8


def test_a_line_that_really_stopped_is_left_alone():
    cues = [{"start": 1.0, "end": 2.0, "text": "clipped", "line_index": 0}]
    assert align.hold_tails(cues, envelope((1.0, 0.0), (1.0, 0.8), (5.0, 0.02)), limit=1.2) == 0
    assert cues[0]["end"] == 2.0


def test_a_tail_is_never_held_past_the_limit():
    cues = [{"start": 1.0, "end": 2.0, "text": "endless", "line_index": 0}]
    align.hold_tails(cues, envelope((1.0, 0.0), (9.0, 0.8)), limit=1.2)
    assert cues[0]["end"] == 3.2


def test_holding_tails_is_off_without_an_envelope_or_a_limit():
    cues = [{"start": 1.0, "end": 2.0, "text": "held", "line_index": 0}]
    full = envelope((1.0, 0.0), (9.0, 0.8))
    assert align.hold_tails(cues, full, limit=0.0) == 0
    assert align.hold_tails(cues, None, limit=1.2) == 0
    assert cues[0]["end"] == 2.0


def test_a_held_tail_still_stops_at_the_next_line():
    lines = ["one", "two"]
    transcript = words(("one", 1.0, 2.0), ("two", 2.5, 3.0))
    cues = align.align(
        lines, transcript, duration=8.0,
        energy=envelope((1.0, 0.0), (7.0, 0.8)), tail_extend=1.2, min_matched_words=1,
    )
    assert cues[0]["end"] <= cues[1]["start"]


def test_no_transcript_means_no_cues():
    assert align.align(["anything"], [], duration=5.0) == []
