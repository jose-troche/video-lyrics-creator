import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from video_lyrics_creator.errors import VideoLyricsError
from video_lyrics_creator.handoff import load_workspace_job, stage_workspace_job
from video_lyrics_creator.manifest import new_manifest
from video_lyrics_creator.overlays import prepare_overlays
from video_lyrics_creator.planning import plan_scenes
from video_lyrics_creator.resolve import ResolveTimelineBuilder
from video_lyrics_creator.workspace import run_workspace_job
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

    def test_resolve_configuration_tolerates_read_only_playback_rate(self):
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

        builder._configure_project()

        project.SetSetting.assert_called_once_with("timelinePlaybackFrameRate", "30")


if __name__ == "__main__":
    unittest.main()
