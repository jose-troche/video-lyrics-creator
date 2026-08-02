"""The project file is split so that starting a new song never overwrites the last.

  * ``project.yaml`` (the pointer) - just enough to find the rest: schema
    version, title, work directory.
  * ``<work>/<slug>/project.yaml`` (the data) - every setting, every stage's
    results, and every working file that stage produces.

Pre-split ("legacy") single-file projects are migrated in place on load: their
flat work directory is moved into its own ``<slug>/`` subfolder.
"""

from __future__ import annotations

import json

import pytest
import yaml

from video_lyrics.config import Project
from video_lyrics.util import VideoLyricsError


@pytest.fixture
def song(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF")
    words = tmp_path / "song.txt"
    words.write_text("a line", encoding="utf-8")
    return audio, words


def make(tmp_path, song, title="Immeasurable Grace", **kwargs):
    audio, words = song
    return Project.create(
        tmp_path / "project.yaml",
        audio=str(audio), lyrics_source=str(words), title=title, **kwargs
    )


# --------------------------------------------------------------- the split


def test_saving_writes_a_minimal_pointer_and_a_full_data_file(tmp_path, song):
    project = make(tmp_path, song)
    project.data["duration"] = 12.5
    project.save()

    pointer = yaml.safe_load(project.path.read_text())
    assert set(pointer) == {"schema_version", "title", "work_dir"}
    assert pointer["title"] == "Immeasurable Grace"

    assert project.data_path == tmp_path / "work" / "immeasurable-grace" / "project.yaml"
    assert project.data_path.is_file()
    reloaded = Project.load(project.path)
    assert reloaded.data["duration"] == 12.5
    assert reloaded.data == project.data


def test_every_work_path_lives_under_the_songs_own_folder(tmp_path, song):
    project = make(tmp_path, song)
    song_dir = tmp_path / "work" / "immeasurable-grace"

    assert project.work_dir == song_dir
    assert project.images_dir == song_dir / "images"
    assert project.overlays_dir == song_dir / "overlays"
    assert project.clips_dir == song_dir / "clips"
    assert project.transcript_path == song_dir / "transcript.json"
    assert project.lyrics_text_path == song_dir / "lyrics.txt"
    assert project.srt_path == song_dir / "lyrics.srt"


def test_a_second_song_gets_its_own_folder_and_does_not_touch_the_first(tmp_path, song):
    first = make(tmp_path, song, title="First Song")
    first.data["duration"] = 100.0
    first.save()
    (first.work_dir / "transcript.json").write_text("first song's transcript")

    second = make(tmp_path, song, title="Second Song")
    second.data["duration"] = 200.0
    second.save()
    (second.work_dir / "transcript.json").write_text("second song's transcript")

    # the pointer now points at the second song ...
    pointer_now = Project.load(tmp_path / "project.yaml")
    assert pointer_now.title == "Second Song"
    assert pointer_now.data["duration"] == 200.0

    # ... but the first song's folder and files are untouched
    assert (tmp_path / "work" / "first-song" / "transcript.json").read_text() == \
        "first song's transcript"
    assert (tmp_path / "work" / "second-song" / "transcript.json").read_text() == \
        "second song's transcript"


def test_pointing_the_title_back_at_an_old_song_recovers_its_data(tmp_path, song):
    """Editing the pointer's title switches the 'current' song without losing work."""
    first = make(tmp_path, song, title="First Song")
    first.data["duration"] = 100.0
    first.save()
    make(tmp_path, song, title="Second Song").save()

    pointer_path = tmp_path / "project.yaml"
    pointer = pointer_path.read_text().replace("Second Song", "First Song")
    pointer_path.write_text(pointer)

    recovered = Project.load(pointer_path)
    assert recovered.data["duration"] == 100.0


def test_saving_to_an_explicit_alternate_path_writes_one_merged_file(tmp_path, song):
    """`convert`/backups get a single self-contained snapshot, not another split."""
    project = make(tmp_path, song)
    project.data["duration"] = 42.0
    snapshot = tmp_path / "backup.json"

    project.save(snapshot)

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["audio"] == str(project.audio)
    assert payload["duration"] == 42.0
    assert payload["title"] == "Immeasurable Grace"


# ------------------------------------------------------------- migration


def _write_legacy(tmp_path, *, title="Immeasurable Grace"):
    """A pre-split project: one monolithic file, a flat (un-slugged) work dir."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "transcript.json").write_text('{"words": []}', encoding="utf-8")
    (work / "lyrics.txt").write_text("Once I was dead\n", encoding="utf-8")
    (work / "images").mkdir()
    (work / "images" / "scene-001.png").write_bytes(b"\x89PNG")

    legacy = {
        "schema_version": 1,
        "title": title,
        "author": "Jose Troche",
        "audio": str(tmp_path / "song.wav"),
        "lyrics_source": str(tmp_path / "song.txt"),
        "visual_style": "cinematic photographic realism",
        "work_dir": str(work),
        "duration": 282.9,
    }
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(legacy), encoding="utf-8")
    return path, work


def test_loading_a_legacy_project_migrates_its_flat_work_dir(tmp_path):
    path, work = _write_legacy(tmp_path)

    project = Project.load(path)

    song_dir = work / "immeasurable-grace"
    assert project.data["duration"] == 282.9
    assert not (work / "transcript.json").exists()          # moved, not copied
    assert (song_dir / "transcript.json").read_text() == '{"words": []}'
    assert (song_dir / "lyrics.txt").read_text() == "Once I was dead\n"
    assert (song_dir / "images" / "scene-001.png").is_file()


def test_a_migrated_project_leaves_a_minimal_pointer_behind(tmp_path):
    path, work = _write_legacy(tmp_path)
    Project.load(path)

    pointer = yaml.safe_load(path.read_text())
    assert set(pointer) == {"schema_version", "title", "work_dir"}

    data_path = work / "immeasurable-grace" / "project.yaml"
    data = yaml.safe_load(data_path.read_text())
    assert data["duration"] == 282.9
    assert data["audio"] == str(tmp_path / "song.wav")


def test_migration_rewrites_paths_recorded_before_the_move(tmp_path):
    """The files move; any path a stage already recorded must move with them.

    Scene images, overlay clips, the bed, and the transcript are all recorded as
    absolute paths *before* migration ever runs - if those strings are not
    rewritten too, they go stale the moment the files are moved, and the next
    stage that reads them fails with "no such file".
    """
    path, work = _write_legacy(tmp_path)
    legacy = yaml.safe_load(path.read_text())
    legacy.update(
        {
            "scenes": [{"index": 1, "image": str(work / "images" / "scene-001.png")}],
            "overlays": {
                "title": {"image": str(work / "overlays" / "title.png"),
                          "clip": str(work / "overlay-clips" / "title.mov")},
                "lyrics": [{"image": str(work / "overlays" / "lyric-001.png"),
                            "clip": str(work / "overlay-clips" / "lyric-001.mov")}],
                "srt": str(work / "lyrics.srt"),
            },
            "bed": [{"path": str(work / "clips" / "bed-001-scene.mp4")}],
            "transcript": {"path": str(work / "transcript.json")},
        }
    )
    path.write_text(yaml.safe_dump(legacy), encoding="utf-8")

    project = Project.load(path)

    song_dir = work / "immeasurable-grace"
    assert project.scenes[0]["image"] == str(song_dir / "images" / "scene-001.png")
    assert project.data["overlays"]["title"]["clip"] == str(song_dir / "overlay-clips" / "title.mov")
    assert project.data["overlays"]["lyrics"][0]["image"] == str(song_dir / "overlays" / "lyric-001.png")
    assert project.data["overlays"]["srt"] == str(song_dir / "lyrics.srt")
    assert project.data["bed"][0]["path"] == str(song_dir / "clips" / "bed-001-scene.mp4")
    assert project.data["transcript"]["path"] == str(song_dir / "transcript.json")
    # and work_dir itself must stay the *base*, not be relocated into the song folder
    assert project.data["work_dir"] == str(work)


def test_migration_is_idempotent(tmp_path):
    path, _work = _write_legacy(tmp_path)
    Project.load(path)
    reloaded = Project.load(path)  # already migrated; loading again must not explode
    assert reloaded.data["duration"] == 282.9


def test_migration_does_not_disturb_an_already_migrated_sibling_song(tmp_path):
    path, work = _write_legacy(tmp_path, title="First Song")
    Project.load(path)
    first_song_dir = work / "first-song"
    assert first_song_dir.is_dir()

    # a second, still-legacy project sharing the same base work dir
    second_legacy = {
        "schema_version": 1, "title": "Second Song", "audio": str(tmp_path / "song.wav"),
        "lyrics_source": str(tmp_path / "song.txt"), "work_dir": str(work),
    }
    second_path = tmp_path / "second.yaml"
    second_path.write_text(yaml.safe_dump(second_legacy), encoding="utf-8")

    Project.load(second_path)

    # the first song's folder is untouched by the second song's migration
    assert (first_song_dir / "transcript.json").is_file()
    assert (work / "second-song").is_dir()


def test_opening_the_inner_data_file_directly_does_not_re_split_it(tmp_path):
    """A power-user edge case: -p pointed straight at work/<slug>/project.yaml."""
    path, work = _write_legacy(tmp_path)
    Project.load(path)  # migrate once, normally

    data_path = work / "immeasurable-grace" / "project.yaml"
    project = Project.load(data_path)
    assert project.data["duration"] == 282.9
    assert project.path == data_path


def test_a_pointer_with_no_title_is_rejected(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(VideoLyricsError, match="title"):
        Project.load(path)


def test_a_pointer_to_a_missing_data_file_is_reported(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text(
        f"schema_version: 1\ntitle: Ghost Song\nwork_dir: {tmp_path / 'work'}\n",
        encoding="utf-8",
    )
    with pytest.raises(VideoLyricsError, match="does not exist"):
        Project.load(path)
