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


def test_manual_provider_adopts_hand_made_png_on_a_second_run(tmp_path):
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")

    for scene in scenes:
        stem = images._stem_for(scene, "manual")
        Image.new("RGB", (100, 100), (10, 20, 30)).save(tmp_path / f"{stem}.png")

    result = images.generate(scenes, images_dir=tmp_path, provider="manual")

    for scene in result:
        assert scene["image"]
        path = Path(scene["image"])
        assert path.is_file()
        assert path.suffix == ".png"


def test_manual_provider_accepts_webp_and_converts_it_to_png(tmp_path):
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")

    for scene in scenes:
        stem = images._stem_for(scene, "manual")
        Image.new("RGB", (100, 100), (10, 20, 30)).save(tmp_path / f"{stem}.webp", "WEBP")

    result = images.generate(scenes, images_dir=tmp_path, provider="manual")

    for scene in result:
        path = Path(scene["image"])
        assert path.suffix == ".png"
        assert path.is_file()
        assert not path.with_suffix(".webp").is_file()


def test_manual_provider_accepts_mixed_formats_in_one_run(tmp_path):
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")

    stems = [images._stem_for(scene, "manual") for scene in scenes]
    Image.new("RGB", (100, 100), (10, 20, 30)).save(tmp_path / f"{stems[0]}.png")
    Image.new("RGB", (100, 100), (40, 50, 60)).save(tmp_path / f"{stems[1]}.webp", "WEBP")

    result = images.generate(scenes, images_dir=tmp_path, provider="manual")

    for scene in result:
        path = Path(scene["image"])
        assert path.suffix == ".png"
        assert path.is_file()


def test_meta_provider_adopts_raw_downloads_already_on_disk(tmp_path):
    """If images.src already has a file for every scene (e.g. from a previous,
    interrupted run), the meta provider must not need Playwright or a browser at
    all - it should just convert what is already there."""
    scenes = make_scenes()
    raw_dir = tmp_path / "images.src"
    raw_dir.mkdir()
    for scene in scenes:
        stem = images._stem_for(scene, "meta")
        Image.new("RGB", (100, 100), (5, 10, 15)).save(raw_dir / f"{stem}.webp", "WEBP")

    result = images.generate(scenes, images_dir=tmp_path, provider="meta")

    for scene in result:
        path = Path(scene["image"])
        assert path.suffix == ".png"
        assert path.parent == tmp_path
        assert path.is_file()
    # the raw download is kept, unlike the manual provider's own-folder file
    for scene in scenes:
        stem = images._stem_for(scene, "meta")
        assert (raw_dir / f"{stem}.webp").is_file()


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(VideoLyricsError):
        images.generate(make_scenes(), images_dir=tmp_path, provider="nonsense")
