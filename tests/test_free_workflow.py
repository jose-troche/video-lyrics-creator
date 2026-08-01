import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from video_lyrics_creator.errors import ResolveError, VideoLyricsError
from video_lyrics_creator.handoff import load_workspace_job, stage_workspace_job
from video_lyrics_creator.manifest import new_manifest
from video_lyrics_creator.overlays import prepare_overlays
from video_lyrics_creator.planning import plan_scenes
from video_lyrics_creator.resolve import ResolveTimelineBuilder
from video_lyrics_creator.workspace import _mux_original_audio, run_workspace_job
from video_lyrics_creator.workspace_install import (
    install_workspace_script,
    macos_resolve_python_runtimes,
    require_resolve_python_runtime,
)


def _audio(path: Path, seconds: float = 6.0) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\0\0\0\0" * round(48000 * seconds))


def _complete_manifest(base: Path) -> tuple[Path, dict]:
    audio = base / "source.wav"
    lyrics_file = base / "lyrics.txt"
    manifest_path = base / "project.json"
    _audio(audio)
    lyrics_file.write_text("First line\nSecond line\n", encoding="utf-8")
    manifest = new_manifest(
        title="Free Resolve Test",
        audio=str(audio),
        lyrics_source=str(lyrics_file),
        visual_style="cinematic realism",
        base=base,
    )
    manifest["duration"] = 6.0
    manifest["lyrics"] = [
        {"start": 0.5, "end": 2.4, "text": "First line"},
        {"start": 3.0, "end": 5.5, "text": "Second line"},
    ]
    manifest["scenes"] = plan_scenes(manifest["lyrics"], 6.0, manifest["visual_style"])
    image_dir = base / "work" / "images"
    image_dir.mkdir(parents=True)
    for index, scene in enumerate(manifest["scenes"], 1):
        image = image_dir / f"scene-{index:03d}.png"
        Image.new("RGB", (1920, 1080), "navy").save(image)
        scene["image"] = str(image)
    prepare_overlays(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, manifest


class FreeWorkflowTests(unittest.TestCase):
    def test_handoff_stages_every_resolve_input(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest_path, manifest = _complete_manifest(base)
            root = (base / "handoff").resolve()
            job_path, job = stage_workspace_job(
                manifest_path,
                manifest,
                project_name="Project",
                timeline_name="Timeline",
                replace_timeline=False,
                render=True,
                handoff_root=root,
            )
            loaded = load_workspace_job(job_path)
            staged = loaded["manifest"]
            paths = [staged["audio"], staged["overlays"]["title"]]
            paths.extend(scene["image"] for scene in staged["scenes"])
            paths.extend(item["image"] for item in staged["overlays"]["lyrics"])

            self.assertTrue((root / "latest-job.json").is_file())
            self.assertTrue(all(Path(path).is_file() for path in paths))
            self.assertTrue(all(str(Path(path)).startswith(str(root)) for path in paths))
            self.assertTrue(all(Path(scene["image"]).suffix == ".mp4" for scene in staged["scenes"]))
            self.assertTrue(Path(staged["resolve_job"]["ffmpeg"]).is_file())
            self.assertTrue(staged["render"]["output"].startswith(str(root / "Output")))
            self.assertEqual(job["job_id"], loaded["job_id"])

    def test_installer_creates_menu_launcher_and_internal_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Fusion" / "Scripts"
            result = install_workspace_script(root, check_python=False)
            launcher = Path(result["launcher"])
            module_dir = Path(result["module_dir"])
            self.assertTrue(launcher.is_file())
            self.assertTrue((module_dir / "workspace.py").is_file())
            self.assertTrue((module_dir / "resolve.py").is_file())
            self.assertNotIn("connect_resolve", (module_dir / "resolve.py").read_text(encoding="utf-8"))
            self.assertIn(str(module_dir.parent), launcher.read_text(encoding="utf-8"))

    def test_macos_python_runtime_detection_requires_framework_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Versions"
            (root / "3.10").mkdir(parents=True)
            self.assertEqual(macos_resolve_python_runtimes(root), [])
            with patch("video_lyrics_creator.workspace_install.sys.platform", "darwin"):
                with self.assertRaisesRegex(VideoLyricsError, "cannot discover a host Python"):
                    require_resolve_python_runtime(root)

            runtime = root / "3.10" / "Python"
            runtime.touch()
            self.assertEqual(macos_resolve_python_runtimes(root), [runtime])
            with patch("video_lyrics_creator.workspace_install.sys.platform", "darwin"):
                self.assertEqual(require_resolve_python_runtime(root), [runtime])

    def test_internal_job_runs_once_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest_path, manifest = _complete_manifest(base)
            job_path, _ = stage_workspace_job(
                manifest_path,
                manifest,
                project_name="Project",
                timeline_name="Timeline",
                replace_timeline=False,
                render=False,
                handoff_root=base / "handoff",
            )

            fake_result = SimpleNamespace(
                project_name="Project",
                timeline_name="Timeline",
                render_job_id=None,
                render_status=None,
            )
            fake_builder = Mock()
            fake_builder.build.return_value = fake_result
            with patch("video_lyrics_creator.workspace.ResolveTimelineBuilder", return_value=fake_builder):
                result = run_workspace_job(object(), job_path)

            self.assertEqual(result["status"], "complete")
            self.assertIsNone(result["output"])
            with self.assertRaises(VideoLyricsError):
                run_workspace_job(object(), job_path)

    def test_resolve_configuration_rejects_a_locked_mismatched_playback_rate(self):
        project = Mock()
        project.GetSetting.side_effect = {
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
            "timelineFrameRate": "30",
            "timelinePlaybackFrameRate": "24",
        }.get
        project.SetSetting.side_effect = lambda key, value: key != "timelinePlaybackFrameRate"
        builder = ResolveTimelineBuilder.__new__(ResolveTimelineBuilder)
        builder.project = project
        builder.plan = {"width": 1920, "height": 1080, "fps": 30.0}

        with self.assertRaisesRegex(ResolveError, "timelinePlaybackFrameRate"):
            builder._configure_project()

        self.assertIn(
            ("timelinePlaybackFrameRate", "30"),
            [call.args for call in project.SetSetting.call_args_list],
        )

    def test_resolve_configuration_sets_playback_rate_before_timeline_rate(self):
        project = Mock()
        project.GetSetting.return_value = ""
        project.SetSetting.return_value = True
        builder = ResolveTimelineBuilder.__new__(ResolveTimelineBuilder)
        builder.project = project
        builder.plan = {"width": 1920, "height": 1080, "fps": 30.0}

        builder._configure_project()

        keys = [call.args[0] for call in project.SetSetting.call_args_list]
        self.assertLess(keys.index("timelinePlaybackFrameRate"), keys.index("timelineFrameRate"))
        self.assertIn("timelineSampleRate", keys)

    def test_fusion_animation_requires_an_explicit_spline_modifier(self):
        comp = Mock()
        tool = Mock()
        spline = Mock()
        comp.BezierSpline.return_value = spline

        ResolveTimelineBuilder._add_spline(comp, tool, "Blend", "test fade")

        comp.BezierSpline.assert_called_once_with()
        self.assertIs(tool.Blend, spline)
        comp.BezierSpline.return_value = None
        with self.assertRaisesRegex(ResolveError, "test fade"):
            ResolveTimelineBuilder._add_spline(comp, tool, "Blend", "test fade")

    def test_audio_append_uses_the_complete_source_without_video_frame_trimming(self):
        media_item = object()
        timeline_item = object()
        media_pool = Mock()
        media_pool.ImportMedia.return_value = [media_item]
        media_pool.AppendToTimeline.return_value = [timeline_item]
        timeline = Mock()
        timeline.GetStartFrame.return_value = 0
        builder = ResolveTimelineBuilder.__new__(ResolveTimelineBuilder)
        builder.plan = {"audio": "/tmp/source.wav"}
        builder.media_pool = media_pool
        builder.timeline = timeline

        builder._append_audio()

        clip_info = media_pool.AppendToTimeline.call_args.args[0][0]
        self.assertEqual(clip_info["mediaPoolItem"], media_item)
        self.assertEqual(clip_info["mediaType"], 2)
        self.assertNotIn("startFrame", clip_info)
        self.assertNotIn("endFrame", clip_info)

    def test_final_audio_is_muxed_from_the_original_wav_at_320k(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "video.mp4"
            audio = root / "audio.wav"
            executable = root / "ffmpeg"
            output.write_bytes(b"resolve-video")
            audio.write_bytes(b"source-audio")
            executable.touch()

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"final-video")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("video_lyrics_creator.workspace.subprocess.run", side_effect=fake_run) as run:
                _mux_original_audio(output, audio, str(executable))

            command = run.call_args.args[0]
            self.assertIn("320k", command)
            self.assertEqual(output.read_bytes(), b"final-video")

    def test_resolve_render_is_video_only_before_original_audio_mux(self):
        project = Mock()
        project.SetCurrentRenderFormatAndCodec.return_value = True
        project.SetRenderSettings.return_value = True
        project.AddRenderJob.return_value = "render-job"
        builder = ResolveTimelineBuilder.__new__(ResolveTimelineBuilder)
        builder.project = project
        builder.manifest = {
            "render": {
                "output": "/tmp/final.mp4",
                "format": "mp4",
                "codec": "H264",
                "replace_existing": True,
            }
        }
        builder.plan = {"width": 1920, "height": 1080, "fps": 30.0}

        self.assertEqual(builder._queue_render(), "render-job")

        settings = [call.args[0] for call in project.SetRenderSettings.call_args_list]
        self.assertIn({"ExportVideo": True}, settings)
        self.assertIn({"ExportAudio": False}, settings)


if __name__ == "__main__":
    unittest.main()
