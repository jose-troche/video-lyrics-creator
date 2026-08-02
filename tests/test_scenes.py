from video_lyrics import scenes


def cue(start, end, text):
    return {"start": start, "end": end, "text": text, "line_index": 0}


BASIC = [
    cue(10.0, 12.0, "line one"),
    cue(12.0, 14.0, "line two"),
    cue(14.0, 16.0, "line three"),
    cue(16.0, 18.0, "line four"),
]


def test_two_lines_share_an_image():
    groups = scenes.group_cues(BASIC, lines_per_image=2, scene_gap=2.5)
    assert [group["lines"] for group in groups] == [
        ["line one", "line two"], ["line three", "line four"]
    ]


def test_a_musical_gap_starts_a_new_image():
    cues = [cue(0.0, 2.0, "a"), cue(9.0, 11.0, "b")]
    groups = scenes.group_cues(cues, lines_per_image=2, scene_gap=2.5)
    assert len(groups) == 2


def test_scenes_tile_the_whole_song_without_gaps_or_overlaps():
    planned = scenes.plan(
        BASIC, duration=40.0, title="Song", visual_style="cinematic", interlude=12.0
    )
    assert planned[0]["start"] == 0.0
    assert planned[-1]["end"] == 40.0
    for earlier, later in zip(planned, planned[1:]):
        assert earlier["end"] == later["start"]
    assert all(scene["end"] > scene["start"] for scene in planned)


def test_long_intro_and_instrumental_break_get_their_own_images():
    cues = [cue(30.0, 32.0, "first"), cue(80.0, 82.0, "second")]
    planned = scenes.plan(
        cues, duration=100.0, title="Song", visual_style="cinematic", interlude=12.0
    )
    # intro image, first line, instrumental break, second line, outro
    assert len(planned) == 5
    assert [bool(scene["lines"]) for scene in planned] == [False, True, False, True, False]
    assert planned[0]["end"] == 30.0
    assert planned[-1]["end"] == 100.0


def test_prompts_carry_the_style_the_lyrics_and_a_no_text_rule():
    planned = scenes.plan(BASIC, duration=40.0, title="Song", visual_style="watercolor")
    prompt = planned[0]["prompt"]
    assert prompt.startswith("watercolor.")
    assert "line one / line two" in prompt
    assert "No words" in prompt


def test_motion_alternates_between_scenes():
    planned = scenes.plan(BASIC, duration=40.0, title="Song", visual_style="cinematic")
    assert planned[0]["motion"] != planned[1]["motion"]


def test_replanning_keeps_images_whose_prompt_is_unchanged():
    old = [{"prompt": "p1", "image": "/tmp/a.png"}, {"prompt": "p2", "image": "/tmp/b.png"}]
    new = [{"prompt": "p2"}, {"prompt": "p3"}]
    merged = scenes.merge_existing_images(new, old)
    assert merged[0]["image"] == "/tmp/b.png"
    assert "image" not in merged[1]
