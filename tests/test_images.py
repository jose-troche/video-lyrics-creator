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
    """If the raw downloads are already there (e.g. from a previous, interrupted
    run), the meta provider must not need Playwright or a browser at all - it
    should just convert what is on disk."""
    scenes = make_scenes()
    images_dir = tmp_path / "images"
    raw_dir = tmp_path / "images.src"
    images_dir.mkdir()
    raw_dir.mkdir()
    for scene in scenes:
        stem = images._stem_for(scene, "meta")
        Image.new("RGB", (100, 100), (5, 10, 15)).save(raw_dir / f"{stem}.webp", "WEBP")

    result = images.generate(
        scenes, images_dir=images_dir, raw_dir=raw_dir, provider="meta"
    )

    for scene in result:
        path = Path(scene["image"])
        assert path.suffix == ".png"
        assert path.parent == images_dir
        assert path.is_file()
    # the raw download is kept, unlike the manual provider's own-folder file
    for scene in scenes:
        stem = images._stem_for(scene, "meta")
        assert (raw_dir / f"{stem}.webp").is_file()


def test_meta_raw_downloads_default_to_a_sibling_of_the_images_folder(tmp_path):
    scenes = make_scenes()
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    raw_dir = tmp_path / "images.src"
    raw_dir.mkdir()
    for scene in scenes:
        stem = images._stem_for(scene, "meta")
        Image.new("RGB", (100, 100), (5, 10, 15)).save(raw_dir / f"{stem}.webp", "WEBP")

    result = images.generate(scenes, images_dir=images_dir, provider="meta")

    assert all(Path(scene["image"]).parent == images_dir for scene in result)
    assert not (images_dir / "images.src").exists()


def test_limit_backfills_the_earliest_gap_first_then_carries_on(tmp_path, monkeypatch):
    """`--limit N` takes the first N scenes with no raw download, in scene order.

    So it resumes where it left off, and a file deleted from the middle is picked
    up before later ones - the existing images either side are left alone.
    """
    from video_lyrics import meta_ai

    asked: list[int] = []
    monkeypatch.setattr(
        meta_ai, "generate", lambda scenes, **kw: asked.extend(s["index"] for s in scenes)
    )

    all_scenes = [
        {"index": i, "prompt": f"prompt {i}", "lines": [f"line {i}"]} for i in range(1, 7)
    ]
    images_dir, raw_dir = tmp_path / "images", tmp_path / "images.src"
    images_dir.mkdir()
    raw_dir.mkdir()
    for scene in all_scenes:
        if scene["index"] in (1, 3):  # 2 is the hole in the middle
            stem = images._stem_for(scene, "meta")
            Image.new("RGB", (400, 225), (9, 9, 9)).save(raw_dir / f"{stem}.webp", "WEBP")

    images.generate(
        list(all_scenes), images_dir=images_dir, raw_dir=raw_dir, provider="meta", limit=2
    )
    assert asked == [2, 4]


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(VideoLyricsError):
        images.generate(make_scenes(), images_dir=tmp_path, provider="nonsense")
