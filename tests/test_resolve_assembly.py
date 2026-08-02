"""Exercise the Resolve timeline builder against a stand-in for the scripting API.

DaVinci Resolve itself cannot run in a test, but the part that is easy to get
wrong - which clip lands on which track at which frame - is pure arithmetic and
is checked here.
"""

from __future__ import annotations

import pytest

from video_lyrics import render_resolve

from .fakes import TIMELINE_START, FakeResolve


@pytest.fixture
def built(tmp_path):
    fake = FakeResolve()

    clips = [
        {"kind": "scene", "path": str(tmp_path / "bed-001.mp4"), "first_frame": 0, "frames": 300},
        {"kind": "transition", "path": str(tmp_path / "bed-002.mp4"), "first_frame": 300, "frames": 22},
        {"kind": "scene", "path": str(tmp_path / "bed-003.mp4"), "first_frame": 322, "frames": 578},
    ]
    lyrics = [
        {"start": 10.0, "end": 13.0, "clip": str(tmp_path / "lyric-001.mov")},
        {"start": 13.0, "end": 16.0, "clip": str(tmp_path / "lyric-002.mov")},
    ]
    title = {"start": 0.0, "end": 9.0, "clip": str(tmp_path / "title.mov")}

    _resolve, project, timeline = render_resolve.assemble(
        clips=clips,
        lyric_items=lyrics,
        title_item=title,
        audio=tmp_path / "song.wav",
        subtitle_file=None,
        project_name="Test Song",
        timeline_name="Test Song - lyrics",
        size=(1920, 1080),
        fps=30,
        duration=30.0,
        resolve=fake,
    )
    return project, timeline, project.GetMediaPool().appended


def test_tracks_are_created_and_named(built):
    _project, timeline, _appended = built
    assert timeline.tracks["video"] == 3
    assert timeline.names[("video", 1)] == "Images"
    assert timeline.names[("video", 2)] == "Lyrics"
    assert timeline.names[("video", 3)] == "Title"
    assert timeline.names[("audio", 1)] == "Music"


def test_the_image_bed_lands_end_to_end_on_video_track_one(built):
    _project, _timeline, appended = built
    images = [clip for clip in appended if clip["trackIndex"] == 1 and clip["mediaType"] == 1]
    assert [clip["recordFrame"] for clip in images] == [
        TIMELINE_START, TIMELINE_START + 300, TIMELINE_START + 322,
    ]
    assert [clip["endFrame"] for clip in images] == [299, 21, 577]
    assert all(clip["startFrame"] == 0 for clip in images)


def test_lyrics_and_title_go_to_their_own_tracks_at_the_right_frames(built):
    _project, _timeline, appended = built
    lyrics = [clip for clip in appended if clip["trackIndex"] == 2]
    assert [clip["recordFrame"] for clip in lyrics] == [TIMELINE_START + 300, TIMELINE_START + 390]
    assert [clip["endFrame"] for clip in lyrics] == [89, 89]

    title = [clip for clip in appended if clip["trackIndex"] == 3]
    assert len(title) == 1
    assert title[0]["recordFrame"] == TIMELINE_START
    assert title[0]["endFrame"] == 269  # 9s at 30fps


def test_the_song_is_placed_as_audio_from_the_first_frame(built):
    _project, _timeline, appended = built
    audio = [clip for clip in appended if clip["mediaType"] == 2]
    assert len(audio) == 1
    assert audio[0]["recordFrame"] == TIMELINE_START
    assert audio[0]["endFrame"] == 899


def test_timeline_and_project_are_set_to_the_video_format(built):
    project, timeline, _appended = built
    assert project.settings["timelineResolutionWidth"] == "1920"
    assert project.settings["timelineFrameRate"] == "30"
    assert timeline.settings["useCustomSettings"] == "1"


def test_a_short_placement_never_collapses_to_zero_frames(tmp_path):
    fake = FakeResolve()
    _resolve, project, _timeline = render_resolve.assemble(
        clips=[{"kind": "scene", "path": str(tmp_path / "b.mp4"), "first_frame": 0, "frames": 30}],
        lyric_items=[{"start": 0.5, "end": 0.51, "clip": str(tmp_path / "l.mov")}],
        title_item=None,
        audio=tmp_path / "song.wav",
        subtitle_file=None,
        project_name="Tiny",
        timeline_name="Tiny - lyrics",
        size=(1920, 1080),
        fps=30,
        duration=1.0,
        resolve=fake,
    )
    lyric = [clip for clip in project.GetMediaPool().appended if clip["trackIndex"] == 2][0]
    assert lyric["endFrame"] == 0  # one frame long, not negative


def test_scripting_hint_explains_the_disabled_preference(monkeypatch):
    monkeypatch.setattr(render_resolve, "scripting_preference", lambda: 0)
    hint = render_resolve.scripting_hint()
    assert "External scripting" in hint
    assert "Local" in hint
