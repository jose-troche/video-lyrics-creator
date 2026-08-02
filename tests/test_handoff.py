"""The free-edition route: prepare everything, then let Resolve run the last step.

Covers the launcher install, the staged job, and `build_and_render` - the function
the in-Resolve launcher calls - against the fake scripting API.
"""

from __future__ import annotations

import json
import sys

import pytest

from video_lyrics import handoff, render_resolve
from video_lyrics.config import Project
from video_lyrics.util import VideoLyricsError

from .fakes import FakeResolve


# ---------------------------------------------------------------- launcher


def test_the_installed_launcher_is_valid_python_with_the_path_filled_in(tmp_path):
    target = handoff.install(tmp_path)
    source = target.read_text(encoding="utf-8")

    assert target.name == "Video Lyrics Creator.py"
    assert handoff.PACKAGE_ROOT_MARKER not in source
    assert str(handoff.package_root()) in source
    compile(source, str(target), "exec")  # syntax-checks exactly what Resolve runs


def test_reinstalling_keeps_a_copy_of_a_different_launcher(tmp_path):
    existing = tmp_path / handoff.LAUNCHER_NAME
    existing.write_text("# someone else's script\n", encoding="utf-8")

    handoff.install(tmp_path)

    backup = tmp_path / "Video Lyrics Creator.py.previous"
    assert backup.read_text(encoding="utf-8") == "# someone else's script\n"


def test_installing_twice_does_not_pile_up_backups(tmp_path):
    handoff.install(tmp_path)
    handoff.install(tmp_path)
    assert not (tmp_path / "Video Lyrics Creator.py.previous").exists()


def test_install_and_uninstall_are_reported(tmp_path):
    assert handoff.is_installed(tmp_path) is False
    handoff.install(tmp_path)
    assert handoff.is_installed(tmp_path) is True
    assert handoff.uninstall(tmp_path) is True
    assert handoff.uninstall(tmp_path) is False


# -------------------------------------------------------------- staged job


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """A project whose prepared media exists on disk."""
    monkeypatch.setattr(handoff, "job_path", lambda: tmp_path / "job.json")

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF")
    words = tmp_path / "song.txt"
    words.write_text("a line")
    project = Project.create(
        tmp_path / "project.json", audio=str(audio), lyrics_source=str(words), title="Test Song"
    )
    project.data["duration"] = 30.0

    media = tmp_path / "media"
    media.mkdir()
    for name in ("bed-001.mp4", "lyric-001.mov", "title.mov"):
        (media / name).write_bytes(b"\0")
    project.data["bed"] = [
        {"kind": "scene", "path": str(media / "bed-001.mp4"), "first_frame": 0, "frames": 900}
    ]
    project.data["overlays"] = {
        "title": {"start": 0.0, "end": 8.0, "clip": str(media / "title.mov")},
        "lyrics": [{"start": 10.0, "end": 13.0, "clip": str(media / "lyric-001.mov")}],
        "srt": str(tmp_path / "lyrics.srt"),
    }
    project.save()
    return project


def test_staging_records_the_project_and_the_checkout(prepared):
    path = handoff.stage(prepared)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["project"] == str(prepared.path.resolve())
    assert payload["package_root"] == str(handoff.package_root())
    assert payload["title"] == "Test Song"
    assert handoff.load()["project"] == payload["project"]


def test_loading_without_a_staged_job_explains_what_to_run(tmp_path, monkeypatch):
    monkeypatch.setattr(handoff, "job_path", lambda: tmp_path / "nothing.json")
    with pytest.raises(VideoLyricsError, match="render"):
        handoff.load()


def test_the_instructions_name_the_menu_item_and_the_output(prepared):
    text = handoff.instructions(prepared)
    assert "Workspace > Scripts > Video Lyrics Creator" in text
    assert str(prepared.output) in text


# --------------------------------------------- what the launcher then runs


def test_build_and_render_assembles_and_renders(prepared):
    output = prepared.output

    def write_the_file(_settings):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")

    fake = FakeResolve(on_render=write_the_file)
    messages: list[str] = []
    result = render_resolve.build_and_render(prepared, resolve=fake, progress=messages.append)

    placed = fake.manager.project.GetMediaPool().appended
    assert len(placed) == 4  # bed clip, lyric, title, audio
    assert fake.manager.project.render["settings"]["CustomName"] == output.stem
    assert result == str(output)
    assert prepared.data["render"]["last_output"] == str(output)
    assert any("rendering" in message for message in messages)


def test_the_launcher_runs_the_staged_job_the_way_resolve_would(prepared, tmp_path, monkeypatch):
    """Exec the installed launcher with a `resolve` object in globals, as Resolve does."""
    monkeypatch.setenv(handoff.JOB_ENV, str(tmp_path / "job.json"))
    handoff.stage(prepared)
    launcher = handoff.install(tmp_path / "scripts")

    output = prepared.output
    fake = FakeResolve(
        on_render=lambda _settings: (
            output.parent.mkdir(parents=True, exist_ok=True), output.write_bytes(b"video")
        )
    )
    scope = {"resolve": fake, "__name__": "__main__"}
    exec(compile(launcher.read_text(encoding="utf-8"), str(launcher), "exec"), scope)

    timeline = fake.manager.project.GetMediaPool().timelines[0]
    placed = fake.manager.project.GetMediaPool().appended
    assert timeline.names[("video", 1)] == "Images"
    assert len(placed) == 4
    assert (prepared.work_dir / "resolve-launcher.log").is_file()


def test_the_launcher_needs_no_yaml_parser(prepared, tmp_path, monkeypatch):
    """Resolve runs its own Python, which has no PyYAML - the job carries the data."""
    monkeypatch.setenv(handoff.JOB_ENV, str(tmp_path / "job.json"))
    yaml_project = prepared.save(prepared.path.with_suffix(".yaml"))
    prepared.path = yaml_project
    handoff.stage(prepared)
    launcher = handoff.install(tmp_path / "scripts")

    monkeypatch.setitem(sys.modules, "yaml", None)  # any `import yaml` now fails

    output = prepared.output
    fake = FakeResolve(
        on_render=lambda _settings: (
            output.parent.mkdir(parents=True, exist_ok=True), output.write_bytes(b"video")
        )
    )
    scope = {"resolve": fake, "__name__": "__main__"}
    exec(compile(launcher.read_text(encoding="utf-8"), str(launcher), "exec"), scope)

    # the timeline was built, and the render is not reported as a failure just
    # because the project file could not be updated afterwards
    assert len(fake.manager.project.GetMediaPool().appended) == 4
    log = (prepared.work_dir / "resolve-launcher.log").read_text(encoding="utf-8")
    assert "could not update project.yaml" in log
    assert "done - rendered" in log


def test_the_launcher_says_what_to_do_when_nothing_is_staged(tmp_path, monkeypatch):
    monkeypatch.setenv(handoff.JOB_ENV, str(tmp_path / "absent.json"))
    launcher = handoff.install(tmp_path / "scripts")
    scope = {"resolve": FakeResolve(), "__name__": "__main__"}
    with pytest.raises(RuntimeError, match="video-lyrics render"):
        exec(compile(launcher.read_text(encoding="utf-8"), str(launcher), "exec"), scope)


def test_build_and_render_refuses_when_prepared_media_is_missing(prepared):
    prepared.data["bed"][0]["path"] = str(prepared.work_dir / "gone.mp4")
    with pytest.raises(VideoLyricsError, match="Prepared media is missing"):
        render_resolve.build_and_render(prepared, resolve=FakeResolve())


def test_build_and_render_needs_a_bed(prepared):
    prepared.data["bed"] = []
    with pytest.raises(VideoLyricsError, match="image bed"):
        render_resolve.build_and_render(prepared, resolve=FakeResolve())
