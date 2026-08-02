"""Exercise the Resolve timeline builder against a stand-in for the scripting API.

DaVinci Resolve itself cannot run in a test, but the part that is easy to get
wrong - which clip lands on which track at which frame - is pure arithmetic and
is checked here.
"""

from __future__ import annotations

import pytest

from video_lyrics import render_resolve

TIMELINE_START = 108000  # Resolve timelines start at 01:00:00:00


class FakeItem:
    def __init__(self, path: str):
        self.path = path

    def GetClipProperty(self, key: str):  # noqa: N802 - mirrors the Resolve API
        return self.path if key == "File Path" else None


class FakeTimeline:
    def __init__(self, name: str):
        self.name = name
        self.tracks = {"video": 1, "audio": 1, "subtitle": 0}
        self.names: dict[tuple[str, int], str] = {}
        self.settings: dict[str, str] = {}

    def GetStartFrame(self):  # noqa: N802
        return TIMELINE_START

    def GetTrackCount(self, track_type):  # noqa: N802
        return self.tracks[track_type]

    def AddTrack(self, track_type, *_args):  # noqa: N802
        self.tracks[track_type] += 1
        return True

    def SetTrackName(self, track_type, index, name):  # noqa: N802
        self.names[(track_type, index)] = name
        return True

    def SetSetting(self, key, value):  # noqa: N802
        self.settings[key] = value
        return True


class FakeMediaPool:
    def __init__(self):
        self.appended: list[dict] = []
        self.timelines: list[FakeTimeline] = []

    def GetRootFolder(self):  # noqa: N802
        return "root"

    def SetCurrentFolder(self, _folder):  # noqa: N802
        return True

    def ImportMedia(self, paths):  # noqa: N802
        return [FakeItem(path) for path in paths]

    def CreateEmptyTimeline(self, name):  # noqa: N802
        timeline = FakeTimeline(name)
        self.timelines.append(timeline)
        return timeline

    def DeleteTimelines(self, _timelines):  # noqa: N802
        return True

    def AppendToTimeline(self, payload):  # noqa: N802
        self.appended.extend(payload)
        return list(payload)


class FakeProject:
    def __init__(self, name):
        self.name = name
        self.media_pool = FakeMediaPool()
        self.settings: dict[str, str] = {}

    def GetName(self):  # noqa: N802
        return self.name

    def GetMediaPool(self):  # noqa: N802
        return self.media_pool

    def GetTimelineByName(self, _name):  # noqa: N802
        return None

    def SetCurrentTimeline(self, _timeline):  # noqa: N802
        return True

    def SetSetting(self, key, value):  # noqa: N802
        self.settings[key] = value
        return True


class FakeManager:
    def __init__(self):
        self.project = None

    def GotoRootFolder(self):  # noqa: N802
        return True

    def GetCurrentProject(self):  # noqa: N802
        return self.project

    def LoadProject(self, _name):  # noqa: N802
        return None

    def CreateProject(self, name):  # noqa: N802
        self.project = FakeProject(name)
        return self.project

    def CloseProject(self, _project):  # noqa: N802
        return True

    def DeleteProject(self, _name):  # noqa: N802
        return True


class FakeResolve:
    def __init__(self):
        self.manager = FakeManager()

    def GetProjectManager(self):  # noqa: N802
        return self.manager


@pytest.fixture
def built(monkeypatch, tmp_path):
    fake = FakeResolve()
    monkeypatch.setattr(render_resolve, "connect", lambda: fake)

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


def test_a_short_placement_never_collapses_to_zero_frames(monkeypatch, tmp_path):
    fake = FakeResolve()
    monkeypatch.setattr(render_resolve, "connect", lambda: fake)
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
    )
    lyric = [clip for clip in project.GetMediaPool().appended if clip["trackIndex"] == 2][0]
    assert lyric["endFrame"] == 0  # one frame long, not negative


def test_scripting_hint_explains_the_disabled_preference(monkeypatch):
    monkeypatch.setattr(render_resolve, "scripting_preference", lambda: 0)
    hint = render_resolve.scripting_hint()
    assert "External scripting" in hint
    assert "Local" in hint
