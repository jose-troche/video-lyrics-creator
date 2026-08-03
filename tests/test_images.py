from pathlib import Path

import pytest
from PIL import Image

from video_lyrics import images
from video_lyrics.util import VideoLyricsError


def make_scenes():
    return [
        {"index": 1, "prompt": "a quiet field at dawn", "lines": ["Once I was dead"]},
        {"index": 2, "prompt": "a city at night", "lines": ["But grace came down"]},
    ]


def test_manual_provider_writes_a_prompt_manifest_and_no_images(tmp_path):
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")

    manifest = tmp_path / "prompts.txt"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert "a quiet field at dawn" in text
    assert "a city at night" in text
    assert all("image" not in scene for scene in scenes)


def test_manual_provider_adopts_hand_made_images_on_a_second_run(tmp_path):
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")

    manifest_lines = (tmp_path / "prompts.txt").read_text(encoding="utf-8").splitlines()
    filenames = [line.split("File: ", 1)[1] for line in manifest_lines if line.startswith("File: ")]
    assert len(filenames) == len(scenes)
    for name in filenames:
        Image.new("RGB", (100, 100), (10, 20, 30)).save(tmp_path / name)

    result = images.generate(scenes, images_dir=tmp_path, provider="manual")

    for scene in result:
        assert scene["image"]
        assert Path(scene["image"]).is_file()


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(VideoLyricsError):
        images.generate(make_scenes(), images_dir=tmp_path, provider="nonsense")
