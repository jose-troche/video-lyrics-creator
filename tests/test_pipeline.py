import os
import subprocess
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from video_lyrics_creator.alignment import TimedWord
from video_lyrics_creator.cli import main
from video_lyrics_creator.envfile import load_project_env
from video_lyrics_creator.errors import VideoLyricsError
from video_lyrics_creator.images import (
    _openai_size,
    generate_scene_images,
    preserve_scene_images_for_replan,
)
from video_lyrics_creator.manifest import load_manifest, new_manifest, save_manifest, validate_manifest
from video_lyrics_creator.overlays import prepare_overlays
from video_lyrics_creator.planning import plan_scenes
from video_lyrics_creator.resolve import seconds_to_frames, timeline_plan


def make_wav(path: Path, seconds: float = 12.0) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\0\0\0\0" * round(48000 * seconds))


class PipelineTests(unittest.TestCase):
    def test_default_image_settings_target_final_video(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            audio = base / "song.wav"
            lyrics = base / "lyrics.txt"
            make_wav(audio, 1.0)
            lyrics.write_text("Line\n", encoding="utf-8")
            manifest = new_manifest(
                title="Defaults",
                audio=str(audio),
                lyrics_source=str(lyrics),
                visual_style="cinematic realism",
                base=base,
            )
        self.assertEqual(manifest["video"]["width"], 1920)
        self.assertEqual(manifest["video"]["height"], 1080)
        self.assertEqual(manifest["image_generation"]["provider"], "codex")
        self.assertEqual(manifest["image_generation"]["quality"], "medium")
        self.assertEqual(_openai_size(1920, 1080), "1920x1088")

    def test_codex_provider_uses_chatgpt_auth_without_api_keys_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / "work" / "images" / "scene-001.png"
            manifest = {
                "work_dir": str(base / "work"),
                "video": {"width": 320, "height": 180},
                "image_generation": {
                    "provider": "codex",
                    "quality": "medium",
                    "codex_timeout": 120,
                },
                "scenes": [{"prompt": "Cinematic sunrise", "image": ""}],
            }
            calls = []

            def fake_run(command, **options):
                calls.append((command, options))
                if command[1:] == ["login", "status"]:
                    return subprocess.CompletedProcess(
                        command, 0, stdout="Logged in using ChatGPT\n", stderr=""
                    )
                self.assertIn("$imagegen", command[-1])
                self.assertIn(str(output.resolve()), command[-1])
                self.assertNotIn("OPENAI_API_KEY", options["env"])
                self.assertNotIn("CODEX_API_KEY", options["env"])
                Image.new("RGB", (640, 640), "blue").save(output)
                return subprocess.CompletedProcess(command, 0, stdout="done\n", stderr="")

            environment = {"OPENAI_API_KEY": "api-secret", "CODEX_API_KEY": "codex-secret"}
            with patch.dict(os.environ, environment, clear=False), patch(
                "video_lyrics_creator.images.shutil.which", return_value="/usr/bin/codex"
            ), patch("video_lyrics_creator.images.subprocess.run", side_effect=fake_run):
                generated = generate_scene_images(manifest)

            marker = output.with_suffix(".codex.sha256")
            with Image.open(output) as image:
                self.assertEqual(image.size, (320, 180))
            self.assertTrue(marker.is_file())
            self.assertEqual(generated, 1)
            self.assertEqual(len(calls), 2)

            with patch(
                "video_lyrics_creator.images.subprocess.run",
                side_effect=AssertionError("Codex should not run for a completed fingerprint"),
            ), patch(
                "video_lyrics_creator.images.shutil.which",
                side_effect=AssertionError("Cached images should not require a login check"),
            ):
                reused = generate_scene_images(manifest)
            self.assertEqual(reused, 0)

    def test_codex_provider_requires_chatgpt_managed_login(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = {
                "work_dir": str(Path(directory) / "work"),
                "video": {"width": 320, "height": 180},
                "image_generation": {"provider": "codex"},
                "scenes": [{"prompt": "Test", "image": ""}],
            }
            api_login = subprocess.CompletedProcess(
                ["codex", "login", "status"],
                0,
                stdout="Logged in using an API key\n",
                stderr="",
            )
            with patch(
                "video_lyrics_creator.images.shutil.which", return_value="/usr/bin/codex"
            ), patch("video_lyrics_creator.images.subprocess.run", return_value=api_login):
                with self.assertRaisesRegex(VideoLyricsError, "ChatGPT-managed"):
                    generate_scene_images(manifest)

    def test_generated_and_reused_images_fill_the_video_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = {
                "work_dir": str(base / "work"),
                "video": {"width": 1920, "height": 1080},
                "image_generation": {"provider": "placeholder"},
                "scenes": [{"prompt": "Test scene", "image": ""}],
            }
            generated = generate_scene_images(manifest)
            image_path = Path(manifest["scenes"][0]["image"])
            with Image.open(image_path) as image:
                self.assertEqual(image.size, (1920, 1080))

            Image.new("RGB", (800, 1200), "red").save(image_path)
            reused = generate_scene_images(manifest)
            with Image.open(image_path) as image:
                self.assertEqual(image.size, (1920, 1080))

        self.assertEqual(generated, 1)
        self.assertEqual(reused, 0)

    def test_openai_key_loads_from_project_env_without_overwriting_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / ".env").write_text("OPENAI_API_KEY=example-key\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_API_KEY", None)
                load_project_env(base)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "example-key")

    def test_scene_plan_is_contiguous(self):
        lyrics = [
            {"start": 1.0, "end": 3.0, "text": "Line one"},
            {"start": 4.0, "end": 6.0, "text": "Line two"},
            {"start": 8.0, "end": 10.0, "text": "Line three"},
        ]
        scenes = plan_scenes(lyrics, 12.0, "cinematic realism")
        self.assertEqual(scenes[0]["start"], 0.0)
        self.assertEqual(scenes[-1]["end"], 12.0)
        for left, right in zip(scenes, scenes[1:]):
            self.assertEqual(left["end"], right["start"])
        self.assertEqual(len(scenes), len(lyrics))
        self.assertNotEqual(scenes[0]["motion"], scenes[1]["motion"])
        self.assertIn("No words", scenes[0]["prompt"])

    def test_scene_plan_keeps_cross_dissolve_shorter_than_half_each_scene(self):
        lyrics = [
            {"start": 0.1, "end": 0.2, "text": "One"},
            {"start": 0.2, "end": 0.3, "text": "Two"},
            {"start": 0.3, "end": 0.4, "text": "Three"},
            {"start": 2.0, "end": 2.2, "text": "Four"},
            {"start": 2.1, "end": 2.4, "text": "Five"},
        ]
        transition = 0.75
        scenes = plan_scenes(lyrics, 4.0, "cinematic realism", transition_seconds=transition)
        if len(scenes) > 1:
            self.assertTrue(
                all(scene["end"] - scene["start"] > transition * 2 for scene in scenes)
            )

    def test_replan_preserves_one_existing_artwork_per_grouped_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "work" / "images" / "scene-001.png"
            source.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), "navy").save(source)
            previous = [
                {
                    "start": 0.0,
                    "end": 8.0,
                    "prompt": "combined prompt",
                    "image": str(source),
                }
            ]
            scenes = [
                {"start": 0.0, "end": 4.0, "prompt": "first", "image": ""},
                {"start": 4.0, "end": 8.0, "prompt": "second", "image": ""},
            ]

            reused = preserve_scene_images_for_replan(
                {"work_dir": str(base / "work")}, previous, scenes
            )

            self.assertEqual(reused, 1)
            self.assertTrue(Path(scenes[0]["image"]).is_file())
            self.assertEqual(scenes[0]["prompt"], "combined prompt")
            self.assertEqual(scenes[1]["image"], "")

    def test_prepare_replaces_invalid_stale_generated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            audio = base / "song.wav"
            lyrics_source = base / "lyrics.txt"
            manifest_path = base / "project.json"
            make_wav(audio, 4.0)
            lyrics_source.write_text("Document notes\nGrace has found me\n", encoding="utf-8")
            data = new_manifest(
                title="Test Song",
                audio=str(audio),
                lyrics_source=str(lyrics_source),
                visual_style="cinematic realism",
                base=base,
            )
            data["duration"] = 4.0
            data["lyrics"] = [
                {"start": 1.0, "end": 1.1, "text": "Old one"},
                {"start": 1.0, "end": 1.2, "text": "Old overlap"},
            ]
            data["scenes"] = [
                {"start": 0.0, "end": 0.02, "image": "", "prompt": "old"},
                {"start": 0.02, "end": 4.0, "image": "", "prompt": "old"},
            ]
            save_manifest(manifest_path, data)
            words = [
                TimedWord("grace", 1.0, 1.2),
                TimedWord("has", 1.3, 1.4),
                TimedWord("found", 1.5, 1.8),
                TimedWord("me", 1.9, 2.0),
            ]
            with patch("video_lyrics_creator.cli.transcribe_words", return_value=words), \
                redirect_stdout(StringIO()):
                result = main(["prepare", str(manifest_path)])
            _, prepared = load_manifest(manifest_path)
            duration = validate_manifest(prepared, require_images=False)

        self.assertEqual(result, 0)
        self.assertEqual(duration, 4.0)
        self.assertEqual([cue["text"] for cue in prepared["lyrics"]], ["Grace has found me"])

    def test_complete_manifest_and_resolve_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            audio = base / "song.wav"
            lyrics_source = base / "lyrics.txt"
            manifest_path = base / "project.json"
            make_wav(audio)
            lyrics_source.write_text("Line one\nLine two\n", encoding="utf-8")
            data = new_manifest(
                title="Test Song",
                audio=str(audio),
                lyrics_source=str(lyrics_source),
                visual_style="cinematic realism",
                base=base,
            )
            data["duration"] = 12.0
            data["lyrics"] = [
                {"start": 1.0, "end": 4.0, "text": "Line one"},
                {"start": 5.0, "end": 10.0, "text": "Line two"},
            ]
            data["scenes"] = plan_scenes(data["lyrics"], 12.0, data["visual_style"])
            image_dir = base / "work" / "images"
            image_dir.mkdir(parents=True)
            for index, scene in enumerate(data["scenes"], 1):
                image = image_dir / f"scene-{index:03d}.png"
                Image.new("RGB", (1920, 1080), "navy").save(image)
                scene["image"] = str(image)
            prepare_overlays(data)
            save_manifest(manifest_path, data)

            _, loaded = load_manifest(manifest_path)
            duration = validate_manifest(loaded)
            loaded["duration"] = duration
            plan = timeline_plan(loaded)

        self.assertAlmostEqual(duration, 12.0, places=2)
        self.assertEqual(plan["duration_frames"], 360)
        self.assertEqual(plan["scenes"][0]["track"], 1)
        self.assertEqual(plan["scenes"][1]["track"], 2)
        self.assertGreater(plan["scenes"][0]["end_frame"], plan["scenes"][1]["start_frame"])
        self.assertEqual(plan["title"]["duration_frames"], 360)
        self.assertEqual([cue["track"] for cue in plan["lyrics"]], [3, 4])
        self.assertLess(plan["lyrics"][0]["start_frame"], 30)
        self.assertGreater(plan["lyrics"][0]["fade_frames"], 0)

    def test_rounding_is_half_up(self):
        self.assertEqual(seconds_to_frames(0.5, 25), 13)


if __name__ == "__main__":
    unittest.main()
