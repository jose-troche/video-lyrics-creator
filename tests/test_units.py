import json

import pytest

from video_lyrics import google_drive, lyrics, motion, overlays
from video_lyrics.config import DEFAULT_AUTHOR, Project
from video_lyrics.util import VideoLyricsError, format_timecode


# ------------------------------------------------------------------- lyrics


def test_section_markers_and_blank_lines_are_dropped():
    raw = "[Verse 1]\nOnce I was dead\n\nChorus:\nBut Christ is rich\n"
    assert lyrics.clean_lines(raw) == ["Once I was dead", "But Christ is rich"]


def test_section_starts_mark_the_first_line_of_the_song_and_after_each_marker():
    raw = "[Verse 1]\nOnce I was dead\nAnd lost\n\nChorus:\nBut Christ is rich\nAnd near\n"
    lines, sections = lyrics.clean_lines_with_sections(raw)
    assert lines == ["Once I was dead", "And lost", "But Christ is rich", "And near"]
    assert sections == {0, 2}


def test_a_blank_line_starts_a_new_section_even_without_a_marker():
    """Not every verse is labelled, but a stanza is always set off from the
    next one by a blank line - that alone should be enough to mark a break."""
    raw = "Once I was dead\nAnd lost\n\nBut Christ is rich\nAnd near\n"
    lines, sections = lyrics.clean_lines_with_sections(raw)
    assert lines == ["Once I was dead", "And lost", "But Christ is rich", "And near"]
    assert sections == {0, 2}


def test_a_google_docs_soft_line_break_is_treated_as_a_real_line_break():
    """The Docs API returns a Shift+Enter soft break as a vertical tab embedded in
    the paragraph's text, not a paragraph break - it must not collapse into a
    space the way ordinary whitespace does."""
    raw = "Praise the Lord\vBend your knee\vFor his ransom\vForevermore\n"
    assert lyrics.clean_lines(raw) == [
        "Praise the Lord", "Bend your knee", "For his ransom", "Forevermore"
    ]


def test_whitespace_is_normalised_but_wording_is_untouched():
    assert lyrics.clean_lines("  I  walked   the world’s ways \n") == [
        "I walked the world’s ways"
    ]


def test_unsupported_lyrics_extension_is_rejected(tmp_path):
    path = tmp_path / "lyrics.pdf"
    path.write_text("x")
    with pytest.raises(VideoLyricsError):
        lyrics.load_lines(path)


# --------------------------------------------------------------- google doc


def test_doc_id_is_read_from_a_drive_stub(tmp_path):
    stub = tmp_path / "song.gdoc"
    stub.write_text(json.dumps({"doc_id": "1AbC" + "x" * 20, "email": "a@b.c"}))
    assert google_drive.doc_id_from_gdoc(stub) == "1AbC" + "x" * 20


def test_doc_id_falls_back_to_the_url(tmp_path):
    stub = tmp_path / "song.gdoc"
    stub.write_text(json.dumps({"url": "https://docs.google.com/document/d/" + "y" * 30 + "/edit"}))
    assert google_drive.doc_id_from_gdoc(stub) == "y" * 30


def test_a_stub_without_an_id_is_an_error(tmp_path):
    stub = tmp_path / "song.gdoc"
    stub.write_text("not a google doc")
    with pytest.raises(VideoLyricsError):
        google_drive.doc_id_from_gdoc(stub)


def _paragraph(*runs: str) -> dict:
    return {"paragraph": {"elements": [{"textRun": {"content": run}} for run in runs]}}


def test_render_body_text_reconstructs_paragraphs_from_the_docs_api_shape():
    # Each paragraph's own last run already carries its trailing "\n" - the Docs
    # API's own way of representing a paragraph break.
    body = {
        "content": [
            _paragraph("Once I was dead\n"),
            _paragraph("Offending ", "God\n"),
            _paragraph("\n"),  # a blank line
        ]
    }
    assert google_drive.render_body_text(body) == "Once I was dead\nOffending God\n\n"


def test_render_body_text_skips_non_paragraph_content_like_tables():
    body = {"content": [{"sectionBreak": {}}, _paragraph("Immeasurable grace\n")]}
    assert google_drive.render_body_text(body) == "Immeasurable grace\n"


def test_export_document_reads_only_the_first_tab(monkeypatch):
    """A multi-tab doc must not have every tab's content requested or returned.

    The Docs API's own contract is: without `includeTabsContent`, `body` holds
    just the first tab. This pins two things - the request never asks for every
    tab, and the response is read from `body`, not from a `tabs` list.
    """
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "body": {"content": [_paragraph("Once I was dead\n")]},
                # A tabs list would only appear with includeTabsContent=true; the
                # code must never send that param, so this must never be read.
                "tabs": [
                    {"documentTab": {"body": {"content": [_paragraph("Once I was dead\n")]}}},
                    {"documentTab": {"body": {"content": [_paragraph("A discarded draft\n")]}}},
                ],
            }

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr(google_drive.requests, "get", fake_get)
    monkeypatch.setattr(google_drive, "access_token", lambda: "token")

    text = google_drive.export_document("doc123")

    assert text == "Once I was dead\n"
    assert "doc123" in captured["url"]
    assert not (captured["params"] or {}).get("includeTabsContent")


# ------------------------------------------------------------------ project


def test_project_round_trips_through_disk(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF")
    words = tmp_path / "song.txt"
    words.write_text("a line")

    project = Project.create(
        tmp_path / "project.json", audio=str(audio), lyrics_source=str(words), title="My Song"
    )
    project.data["duration"] = 12.5
    project.save()

    reloaded = Project.load(tmp_path / "project.json")
    assert reloaded.title == "My Song"
    assert reloaded.author == DEFAULT_AUTHOR
    assert reloaded.duration == 12.5
    assert reloaded.size == (1920, 1080)
    assert reloaded.output.name == "my-song.mp4"


def test_a_missing_audio_file_is_caught_at_init(tmp_path):
    words = tmp_path / "song.txt"
    words.write_text("a line")
    with pytest.raises(VideoLyricsError):
        Project.create(tmp_path / "p.json", audio=str(tmp_path / "nope.wav"), lyrics_source=str(words))


# ----------------------------------------------------------------- overlays


def test_srt_is_written_with_the_lead_in_applied(tmp_path):
    cues = [
        {"start": 5.0, "end": 7.0, "text": "first line"},
        {"start": 7.0, "end": 9.5, "text": "second line"},
    ]
    path = overlays.write_srt(cues, tmp_path / "lyrics.srt", lead=0.5)
    body = path.read_text(encoding="utf-8")
    assert "00:00:04,500 --> 00:00:07,000" in body
    assert "second line" in body


def test_a_lead_in_never_pushes_a_cue_over_the_one_before_it():
    previous = 7.0
    start, end = overlays.cue_display_times({"start": 7.2, "end": 9.0}, lead=0.5, previous_end=previous)
    assert start == previous


def test_the_title_card_ends_before_the_first_lyric():
    cues = [{"start": 21.7, "end": 25.0, "text": "first"}]
    start, end = overlays.title_window(cues, duration=200.0, requested=12.0, fade=0.75, lead=0.35)
    assert start == 0.0
    assert end == 12.0

    tight = [{"start": 4.0, "end": 6.0, "text": "first"}]
    _, end = overlays.title_window(tight, duration=200.0, requested=12.0, fade=0.75, lead=0.35)
    assert end < tight[0]["start"] - 0.35


def test_timecode_formatting():
    assert format_timecode(3661.5) == "01:01:01,500"


# ------------------------------------------------------------------ the bed


def scene(index, start, end, motion="zoom_in"):
    return {"index": index, "start": start, "end": end, "motion": motion, "image": f"/tmp/{index}.png"}


def test_the_bed_covers_every_frame_of_the_song_exactly_once():
    scenes = [scene(1, 0.0, 10.0), scene(2, 10.0, 20.0), scene(3, 20.0, 30.0)]
    clips = motion.plan_bed(scenes, fps=30, duration=30.0, transition=0.8)

    assert sum(clip["frames"] for clip in clips) == 900
    cursor = 0
    for clip in clips:
        assert clip["first_frame"] == cursor
        cursor += clip["frames"]
    assert cursor == 900


def test_dissolves_sit_across_the_scene_boundaries():
    scenes = [scene(1, 0.0, 10.0), scene(2, 10.0, 20.0)]
    clips = motion.plan_bed(scenes, fps=30, duration=20.0, transition=1.0)
    dissolves = [clip for clip in clips if clip["kind"] == "transition"]
    assert len(dissolves) == 1
    dissolve = dissolves[0]
    assert dissolve["frames"] == 30
    assert dissolve["first_frame"] == 300 - 15  # centred on the 10s boundary


def test_a_scene_shorter_than_the_transition_still_gets_frames():
    scenes = [scene(1, 0.0, 5.0), scene(2, 5.0, 5.4), scene(3, 5.4, 12.0)]
    clips = motion.plan_bed(scenes, fps=30, duration=12.0, transition=1.5)
    assert sum(clip["frames"] for clip in clips) == 360
    assert all(clip["frames"] >= 1 for clip in clips)


def test_motion_spans_extend_through_the_dissolves():
    scenes = [scene(1, 0.0, 10.0), scene(2, 10.0, 20.0)]
    motion.plan_bed(scenes, fps=30, duration=20.0, transition=1.0)
    # scene 2's motion starts 15 frames early so it is already moving when it fades in
    assert scenes[1]["motion_first_frame"] == 285
    assert scenes[1]["motion_frames"] == 315


def test_zoompan_expressions_interpolate_across_the_scene():
    chain = motion.zoompan_filter(
        motion="zoom_in", zoom=1.1, size=(1920, 1080), fps=30,
        first_frame=30, motion_start=0, motion_span=301, supersample=2,
    )
    assert "zoompan=" in chain
    assert "scale=3840:2160" in chain
    assert "s=1920x1080" in chain
    assert "(30+on-0)/300" in chain


def test_a_still_motion_has_no_interpolation_term():
    chain = motion.zoompan_filter(
        motion="still", zoom=1.1, size=(1920, 1080), fps=30,
        first_frame=0, motion_start=0, motion_span=100, supersample=2,
    )
    assert "on" not in chain.split("zoompan=")[1].split(":d=")[0]


def test_scene_zoom_scales_with_duration_between_a_floor_and_a_cap():
    # a 6s scene (the reference duration) gets exactly the configured zoom
    assert motion.scene_zoom(1.20, 6.0) == pytest.approx(1.20)
    # a much shorter scene still gets at least the floor, not something imperceptible
    assert motion.scene_zoom(1.20, 0.5) == pytest.approx(motion.MOTION_MIN_ZOOM)
    # a much longer scene is capped rather than zooming in absurdly far
    assert motion.scene_zoom(1.20, 60.0) == pytest.approx(motion.MOTION_MAX_ZOOM)
    # roughly double the duration is roughly double the zoom, before the cap
    assert motion.scene_zoom(1.20, 12.0) == pytest.approx(1.40)


def test_pans_get_real_travel_room_even_at_a_low_configured_zoom():
    """A pan needs crop room; too-low a zoom must not collapse its travel to nothing."""
    z0, z1 = motion._zoom_levels("pan_right", 1.01)
    assert z0 == z1 == pytest.approx(motion.PAN_MIN_ZOOM)

    chain = motion.zoompan_filter(
        motion="pan_right", zoom=1.20, size=(1920, 1080), fps=30,
        first_frame=0, motion_start=0, motion_span=180, supersample=2,
    )
    assert "(iw-iw/zoom)*(0.120000" in chain  # the widened pan range is present


def test_a_short_scene_and_a_long_scene_pan_at_a_similar_rate():
    """The point of scaling by duration: apparent speed stays steady, not just total."""
    short_zoom = motion.scene_zoom(1.20, 3.0)
    long_zoom = motion.scene_zoom(1.20, 9.0)
    short_rate = (short_zoom - 1.0) / 3.0
    long_rate = (long_zoom - 1.0) / 9.0
    assert short_rate == pytest.approx(long_rate, rel=0.05)


def test_supersampling_defaults_high_enough_to_avoid_pixel_stepping():
    # at the old default (2x) a multi-second pan moves under a pixel per frame,
    # which is what read as jerky; the new default must clear that bar comfortably.
    assert motion.DEFAULT_SUPERSAMPLE >= 3


def _fake_ffmpeg(monkeypatch, calls):
    monkeypatch.setattr(motion, "which", lambda _name: "ffmpeg")
    monkeypatch.setattr(motion, "run", lambda cmd, **kw: calls.append(cmd))


def test_concat_clips_always_rebuilds(tmp_path, monkeypatch):
    # The concat is a stream copy (well under a second), and a `force` bed
    # rebuild can overwrite a clip's content without changing its fingerprinted
    # filename - a cache keyed on the clip list would miss that and leave a
    # stale `bed.mp4` behind. So there is no cache: every call re-concatenates.
    calls = []
    _fake_ffmpeg(monkeypatch, calls)
    out = tmp_path / "bed.mp4"
    clip_a = tmp_path / "bed-001-scene-aaa.mp4"
    clip_a.touch()

    motion.concat_clips([{"path": str(clip_a)}], out)
    assert len(calls) == 1
    out.touch()  # ffmpeg is faked, so nothing actually wrote `out`

    motion.concat_clips([{"path": str(clip_a)}], out)
    assert len(calls) == 2
