import importlib
from pathlib import Path

import pytest
from PIL import Image

from video_lyrics import images, pipeline
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


def test_a_wrongly_shaped_image_is_cropped_to_16_9_and_re_saved_as_png(tmp_path):
    """A re-encode is the one case that changes the file. It goes out as PNG,
    whatever came in, so a lossy source is never compressed a second time."""
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
        with Image.open(path) as image:
            assert abs(image.width / image.height - 16 / 9) < 0.01


def test_an_image_that_is_already_usable_is_kept_exactly_as_served(tmp_path):
    """The generator's own file is the one that is kept. Nothing about a 16:9 RGB
    webp needs fixing, so it is not rewritten - not even into PNG."""
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")

    served = {}
    for scene in scenes:
        stem = images._stem_for(scene, "manual")
        path = tmp_path / f"{stem}.webp"
        Image.new("RGB", (1600, 900), (10, 20, 30)).save(path, "WEBP")
        served[stem] = path.read_bytes()

    result = images.generate(scenes, images_dir=tmp_path, provider="manual")

    for scene in result:
        path = Path(scene["image"])
        assert path.suffix == ".webp"
        assert path.read_bytes() == served[path.stem]


def test_an_image_smaller_than_the_render_is_not_upscaled(tmp_path):
    """The motion stage scales every image on its way into ffmpeg, so upscaling
    here would only interpolate twice and store the bigger of the two."""
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")
    for scene in scenes:
        stem = images._stem_for(scene, "manual")
        Image.new("RGB", (640, 360), (10, 20, 30)).save(tmp_path / f"{stem}.png")

    result = images.generate(
        scenes, images_dir=tmp_path, provider="manual", size=(1920, 1080)
    )

    for scene in result:
        with Image.open(scene["image"]) as image:
            assert image.size == (640, 360)


def test_manual_provider_accepts_mixed_formats_in_one_run(tmp_path):
    scenes = make_scenes()
    images.generate(scenes, images_dir=tmp_path, provider="manual")

    stems = [images._stem_for(scene, "manual") for scene in scenes]
    Image.new("RGB", (1600, 900), (10, 20, 30)).save(tmp_path / f"{stems[0]}.png")
    Image.new("RGB", (1600, 900), (40, 50, 60)).save(tmp_path / f"{stems[1]}.webp", "WEBP")

    result = images.generate(scenes, images_dir=tmp_path, provider="manual")

    assert [Path(scene["image"]).suffix for scene in result] == [".png", ".webp"]
    assert all(Path(scene["image"]).is_file() for scene in result)


def test_one_scene_left_in_two_formats_keeps_only_the_one_that_is_used(tmp_path):
    """A run cut short can leave a fresh download beside an older image under the
    same stem. Only the first is ever reachable, so the other is dropped rather
    than left to be committed."""
    scenes = make_scenes()
    stem = images._stem_for(scenes[0], "manual")
    Image.new("RGB", (1600, 900), (10, 20, 30)).save(tmp_path / f"{stem}.png")
    Image.new("RGB", (1600, 900), (40, 50, 60)).save(tmp_path / f"{stem}.webp", "WEBP")

    result = images.generate(scenes, images_dir=tmp_path, provider="manual")

    assert result[0]["image"] == str(tmp_path / f"{stem}.png")
    assert not (tmp_path / f"{stem}.webp").exists()


def site_module(provider):
    return importlib.import_module(f"video_lyrics.{images.BROWSER_MODULES[provider]}")


@pytest.mark.parametrize("provider", images.BROWSER_PROVIDERS)
def test_browser_provider_adopts_downloads_already_on_disk(tmp_path, provider):
    """If the downloads are already there (e.g. from a previous, interrupted run),
    a browser provider must not need Playwright or a browser at all - it should
    just take what is on disk."""
    scenes = make_scenes()
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    for scene in scenes:
        stem = images._stem_for(scene, provider)
        Image.new("RGB", (1600, 900), (5, 10, 15)).save(images_dir / f"{stem}.webp", "WEBP")

    result = images.generate(scenes, images_dir=images_dir, provider=provider)

    for scene in result:
        path = Path(scene["image"])
        assert path.parent == images_dir
        assert path.suffix == ".webp"   # the download itself, not a copy of it
        assert path.is_file()


@pytest.mark.parametrize("provider", images.BROWSER_PROVIDERS)
def test_each_provider_has_its_own_fingerprint(tmp_path, provider):
    """One site's downloads are never mistaken for another's: same prompt, same
    scene, different stem - so switching provider regenerates rather than
    silently adopting the other site's picture."""
    other = next(name for name in images.BROWSER_PROVIDERS if name != provider)
    scene = make_scenes()[0]
    assert images._stem_for(scene, provider) != images._stem_for(scene, other)


def test_a_browser_run_makes_no_second_folder_of_its_own(tmp_path):
    """Downloads used to be kept in a sibling images.src/ and converted across into
    images/. There is one folder now, and nothing should recreate the other."""
    scenes = make_scenes()
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    for scene in scenes:
        stem = images._stem_for(scene, "meta")
        Image.new("RGB", (1600, 900), (5, 10, 15)).save(images_dir / f"{stem}.webp", "WEBP")

    result = images.generate(scenes, images_dir=images_dir, provider="meta")

    assert all(Path(scene["image"]).parent == images_dir for scene in result)
    assert [path.name for path in tmp_path.iterdir()] == ["images"]
    assert all(path.is_file() for path in images_dir.iterdir())


@pytest.mark.parametrize("provider", images.BROWSER_PROVIDERS)
def test_limit_backfills_the_earliest_gap_first_then_carries_on(
    tmp_path, monkeypatch, provider
):
    """`--limit N` takes the first N scenes with no image, in scene order.

    So it resumes where it left off, and a file deleted from the middle is picked
    up before later ones - the existing images either side are left alone.
    """
    asked: list[int] = []
    monkeypatch.setattr(
        site_module(provider), "generate",
        lambda scenes, **kw: asked.extend(s["index"] for s in scenes),
    )

    all_scenes = [
        {"index": i, "prompt": f"prompt {i}", "lines": [f"line {i}"]} for i in range(1, 7)
    ]
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    for scene in all_scenes:
        if scene["index"] in (1, 3):  # 2 is the hole in the middle
            stem = images._stem_for(scene, provider)
            Image.new("RGB", (400, 225), (9, 9, 9)).save(images_dir / f"{stem}.webp", "WEBP")

    images.generate(
        list(all_scenes), images_dir=images_dir, provider=provider, limit=2,
    )
    assert asked == [2, 4]


def test_a_scene_that_already_has_an_image_is_not_regenerated(tmp_path, monkeypatch):
    """An image made by another provider (a song generated before chatgpt replaced
    codex, say) has no chatgpt-stemmed download - but it is still a finished image,
    and asking the browser to redraw it would throw the work away."""
    asked: list[int] = []
    monkeypatch.setattr(
        site_module("chatgpt"), "generate",
        lambda scenes, **kw: asked.extend(s["index"] for s in scenes),
    )

    scenes = make_scenes()
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    existing = images_dir / "scene-001-fromcodex.png"
    Image.new("RGB", (400, 225), (7, 7, 7)).save(existing)
    scenes[0]["image"] = str(existing)

    result = images.generate(scenes, images_dir=images_dir, provider="chatgpt")

    assert asked == [2]                        # only the scene with nothing on disk
    assert result[0]["image"] == str(existing)  # ... and the old image is untouched


def test_force_regenerates_even_a_scene_that_has_an_image(tmp_path, monkeypatch):
    asked: list[int] = []
    monkeypatch.setattr(
        site_module("chatgpt"), "generate",
        lambda scenes, **kw: asked.extend(s["index"] for s in scenes),
    )

    scenes = make_scenes()
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    existing = images_dir / "scene-001-fromcodex.png"
    Image.new("RGB", (400, 225), (7, 7, 7)).save(existing)
    scenes[0]["image"] = str(existing)

    images.generate(scenes, images_dir=images_dir, provider="chatgpt", force=True)
    assert asked == [1, 2]


def test_a_scene_about_to_be_redrawn_is_cleared_first(tmp_path, monkeypatch):
    """One folder means a fresh download can land beside the image it replaces -
    and if the two are in different formats, the next run picks whichever suffix
    comes first rather than the new one. So the stem is emptied before asking."""
    monkeypatch.setattr(site_module("chatgpt"), "generate", lambda scenes, **kw: None)

    scenes = make_scenes()
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    stems = [images._stem_for(scene, "chatgpt") for scene in scenes]
    for stem in stems:
        Image.new("RGB", (1600, 900), (7, 7, 7)).save(images_dir / f"{stem}.png")

    images.generate(scenes, images_dir=images_dir, provider="chatgpt", force=True)

    assert list(images_dir.iterdir()) == []


# ------------------------------------------------------- redrawing one scene

def redraw_fixture(tmp_path, monkeypatch, provider="chatgpt"):
    """Three scenes, all with an image already, and a record of what was asked for."""
    asked: list[int] = []
    monkeypatch.setattr(
        site_module(provider), "generate",
        lambda scenes, **kw: asked.extend(s["index"] for s in scenes),
    )
    scenes = [
        {"index": i, "prompt": f"prompt {i}", "lines": [f"line {i}"]} for i in (1, 2, 3)
    ]
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    for scene in scenes:
        stem = images._stem_for(scene, provider)
        Image.new("RGB", (1600, 900), (3, 3, 3)).save(images_dir / f"{stem}.png")
        scene["image"] = str(images_dir / f"{stem}.png")
    return scenes, images_dir, asked


def test_naming_a_scene_redraws_it_even_though_its_image_is_there(tmp_path, monkeypatch):
    scenes, images_dir, asked = redraw_fixture(tmp_path, monkeypatch)

    images.generate(scenes, images_dir=images_dir, provider="chatgpt", redraw=(2,))

    assert asked == [2]


def test_naming_a_scene_draws_nothing_else(tmp_path, monkeypatch):
    """Not even a scene with no image at all: `--scene` is a deliberate "this one",
    and quietly backfilling the rest would be answering a different question."""
    scenes, images_dir, asked = redraw_fixture(tmp_path, monkeypatch)
    Path(scenes[2]["image"]).unlink()
    scenes[2].pop("image")

    images.generate(scenes, images_dir=images_dir, provider="chatgpt", redraw=(1,))

    assert asked == [1]


def test_a_prompt_edited_without_naming_the_scene_still_keeps_its_image(tmp_path, monkeypatch):
    """The check this is guarding: `plan` rewrites every prompt in the song and
    carries the pictures across, so a prompt that no longer matches its image is
    the normal state of a re-planned song - not a signal to redraw."""
    scenes, images_dir, asked = redraw_fixture(tmp_path, monkeypatch)
    scenes[1]["prompt"] = "edited by hand, but nothing says to redraw it"

    images.generate(scenes, images_dir=images_dir, provider="chatgpt")

    assert asked == []


def test_the_scene_being_redrawn_has_its_old_file_cleared_first(tmp_path, monkeypatch):
    """Same prompt, so the same stem: the file has to go before the browser is
    asked, or a download in another format would land beside it."""
    scenes, images_dir, _ = redraw_fixture(tmp_path, monkeypatch)
    doomed = Path(scenes[1]["image"])

    images.generate(scenes, images_dir=images_dir, provider="chatgpt", redraw=(2,))

    assert not doomed.exists()
    assert all(Path(scenes[i]["image"]).is_file() for i in (0, 2))


def test_naming_several_scenes_redraws_each_of_them(tmp_path, monkeypatch):
    scenes, images_dir, asked = redraw_fixture(tmp_path, monkeypatch)

    images.generate(scenes, images_dir=images_dir, provider="chatgpt", redraw=(3, 1))

    assert sorted(asked) == [1, 3]


def test_the_manual_provider_takes_a_named_scene_too(tmp_path):
    scenes = make_scenes()
    for scene in scenes:
        stem = images._stem_for(scene, "manual")
        Image.new("RGB", (1600, 900), (3, 3, 3)).save(tmp_path / f"{stem}.png")

    images.generate(scenes, images_dir=tmp_path, provider="manual", redraw=(2,))

    manifest = (tmp_path / "prompts.txt").read_text(encoding="utf-8")
    assert "a city at night" in manifest        # scene 2, asked for again
    assert "a quiet field at dawn" not in manifest


def test_an_unknown_scene_number_is_rejected_rather_than_ignored(tmp_path, monkeypatch):
    from video_lyrics.config import Project

    audio, words = tmp_path / "song.wav", tmp_path / "song.txt"
    audio.write_bytes(b"RIFF")
    words.write_text("a line", encoding="utf-8")
    project = Project.create(
        tmp_path / "project.yaml", audio=str(audio), lyrics_source=str(words), title="Song"
    )
    project.data["scenes"] = make_scenes()          # scenes 1 and 2
    monkeypatch.setattr(images, "generate", lambda scenes, **kw: scenes)

    with pytest.raises(VideoLyricsError) as error:
        pipeline.stage_images(project, scene=(2, 9))
    assert "9" in str(error.value)


def test_the_scene_flag_is_parsed_as_numbers():
    from video_lyrics import cli

    parse = lambda argv: cli.build_parser().parse_args(argv).scene
    assert parse(["images", "--scene", "19"]) == (19,)
    assert parse(["images", "--scene", "19,23"]) == (19, 23)
    assert parse(["images", "--scene", "19, 23, 19"]) == (19, 23)   # spaces, duplicates
    assert parse(["images"]) is None
    for bad in ("nineteen", "0", "-2", ","):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["images", "--scene", bad])


def test_browser_options_reach_the_site_driver(tmp_path, monkeypatch):
    """The prefix-stripped settings are passed straight through, and anything the
    driver does not take (a stray key in an old project file) is dropped."""
    seen: dict = {}
    monkeypatch.setattr(
        site_module("chatgpt"), "generate", lambda scenes, **kw: seen.update(kw)
    )

    images.generate(
        make_scenes(), images_dir=tmp_path / "images", provider="chatgpt",
        browser={"headless": False, "min_delay": 2.5, "image_selector": None, "nonsense": 1},
    )

    assert seen["images_dir"] == tmp_path / "images"
    assert seen["headless"] is False
    assert seen["min_delay"] == 2.5
    assert "nonsense" not in seen
    assert "image_selector" not in seen  # unset overrides fall back to the site's own


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(VideoLyricsError):
        images.generate(make_scenes(), images_dir=tmp_path, provider="nonsense")


# ------------------------------------------- what the images stage hands over

def test_a_project_still_asking_for_codex_falls_back_to_chatgpt():
    """`codex` was the default provider until chatgpt.com replaced it; projects
    written back then must keep working rather than fail on an unknown name."""
    assert pipeline._image_provider({"provider": "codex"}) == "chatgpt"
    assert pipeline._image_provider({"provider": "meta"}) == "meta"
    assert pipeline._image_provider({}) == "chatgpt"


def test_only_the_active_provider_s_settings_are_handed_to_it():
    settings = {
        "provider": "meta",
        "meta_min_delay": 3.0,
        "meta_profile_dir": "~/elsewhere",
        "chatgpt_min_delay": 9.0,
        "lines_per_image": 2,
    }
    options = pipeline._browser_options(settings, "meta")
    assert options == {"min_delay": 3.0, "profile_dir": "~/elsewhere"}


def test_the_images_stage_wires_the_project_settings_through(tmp_path, monkeypatch):
    from video_lyrics.config import Project

    audio, words = tmp_path / "song.wav", tmp_path / "song.txt"
    audio.write_bytes(b"RIFF")
    words.write_text("a line", encoding="utf-8")
    project = Project.create(
        tmp_path / "project.yaml", audio=str(audio), lyrics_source=str(words), title="Song"
    )
    project.data["scenes"] = make_scenes()
    project.image_generation["chatgpt_max_delay"] = 6.0

    passed: dict = {}
    monkeypatch.setattr(images, "generate", lambda scenes, **kw: passed.update(kw) or scenes)
    pipeline.stage_images(project)

    assert passed["provider"] == "chatgpt"
    assert passed["browser"]["max_delay"] == 6.0
    assert passed["images_dir"] == project.work_dir / "images"
    assert not (project.work_dir / "images.src").exists()
