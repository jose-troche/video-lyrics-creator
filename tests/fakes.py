"""A stand-in for the DaVinci Resolve scripting API, used by the tests."""

from __future__ import annotations

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

    def GetName(self):  # noqa: N802
        return self.name

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

    def DeleteTimelines(self, timelines):  # noqa: N802
        for timeline in timelines:
            if timeline in self.timelines:
                self.timelines.remove(timeline)
        return True

    def AppendToTimeline(self, payload):  # noqa: N802
        self.appended.extend(payload)
        return list(payload)


class FakeProject:
    def __init__(self, name, on_render=None):
        self.name = name
        self.media_pool = FakeMediaPool()
        self.settings: dict[str, str] = {}
        self.render: dict = {}
        # tests set this to emulate Resolve actually writing the file
        self.on_render = on_render

    def GetName(self):  # noqa: N802
        return self.name

    def GetMediaPool(self):  # noqa: N802
        return self.media_pool

    # Resolve has no GetTimelineByName; timelines are walked by index, from 1.
    def GetTimelineCount(self):  # noqa: N802
        return len(self.media_pool.timelines)

    def GetTimelineByIndex(self, index):  # noqa: N802
        timelines = self.media_pool.timelines
        return timelines[index - 1] if 1 <= index <= len(timelines) else None

    def SetCurrentTimeline(self, _timeline):  # noqa: N802
        return True

    def SetSetting(self, key, value):  # noqa: N802
        self.settings[key] = value
        return True

    # --- the Deliver page ---------------------------------------------------

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):  # noqa: N802
        self.render["format"] = fmt
        self.render["codec"] = codec
        return True

    def SetCurrentRenderMode(self, mode):  # noqa: N802
        self.render["mode"] = mode
        return True

    def SetRenderSettings(self, settings):  # noqa: N802
        self.render.setdefault("settings", {}).update(settings)
        return True

    def AddRenderJob(self):  # noqa: N802
        self.render["job"] = "job-1"
        return "job-1"

    def StartRendering(self, _jobs, isInteractiveMode=False):  # noqa: N802, N803
        self.render["started"] = True
        if self.on_render:
            self.on_render(self.render.get("settings", {}))
        return True

    def IsRenderingInProgress(self):  # noqa: N802
        return False

    def GetRenderJobStatus(self, _job):  # noqa: N802
        return {"JobStatus": "Complete", "CompletionPercentage": 100}


class FakeManager:
    def __init__(self, on_render=None):
        self.project = None
        self.on_render = on_render

    def GotoRootFolder(self):  # noqa: N802
        return True

    def GetCurrentProject(self):  # noqa: N802
        return self.project

    def LoadProject(self, _name):  # noqa: N802
        return None

    def CreateProject(self, name):  # noqa: N802
        self.project = FakeProject(name, self.on_render)
        return self.project

    def CloseProject(self, _project):  # noqa: N802
        return True

    def DeleteProject(self, _name):  # noqa: N802
        return True


class FakeResolve:
    def __init__(self, on_render=None):
        self.manager = FakeManager(on_render)

    def GetProjectManager(self):  # noqa: N802
        return self.manager
