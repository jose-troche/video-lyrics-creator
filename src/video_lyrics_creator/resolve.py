from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    lyric_lead = max(0.0, float(video.get("lyric_lead", 0.35)))
    lyric_fade = max(0.0, float(video.get("lyric_fade", 0.2)))
    planned_lyrics = []
    for index, item in enumerate(manifest["overlays"]["lyrics"]):
        cue_start = float(item["start"])
        cue_end = float(item["end"])
        clip_start = max(0.0, cue_start - lyric_lead - lyric_fade)
        clip_end = min(duration, cue_end + lyric_fade)
        duration_frames = max(1, seconds_to_frames(clip_end - clip_start, fps))
        fade_frames = min(
            seconds_to_frames(lyric_fade, fps), max(1, (duration_frames - 1) // 2)
        )
        planned_lyrics.append(
            {
                **item,
                "track": 3 + index % 2,
                "start_frame": seconds_to_frames(clip_start, fps),
                "duration_frames": duration_frames,
                "fade_frames": fade_frames,
            }
        )

    title_duration = min(float(video["title_duration"]), duration)
    title_frames = max(1, seconds_to_frames(title_duration, fps))
    title_fade_frames = min(
        seconds_to_frames(max(0.0, float(video.get("title_fade", 0.75))), fps),
        max(1, (title_frames - 1) // 2),
    )
    return {
        "fps": fps,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration_frames": seconds_to_frames(duration, fps),
        "scenes": scenes,
        "lyrics": planned_lyrics,
        "title": {
            "image": manifest["overlays"]["title"],
            "track": 5,
            "start_frame": 0,
            "duration_frames": title_frames,
            "fade_frames": title_fade_frames,
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
    def __init__(
        self,
        resolve: Any,
        manifest: dict,
        progress: Callable[[str], None] | None = None,
    ):
        self.resolve = resolve
        self.manifest = manifest
        self.plan = timeline_plan(manifest)
        self.project = None
        self.media_pool = None
        self.timeline = None
        self.progress = progress or (lambda _message: None)

    def build(
        self,
        *,
        project_name: str,
        timeline_name: str,
        replace_timeline: bool = False,
        render: bool = False,
        wait: bool = True,
    ) -> BuildResult:
        self._report(f"Opening Resolve project: {project_name}")
        self._open_project(project_name)
        self._report("Configuring 1920x1080 timeline settings")
        self._configure_project()
        self._report(f"Creating timeline: {timeline_name}")
        self._create_timeline(timeline_name, replace_timeline)
        self._ensure_tracks()
        self._report("Adding the original audio")
        self._append_audio()
        self._append_scenes()
        self._append_overlays()
        self._report("Adding lyric review markers")
        self._add_review_markers()
        manager = self.resolve.GetProjectManager()
        if not manager.SaveProject():
            raise ResolveError("Resolve did not save the project")

        result = BuildResult(project_name=project_name, timeline_name=timeline_name)
        if render:
            self._report("Starting Resolve video render")
            result.render_job_id = self._queue_render()
            if not self.project.StartRendering([result.render_job_id], False):
                raise ResolveError("Resolve did not start the render job")
            if wait:
                result.render_status = self._wait_for_render(result.render_job_id)
        return result

    def _report(self, message: str) -> None:
        self.progress(message)

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
            # timelinePlaybackFrameRate is not a supported scripting setting in Resolve 21 Free.
            # The supported timeline rate must be set before adding timeline media.
            "timelineFrameRate": _fps_string(self.plan["fps"]),
            "timelineSampleRate": "48000",
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
                + ". Resolve locks timeline settings after timeline media exists; stage this "
                "job with a new --project-name."
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
        while self.timeline.GetTrackCount("video") < 5:
            if not self.timeline.AddTrack("video"):
                raise ResolveError("Resolve could not add a video track")
        if self.timeline.GetTrackCount("audio") < 1 and not self.timeline.AddTrack("audio", "stereo"):
            raise ResolveError("Resolve could not add an audio track")
        for index, name in enumerate(
            ("Scenes A", "Scenes B", "Lyrics A", "Lyrics B", "Title"), 1
        ):
            self.timeline.SetTrackName("video", index, name)
        self.timeline.SetTrackName("audio", 1, "Original Song")

    def _import(self, path: str):
        items = self.media_pool.ImportMedia([str(Path(path).resolve())])
        if not items:
            raise ResolveError(f"Resolve could not import media: {path}")
        return items[0]

    def _append_clip(
        self,
        media_item: Any,
        *,
        track: int,
        start_frame: int,
        duration_frames: int | None,
        media_type: int = 1,
    ):
        timeline_start = int(self.timeline.GetStartFrame())
        info = {
            "mediaPoolItem": media_item,
            "mediaType": media_type,
            "trackIndex": track,
            "recordFrame": timeline_start + start_frame,
        }
        if duration_frames is not None:
            info["startFrame"] = 0
            info["endFrame"] = max(0, duration_frames - 1)
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
            duration_frames=None,
            media_type=2,
        )

    def _append_scenes(self) -> None:
        scale_fill = getattr(self.resolve, "SCALE_FILL", 2)
        zoom = float(self.manifest["video"].get("zoom", 1.08))
        for scene in self.plan["scenes"]:
            self._report(
                f"Adding animated image scene {scene['index']}/{len(self.plan['scenes'])}"
            )
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
            self._add_spline(comp, transform, "Size", f"scene {scene['index']} zoom")
            transform.Size[0] = start_zoom
            transform.Size[last] = end_zoom

            transition = min(int(scene["transition_frames"]), max(1, last // 2))
            self._add_spline(comp, merge, "Blend", f"scene {scene['index']} dissolve")
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

    @staticmethod
    def _add_spline(comp: Any, tool: Any, input_name: str, label: str) -> None:
        spline = comp.BezierSpline()
        if not spline:
            raise ResolveError(f"Fusion could not animate {label}")
        setattr(tool, input_name, spline)

    def _configure_overlay_fade(
        self, clip: Any, *, duration_frames: int, fade_frames: int, label: str
    ) -> None:
        comp = clip.AddFusionComp()
        if not comp:
            raise ResolveError(f"Resolve could not add Fusion fade to {label}")
        comp.Lock()
        try:
            media_in = comp.FindTool("MediaIn1")
            media_out = comp.FindTool("MediaOut1")
            if not media_in or not media_out:
                raise ResolveError(f"Fusion MediaIn/MediaOut missing on {label}")
            background = comp.AddTool("Background", 0, 1)
            merge = comp.AddTool("Merge", 1, 1)
            if not background or not merge:
                raise ResolveError(f"Fusion could not create fade nodes for {label}")
            background.SetInput("UseFrameFormatSettings", 1.0)
            for corner in (
                "TopLeftAlpha",
                "TopRightAlpha",
                "BottomLeftAlpha",
                "BottomRightAlpha",
            ):
                background.SetInput(corner, 0.0)
            merge.Background = background.Output
            merge.Foreground = media_in.Output
            media_out.Input = merge.Output

            last = max(1, int(duration_frames) - 1)
            fade = min(max(1, int(fade_frames)), max(1, last // 2))
            self._add_spline(comp, merge, "Blend", f"{label} opacity")
            merge.Blend[0] = 0.0
            merge.Blend[min(fade, last)] = 1.0
            merge.Blend[max(0, last - fade)] = 1.0
            merge.Blend[last] = 0.0
        finally:
            comp.Unlock()

    def _append_overlays(self) -> None:
        self._report("Adding the fading title overlay")
        title = self._import(self.plan["title"]["image"])
        title_clip = self._append_clip(
            title,
            track=self.plan["title"]["track"],
            start_frame=self.plan["title"]["start_frame"],
            duration_frames=self.plan["title"]["duration_frames"],
        )
        self._configure_overlay_fade(
            title_clip,
            duration_frames=self.plan["title"]["duration_frames"],
            fade_frames=self.plan["title"]["fade_frames"],
            label="title",
        )
        for index, cue in enumerate(self.plan["lyrics"], 1):
            self._report(f"Adding fading lyric overlay {index}/{len(self.plan['lyrics'])}")
            item = self._import(cue["image"])
            clip = self._append_clip(
                item,
                track=cue["track"],
                start_frame=cue["start_frame"],
                duration_frames=cue["duration_frames"],
            )
            self._configure_overlay_fade(
                clip,
                duration_frames=cue["duration_frames"],
                fade_frames=cue["fade_frames"],
                label=f"lyric {index}",
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
        required_settings = {
            "SelectAllFrames": True,
            "TargetDir": str(target.parent),
            "CustomName": target.stem,
            "ExportVideo": True,
            # The original WAV is muxed after Resolve renders, avoiding Resolve's AAC artifacts.
            "ExportAudio": False,
        }
        optional_settings = {
            "FormatWidth": self.plan["width"],
            "FormatHeight": self.plan["height"],
            "FrameRate": self.plan["fps"],
            "VideoQuality": "Best",
            "NetworkOptimization": True,
            "ReplaceExistingFilesInPlace": bool(render.get("replace_existing", True)),
        }
        failed = [
            key
            for key, value in required_settings.items()
            if not self.project.SetRenderSettings({key: value})
        ]
        if failed:
            raise ResolveError("Resolve rejected required render setting(s): " + ", ".join(failed))
        for key, value in optional_settings.items():
            if not self.project.SetRenderSettings({key: value}):
                print(f"Video Lyrics Creator: Resolve ignored optional render setting {key}.")
        job_id = self.project.AddRenderJob()
        if not job_id:
            raise ResolveError("Resolve could not add a render job")
        return job_id

    def _wait_for_render(self, job_id: str) -> dict:
        last_progress_bucket = -1
        while True:
            status = self.project.GetRenderJobStatus(job_id) or {}
            state = str(status.get("JobStatus", ""))
            try:
                progress = max(0, min(100, int(float(status.get("CompletionPercentage", 0)))))
            except (TypeError, ValueError):
                progress = 0
            progress_bucket = progress // 5
            if progress_bucket != last_progress_bucket:
                self._report(f"Rendering video: {progress}%")
                last_progress_bucket = progress_bucket
            if state in {"Complete", "Failed", "Cancelled", "Canceled"}:
                if state != "Complete":
                    raise ResolveError(f"Render ended with status {state}: {status}")
                return status
            time.sleep(1.0)


def _fps_string(fps: float) -> str:
    return str(int(fps)) if float(fps).is_integer() else str(fps)
