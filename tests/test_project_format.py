"""YAML is the default project format; JSON keeps working."""

from __future__ import annotations

import json

import pytest

from video_lyrics import cli
from video_lyrics.config import DEFAULT_PROJECT_NAME, Project, find_project
from video_lyrics.util import VideoLyricsError


@pytest.fixture
def song(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF")
    words = tmp_path / "song.txt"
    words.write_text("a line", encoding="utf-8")
    return audio, words


def make(tmp_path, song, name="project.yaml"):
    audio, words = song
    project = Project.create(
        tmp_path / name, audio=str(audio), lyrics_source=str(words), title="Immeasurable grace"
    )
    project.data["duration"] = 282.92
    project.data["lyrics"] = [
        {"start": 20.5, "end": 25.78, "text": "I walked the world’s dark crooked ways",
         "line_index": 3, "alignment_confidence": 0.62}
    ]
    return project


# ------------------------------------------------------------------ formats


def test_a_yaml_project_round_trips_exactly(tmp_path, song):
    project = make(tmp_path, song)
    project.save()

    body = project.path.read_text(encoding="utf-8")
    assert body.startswith("schema_version:")          # readable, key order preserved
    assert "world’s" in body                           # unicode not escaped
    assert Project.load(project.path).data == project.data


def test_a_json_project_still_works(tmp_path, song):
    project = make(tmp_path, song, name="project.json")
    project.save()

    assert json.loads(project.path.read_text(encoding="utf-8"))["title"] == "Immeasurable grace"
    assert Project.load(project.path).data == project.data


def test_yaml_and_json_carry_the_same_data(tmp_path, song):
    project = make(tmp_path, song)
    as_yaml = project.save(tmp_path / "a.yaml")
    as_json = project.save(tmp_path / "a.json")
    assert Project.load(as_yaml).data == Project.load(as_json).data


def test_a_yml_suffix_is_yaml_too(tmp_path, song):
    project = make(tmp_path, song, name="project.yml")
    project.save()
    assert "schema_version:" in project.path.read_text(encoding="utf-8")
    assert Project.load(project.path).title == "Immeasurable grace"


def test_a_broken_project_file_is_reported(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(VideoLyricsError, match="mapping"):
        Project.load(path)


# -------------------------------------------------------------- discovery


def test_yaml_is_preferred_but_json_is_found(tmp_path):
    assert find_project(base=tmp_path).name == DEFAULT_PROJECT_NAME  # nothing there yet

    (tmp_path / "project.json").write_text("{}", encoding="utf-8")
    assert find_project(base=tmp_path).name == "project.json"

    (tmp_path / "project.yaml").write_text("{}", encoding="utf-8")
    assert find_project(base=tmp_path).name == "project.yaml"


def test_an_explicit_path_always_wins(tmp_path):
    (tmp_path / "project.yaml").write_text("{}", encoding="utf-8")
    assert find_project(tmp_path / "other.json", base=tmp_path) == tmp_path / "other.json"


# ------------------------------------------------------------------- CLI


def test_init_writes_yaml_by_default(tmp_path, song, monkeypatch):
    audio, words = song
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--audio", str(audio), "--lyrics", str(words)]) == 0

    project = tmp_path / "project.yaml"
    assert project.is_file()
    assert not (tmp_path / "project.json").exists()
    assert Project.load(project).title == "song"


def test_init_can_still_write_json(tmp_path, song, monkeypatch):
    audio, words = song
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--format", "json", "--audio", str(audio), "--lyrics", str(words)]) == 0
    assert (tmp_path / "project.json").is_file()


def test_commands_find_the_project_without_being_told(tmp_path, song, monkeypatch, capsys):
    audio, words = song
    monkeypatch.chdir(tmp_path)
    cli.main(["init", "--audio", str(audio), "--lyrics", str(words), "--title", "Found Me"])
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    assert "Found Me" in capsys.readouterr().out


def test_convert_writes_the_other_format_and_keeps_the_original(tmp_path, song, monkeypatch, capsys):
    audio, words = song
    monkeypatch.chdir(tmp_path)
    cli.main(["init", "--format", "json", "--audio", str(audio), "--lyrics", str(words)])
    capsys.readouterr()

    assert cli.main(["convert", "--to", "yaml"]) == 0
    output = capsys.readouterr().out
    assert "project.yaml" in output
    assert (tmp_path / "project.json").is_file()          # not deleted
    assert Project.load(tmp_path / "project.yaml").data == Project.load(tmp_path / "project.json").data


def test_set_keeps_the_file_in_its_own_format(tmp_path, song, monkeypatch, capsys):
    audio, words = song
    monkeypatch.chdir(tmp_path)
    cli.main(["init", "--audio", str(audio), "--lyrics", str(words)])
    capsys.readouterr()

    assert cli.main(["set", "video.zoom", "1.15"]) == 0
    body = (tmp_path / "project.yaml").read_text(encoding="utf-8")
    assert "zoom: 1.15" in body
