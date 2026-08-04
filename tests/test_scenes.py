from video_lyrics import scenes


def cue(start, end, text, line_index=0):
    return {"start": start, "end": end, "text": text, "line_index": line_index}


BASIC = [
    cue(10.0, 12.0, "line one"),
    cue(12.0, 14.0, "line two"),
    cue(14.0, 16.0, "line three"),
    cue(16.0, 18.0, "line four"),
]


# --------------------------------------------------------------- grouping


def test_two_lines_share_an_image():
    groups = scenes.group_cues(BASIC, lines_per_image=2, scene_gap=2.5)
    assert [group["lines"] for group in groups] == [
        ["line one", "line two"], ["line three", "line four"]
    ]


def test_a_musical_gap_starts_a_new_image():
    cues = [cue(0.0, 2.0, "a"), cue(9.0, 11.0, "b")]
    groups = scenes.group_cues(cues, lines_per_image=2, scene_gap=2.5)
    assert len(groups) == 2


def test_short_lines_pair_up_to_reach_the_minimum():
    """Each line alone is only a second long; pairing is what gets them to a
    length worth looking at."""
    cues = [cue(0.0, 1.0, "a"), cue(1.0, 2.0, "b")]
    groups = scenes.group_cues(cues, lines_per_image=2, scene_gap=2.5)
    assert [group["lines"] for group in groups] == [["a", "b"]]


def test_a_pair_that_would_run_past_the_maximum_splits_apart():
    cues = [cue(0.0, 10.0, "a"), cue(10.0, 20.0, "b")]  # 10s each, 20s together
    groups = scenes.group_cues(cues, lines_per_image=2, scene_gap=2.5)
    assert [group["lines"] for group in groups] == [["a"], ["b"]]


def test_a_section_boundary_always_starts_a_new_image():
    cues = [
        cue(0.0, 1.0, "last line of verse", line_index=0),
        cue(1.0, 2.0, "first line of chorus", line_index=1),
    ]
    groups = scenes.group_cues(
        cues, lines_per_image=2, scene_gap=2.5, section_starts={1}
    )
    assert [group["lines"] for group in groups] == [
        ["last line of verse"], ["first line of chorus"]
    ]


def test_a_cue_with_no_line_index_never_forces_a_section_break():
    cues = [
        cue(0.0, 1.0, "a", line_index=None),
        cue(1.0, 2.0, "b", line_index=None),
    ]
    groups = scenes.group_cues(
        cues, lines_per_image=2, scene_gap=2.5, section_starts={1}
    )
    assert [group["lines"] for group in groups] == [["a", "b"]]


def test_a_pair_only_slightly_over_the_maximum_still_beats_a_sliver():
    """Neither line reaches the minimum alone, so pairing wins even though the
    result runs a little past the maximum - a sliver of an image is worse."""
    cues = [cue(0.0, 2.0, "a"), cue(2.0, 15.0, "b")]  # 2s then 13s: 15s together
    groups = scenes.group_cues(cues, lines_per_image=2, scene_gap=2.5, max_scene=10.0)
    assert [group["lines"] for group in groups] == [["a", "b"]]


# ------------------------------------------------------------------- planning


def test_scenes_tile_the_whole_song_without_gaps_or_overlaps():
    planned = scenes.plan(BASIC, duration=40.0, title="Song", visual_style="cinematic")
    assert planned[0]["start"] == 0.0
    assert planned[-1]["end"] == 40.0
    for earlier, later in zip(planned, planned[1:]):
        assert earlier["end"] == later["start"]
    assert all(scene["end"] > scene["start"] for scene in planned)


def test_transitions_never_fall_inside_a_lyric_line():
    cues = [cue(0.0, 3.0, "a"), cue(3.0, 5.0, "b"), cue(20.0, 24.0, "c")]
    planned = scenes.plan(cues, duration=40.0, title="Song", visual_style="cinematic")
    boundaries = {round(s["start"], 3) for s in planned} | {round(s["end"], 3) for s in planned}
    for lyric in cues:
        # a scene boundary may sit at a cue's own start or end, never inside it
        assert not any(lyric["start"] < b < lyric["end"] for b in boundaries)


def test_a_short_gap_is_just_absorbed_with_no_extra_image():
    # 3s gap: its own phrase (breaks pairing), but short enough not to need an
    # image of its own - it is simply held on the first image instead.
    cues = [cue(0.0, 2.0, "a"), cue(5.0, 7.0, "b")]
    planned = scenes.plan(cues, duration=9.0, title="Song", visual_style="cinematic")
    assert [bool(s["lines"]) for s in planned] == [True, True]
    assert planned[0]["end"] == 5.0


def test_a_long_instrumental_break_splits_into_a_few_even_images():
    cues = [cue(0.0, 2.0, "first"), cue(42.0, 44.0, "second")]
    planned = scenes.plan(cues, duration=46.0, title="Song", visual_style="cinematic")

    assert [bool(s["lines"]) for s in planned] == [True, False, False, False, True]
    assert planned[0]["start"] == 0.0
    assert planned[-1]["end"] == 46.0
    for earlier, later in zip(planned, planned[1:]):
        assert earlier["end"] == later["start"]
    for scene in planned:
        span = scene["end"] - scene["start"]
        assert scenes.MIN_SCENE_DURATION - 0.01 <= span <= scenes.MAX_SCENE_DURATION + 0.01


def test_long_intro_and_instrumental_break_get_split_into_several_images():
    cues = [cue(30.0, 32.0, "first"), cue(80.0, 82.0, "second")]
    planned = scenes.plan(cues, duration=100.0, title="Song", visual_style="cinematic")

    # 2 lead-in images, "first", 4 images across the long break, "second", 2 outro
    assert [bool(s["lines"]) for s in planned] == [
        False, False, True, False, False, False, False, True, False, False,
    ]
    assert planned[0]["start"] == 0.0
    assert planned[-1]["end"] == 100.0
    for earlier, later in zip(planned, planned[1:]):
        assert earlier["end"] == later["start"]
    for scene in planned:
        span = scene["end"] - scene["start"]
        assert scenes.MIN_SCENE_DURATION - 0.01 <= span <= scenes.MAX_SCENE_DURATION + 0.01


def test_a_split_instrumental_breaks_prompt_says_which_part_it_is():
    cues = [cue(0.0, 2.0, "first"), cue(42.0, 44.0, "second")]
    planned = scenes.plan(cues, duration=46.0, title="Song", visual_style="cinematic")
    instrumentals = [s for s in planned if not s["lines"]]
    assert len(instrumentals) == 3
    assert [s["prompt"].count("part 1 of 3") for s in instrumentals[:1]] == [1]
    assert "part 3 of 3" in instrumentals[-1]["prompt"]


def test_prompts_carry_the_style_the_lyrics_and_a_no_text_rule():
    planned = scenes.plan(BASIC, duration=40.0, title="Song", visual_style="watercolor")
    prompt = planned[0]["prompt"]
    assert prompt.startswith("watercolor.")
    assert "line one / line two" in prompt
    assert "No words" in prompt
    assert "margin" in prompt.lower()


def test_instrumental_prompts_also_ask_for_margin():
    cues = [cue(42.0, 44.0, "second")]
    planned = scenes.plan(cues, duration=46.0, title="Song", visual_style="cinematic")
    instrumental = next(s for s in planned if not s["lines"])
    assert "margin" in instrumental["prompt"].lower()


def test_lines_mentioning_god_or_jesus_get_the_reverence_note():
    cues = [cue(10.0, 12.0, "Christ is supreme"), cue(12.0, 14.0, "over all things")]
    planned = scenes.plan(cues, duration=40.0, title="Song", visual_style="cinematic")
    assert "blurred, veiled, or turned away" in planned[0]["prompt"]


def test_lines_without_god_or_jesus_have_no_reverence_note():
    planned = scenes.plan(BASIC, duration=40.0, title="Song", visual_style="cinematic")
    assert "blurred, veiled, or turned away" not in planned[0]["prompt"]


def test_an_instrumental_break_inherits_the_reverence_note_from_its_context():
    cues = [cue(0.0, 2.0, "Jesus is Lord"), cue(42.0, 44.0, "second")]
    planned = scenes.plan(cues, duration=46.0, title="Song", visual_style="cinematic")
    instrumental = next(s for s in planned if not s["lines"])
    assert "blurred, veiled, or turned away" in instrumental["prompt"]


def test_motion_alternates_between_scenes():
    planned = scenes.plan(BASIC, duration=40.0, title="Song", visual_style="cinematic")
    assert planned[0]["motion"] != planned[1]["motion"]


def test_replanning_keeps_images_whose_prompt_is_unchanged():
    old = [{"prompt": "p1", "image": "/tmp/a.png"}, {"prompt": "p2", "image": "/tmp/b.png"}]
    new = [{"prompt": "p2"}, {"prompt": "p3"}]
    merged = scenes.merge_existing_images(new, old)
    assert merged[0]["image"] == "/tmp/b.png"
    assert "image" not in merged[1]
