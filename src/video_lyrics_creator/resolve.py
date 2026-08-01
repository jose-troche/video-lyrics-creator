from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ResolveError


def seconds_to_frames(seconds: float, fps: float) -> int:
    return int(seconds * fps + 0.5)


def timeline_plan(manifest: dict) -> dict:
    video = manifest["video"]
    fps = float(video["fps"])
    transition = float(video["transition"])
    transition_half = transition / 2
    duration = float(manifest["duration"])
    scenes = []
    for index, scene in enumerate(manifest["scenes"]):
        clip_start = 0.0 if index == 0 else max(0.0, float(scene["start"]) - transition_half)
        clip_end = (
            duration
            if index + 1 == len(manifest["scenes"])
            else min(duration, float(scene["end"]) + transition_half)
        )
        scenes.append(
            {
                "index": index + 1,
                "track": 1 + index % 2,
                "start_frame": seconds_to_frames(clip_start, fps),
                "end_frame": seconds_to_frames(clip_end, fps),
                "duration_frames": max(1, seconds_to_frames(clip_end - clip_start, fps)),
                "transition_frames": seconds_to_frames(transition, fps),
                "image": scene["image"],
                "motion": scene.get("motion", "zoom_in"),
                "fade_in": index > 0,
                "fade_out": index + 1 < len(manifest["scenes"]),
            }
        )
    return {
        "fps": fps,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration_frames": seconds_to_frames(duration, fps),
        "scenes": scenes,
        "lyrics": [
            {
                **item,
                "start_frame": seconds_to_frames(float(item["start"]), fps),
                "duration_frames": max(
                    1, seconds_to_frames(float(item["end"]) - float(item["start"]), fps)
                ),
            }
            for item in manifest["overlays"]["lyrics"]
        ],
        "title": {
            "image": manifest["overlays"]["title"],
            "start_frame": 0,
            "duration_frames": min(
                seconds_to_frames(float(video["title_duration"]), fps),
                seconds_to_frames(duration, fps),
            ),
        },
        "audio": manifest["audio"],
    }


@dataclass
class BuildResult:
    project_name: str
    timeline_name: str
    render_job_id: str | None = None
    render_status: dict | None = None


class ResolveTimelineBuilder:
    def __init__(self, resolve: Any, manifest: dict):
        self.resolve = resolve
        self.manifest = manifest
        self.plan = timeline_plan(manifest)
        self.project = None
        self.media_pool = None
        self.timeline = None

    def build(
        self,
        *,
        project_name: str,
        timeline_name: str,
        replace_timeline: bool = False,
        render: bool = False,
        wait: bool = True,
    ) -> BuildResult:
        self._open_project(project_name)
        self._configure_project()
        self._create_timeline(timeline_name, replace_timeline)
        self._ensure_tracks()
        self._append_audio()
        self._append_scenes()
        self._append_overlays()
        self._add_review_markers()
        manager = self.resolve.GetProjectManager()
        if not manager.SaveProject():
            raise ResolveError("Resolve did not save the project")

        result = BuildResult(project_name=project_name, timeline_name=timeline_name)
        if render:
            result.render_job_id = self._queue_render()
            if not self.project.StartRendering([result.render_job_id], False):
                raise ResolveError("Resolve did not start the render job")
            if wait:
                result.render_status = self._wait_for_render(result.render_job_id)
        return result

    def _open_project(self, name: str) -> None:
        manager = self.resolve.GetProjectManager()
        current = manager.GetCurrentProject()
        if current and current.GetName() == name:
            project = current
        else:
            project = manager.LoadProject(name) or manager.CreateProject(name)
        if not project:
            raise ResolveError(f"Could not load or create Resolve project: {name}")
        self.project = project
        self.media_pool = project.GetMediaPool()

    def _configure_project(self) -> None:
        settings = {
            "timelineResolutionWidth": str(self.plan["width"]),
            "timelineResolutionHeight": str(self.plan["height"]),
            "timelineFrameRate": _fps_string(self.plan["fps"]),
        }
        failed = []
        for key, value in settings.items():
            current = str(self.project.GetSetting(key) or "")
            if current == value:
                continue
            if not self.project.SetSetting(key, value):
                failed.append(key)
        if failed:
            raise ResolveError(
                "Resolve rejected project setting(s): "
                + ", ".join(failed)
                + ". Use a new project or match the manifest to the existing project."
            )

        playback_rate = _fps_string(self.plan["fps"])
        current_playback = str(self.project.GetSetting("timelinePlaybackFrameRate") or "")
        if current_playback != playback_rate and not self.project.SetSetting(
            "timelinePlaybackFrameRate", playback_rate
        ):
            # Resolve Free accepts the timeline frame rate but may expose playback frame rate as
            # a read-only setting through the scripting API. Rendering uses the timeline rate.
            print(
                "Video Lyrics Creator: Resolve did not allow setting playback frame rate to "
                f"{playback_rate}; continuing with timeline frame rate {playback_rate}."
            )

    def _create_timeline(self, name: str, replace: bool) -> None:
        existing = None
        for index in range(1, self.project.GetTimelineCount() + 1):
            candidate = self.project.GetTimelineByIndex(index)
            if candidate.GetName() == name:
                existing = candidate
                break
        if existing and not replace:
            raise ResolveError(
                f"Timeline {name!r} already exists. Pass --replace-timeline to replace only that timeline."
            )
        if existing and not self.media_pool.DeleteTimelines([existing]):
            raise ResolveError(f"Resolve could not delete existing timeline: {name}")
        self.timeline = self.media_pool.CreateEmptyTimeline(name)
        if not self.timeline or not self.project.SetCurrentTimeline(self.timeline):
            raise ResolveError(f"Resolve could not create timeline: {name}")
        self.timeline.SetStartTimecode("00:00:00:00")

    def _ensure_tracks(self) -> None:
        while self.timeline.GetTrackCount("video") < 4:
            if not self.timeline.AddTrack("video"):
                raise ResolveError("Resolve could not add a video track")
        if self.timeline.GetTrackCount("audio") < 1 and not self.timeline.AddTrack("audio", "stereo"):
            raise ResolveError("Resolve could not add an audio track")
        for index, name in enumerate(("Scenes A", "Scenes B", "Lyrics", "Title"), 1):
            self.timeline.SetTrackName("video", index, name)
        self.timeline.SetTrackName("audio", 1, "Original Song")

    def _import(self, path: str):
        items = self.media_pool.ImportMedia([str(Path(path).resolve())])
        if not items:
            raise ResolveError(f"Resolve could not import media: {path}")
        return items[0]

    def _append_clip(
        self, media_item: Any, *, track: int, start_frame: int, duration_frames: int, media_type: int = 1
    ):
        timeline_start = int(self.timeline.GetStartFrame())
        info = {
            "mediaPoolItem": media_item,
            "startFrame": 0,
            "endFrame": max(0, duration_frames - 1),
            "mediaType": media_type,
            "trackIndex": track,
            "recordFrame": timeline_start + start_frame,
        }
        items = self.media_pool.AppendToTimeline([info])
        if not items:
            raise ResolveError("Resolve could not append a clip to the timeline")
        return items[0]

    def _append_audio(self) -> None:
        item = self._import(self.plan["audio"])
        self._append_clip(
            item,
            track=1,
            start_frame=0,
            duration_frames=self.plan["duration_frames"],
            media_type=2,
        )

    def _append_scenes(self) -> None:
        scale_fill = getattr(self.resolve, "SCALE_FILL", 2)
        zoom = float(self.manifest["video"].get("zoom", 1.08))
        for scene in self.plan["scenes"]:
            item = self._import(scene["image"])
            clip = self._append_clip(
                item,
                track=scene["track"],
                start_frame=scene["start_frame"],
                duration_frames=scene["duration_frames"],
            )
            if not clip.SetProperty("Scaling", scale_fill):
                raise ResolveError(f"Resolve could not set scaling on scene {scene['index']}")
            self._configure_fusion(clip, scene, zoom)

    def _configure_fusion(self, clip: Any, scene: dict, zoom: float) -> None:
        comp = clip.AddFusionComp()
        if not comp:
            raise ResolveError(f"Resolve could not add Fusion motion to scene {scene['index']}")
        comp.Lock()
        try:
            media_in = comp.FindTool("MediaIn1")
            media_out = comp.FindTool("MediaOut1")
            if not media_in or not media_out:
                raise ResolveError(f"Fusion MediaIn/MediaOut missing on scene {scene['index']}")
            transform = comp.AddTool("Transform", -1, 0)
            background = comp.AddTool("Background", 0, 1)
            merge = comp.AddTool("Merge", 1, 1)
            if not transform or not background or not merge:
                raise ResolveError(f"Fusion could not create nodes for scene {scene['index']}")
            transform.Input = media_in.Output
            background.SetInput("UseFrameFormatSettings", 1.0)
            background.SetInput("TopLeftAlpha", 0.0)
            background.SetInput("TopRightAlpha", 0.0)
            background.SetInput("BottomLeftAlpha", 0.0)
            background.SetInput("BottomRightAlpha", 0.0)
            merge.Background = background.Output
            merge.Foreground = transform.Output
            media_out.Input = merge.Output

            last = max(1, int(scene["duration_frames"]) - 1)
            start_zoom, end_zoom = (1.0, zoom)
            if scene["motion"] == "zoom_out":
                start_zoom, end_zoom = end_zoom, start_zoom
            transform.Size[0] = start_zoom
            transform.Size[last] = end_zoom

            transition = min(int(scene["transition_frames"]), max(1, last // 2))
            merge.Blend[0] = 0.0 if scene["fade_in"] else 1.0
            if scene["fade_in"]:
                merge.Blend[min(transition, last)] = 1.0
            if scene["fade_out"]:
                merge.Blend[max(0, last - transition)] = 1.0
                merge.Blend[last] = 0.0
            else:
                merge.Blend[last] = 1.0
        finally:
            comp.Unlock()

    def _append_overlays(self) -> None:
        title = self._import(self.plan["title"]["image"])
        self._append_clip(
            title,
            track=4,
            start_frame=self.plan["title"]["start_frame"],
            duration_frames=self.plan["title"]["duration_frames"],
        )
        for cue in self.plan["lyrics"]:
            item = self._import(cue["image"])
            self._append_clip(
                item,
                track=3,
                start_frame=cue["start_frame"],
                duration_frames=cue["duration_frames"],
            )

    def _add_review_markers(self) -> None:
        fps = self.plan["fps"]
        for index, cue in enumerate(self.manifest["lyrics"], 1):
            confidence = cue.get("alignment_confidence")
            note = f"Lyric {index}: {cue['text']}"
            if confidence is not None:
                note += f" | alignment confidence {float(confidence):.0%}"
            self.timeline.AddMarker(
                seconds_to_frames(float(cue["start"]), fps),
                "Blue" if confidence is None or float(confidence) >= 0.6 else "Red",
                f"Lyric {index}",
                note,
                1,
                f"video-lyrics:lyric:{index}",
            )

    def _queue_render(self) -> str:
        render = self.manifest["render"]
        target = Path(render["output"])
        target.parent.mkdir(parents=True, exist_ok=True)
        render_format = str(render.get("format", "mp4"))
        codec = str(render.get("codec", "H264"))
        if not self.project.SetCurrentRenderFormatAndCodec(render_format, codec):
            available = self.project.GetRenderCodecs(render_format) or {}
            raise ResolveError(
                f"Resolve rejected render format/codec {render_format}/{codec}. "
                f"Available codecs: {json.dumps(available, sort_keys=True)}"
            )
        settings = {
            "SelectAllFrames": True,
            "TargetDir": str(target.parent),
            "CustomName": target.stem,
            "ExportVideo": True,
            "ExportAudio": True,
            "FormatWidth": self.plan["width"],
            "FormatHeight": self.plan["height"],
            "FrameRate": self.plan["fps"],
            "AudioCodec": str(render.get("audio_codec", "aac")),
            "AudioSampleRate": 48000,
            "VideoQuality": "Best",
            "NetworkOptimization": True,
            "ReplaceExistingFilesInPlace": bool(render.get("replace_existing", True)),
        }
        if not self.project.SetRenderSettings(settings):
            raise ResolveError("Resolve rejected the render settings")
        job_id = self.project.AddRenderJob()
        if not job_id:
            raise ResolveError("Resolve could not add a render job")
        return job_id

    def _wait_for_render(self, job_id: str) -> dict:
        while True:
            status = self.project.GetRenderJobStatus(job_id) or {}
            state = str(status.get("JobStatus", ""))
            if state in {"Complete", "Failed", "Cancelled", "Canceled"}:
                if state != "Complete":
                    raise ResolveError(f"Render ended with status {state}: {status}")
                return status
            time.sleep(1.0)


def _fps_string(fps: float) -> str:
    return str(int(fps)) if float(fps).is_integer() else str(fps)
