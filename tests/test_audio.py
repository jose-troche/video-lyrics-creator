"""The audio fade baked in before either render engine sees the song.

`audio.bake_fades` produces the file that actually gets rendered/imported: same
length as the source, exactly, with a short fade-in and fade-out.
"""

from __future__ import annotations

import pytest

from video_lyrics import audio
from video_lyrics.util import VideoLyricsError, run, which


def make_tone(path, seconds=6.0, rate=8000):
    run([
        which("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
        str(path),
    ])


@pytest.mark.slow
def test_the_faded_copy_is_exactly_as_long_as_the_source(tmp_path):
    source = tmp_path / "song.wav"
    make_tone(source, seconds=6.0)

    output = audio.bake_fades(source, tmp_path / "faded.wav", duration=6.0, fade=1.0)

    assert output.is_file()
    assert audio.duration(output) == pytest.approx(6.0, abs=0.02)


@pytest.mark.slow
def test_the_ends_fade_but_the_middle_does_not(tmp_path):
    source = tmp_path / "song.wav"
    make_tone(source, seconds=6.0)

    output = audio.bake_fades(source, tmp_path / "faded.wav", duration=6.0, fade=1.0)
    peaks = audio.envelope(output, resolution=20)

    # A full-volume 440Hz tone: near-silent at the very start and end, full
    # strength by the time the fade has finished (a second in and a second from
    # the end, i.e. after/before 20 buckets at this resolution).
    assert peaks[0] < 0.1
    assert peaks[-1] < 0.1
    assert min(peaks[25:-25]) > 0.9


@pytest.mark.slow
def test_a_fade_longer_than_half_the_song_is_clamped(tmp_path):
    """A fade cannot eat the whole clip - it is capped at half the duration."""
    source = tmp_path / "song.wav"
    make_tone(source, seconds=2.0)

    output = audio.bake_fades(source, tmp_path / "faded.wav", duration=2.0, fade=5.0)
    assert audio.duration(output) == pytest.approx(2.0, abs=0.02)


@pytest.mark.slow
def test_an_existing_output_is_reused_unless_forced(tmp_path):
    source = tmp_path / "song.wav"
    make_tone(source, seconds=3.0)
    output_path = tmp_path / "faded.wav"

    first = audio.bake_fades(source, output_path, duration=3.0, fade=1.0)
    source.unlink()  # the cached copy must not need the source again

    second = audio.bake_fades(source, output_path, duration=3.0, fade=1.0)
    assert second == first
    assert second.is_file()


def test_a_missing_source_is_an_error_when_nothing_is_cached(tmp_path):
    with pytest.raises(VideoLyricsError, match="not found"):
        audio.bake_fades(tmp_path / "missing.wav", tmp_path / "faded.wav", duration=3.0)


# ------------------------------------------------------------------ the pipeline


def test_render_bakes_the_fade_and_renders_the_faded_copy(tmp_path, monkeypatch):
    """stage_render must hand the *faded* file to the engine, not the raw source."""
    from video_lyrics import pipeline
    from video_lyrics.config import Project

    src = tmp_path / "song.wav"
    src.write_bytes(b"RIFF")
    words = tmp_path / "song.txt"
    words.write_text("a line", encoding="utf-8")
    project = Project.create(
        tmp_path / "project.json", audio=str(src), lyrics_source=str(words), title="Test Song",
    )
    project.data["duration"] = 12.0
    project.data["bed"] = [{"kind": "scene", "path": str(tmp_path / "bed.mp4")}]
    project.render_settings["engine"] = "ffmpeg"

    baked = []
    monkeypatch.setattr(
        pipeline.audio_mod, "bake_fades",
        lambda source, output, **kw: baked.append((source, output, kw)) or output,
    )
    rendered = {}
    monkeypatch.setattr(
        pipeline.render_ffmpeg, "render",
        lambda **kw: rendered.update(kw) or kw["output"],
    )

    pipeline.stage_render(project)

    assert baked == [(project.audio, project.faded_audio_path, {
        "duration": 12.0, "fade": 1.0, "force": False,
    })]
    assert rendered["audio"] == project.faded_audio_path


def test_a_configured_audio_fade_length_is_used(tmp_path, monkeypatch):
    from video_lyrics import pipeline
    from video_lyrics.config import Project

    src = tmp_path / "song.wav"
    src.write_bytes(b"RIFF")
    words = tmp_path / "song.txt"
    words.write_text("a line", encoding="utf-8")
    project = Project.create(
        tmp_path / "project.json", audio=str(src), lyrics_source=str(words), title="Test Song",
    )
    project.data["duration"] = 12.0
    project.data["bed"] = [{"kind": "scene", "path": str(tmp_path / "bed.mp4")}]
    project.render_settings["engine"] = "ffmpeg"
    project.video["audio_fade"] = 2.5

    fades = []
    monkeypatch.setattr(
        pipeline.audio_mod, "bake_fades",
        lambda source, output, **kw: fades.append(kw["fade"]) or output,
    )
    monkeypatch.setattr(pipeline.render_ffmpeg, "render", lambda **kw: kw["output"])

    pipeline.stage_render(project)
    assert fades == [2.5]
