"""A full pipeline run on a tiny synthetic song, rendered with the ffmpeg engine.

Skipped automatically when ffmpeg is missing.  Marked slow: `pytest -m "not slow"`
skips it.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from video_lyrics import pipeline
from video_lyrics.config import Project

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

DURATION = 12.0
FPS = 24
SIZE = (640, 360)

LINES = [
    "Once I was dead",
    "But grace came down",
    "This line is never sung",
    "And I was raised",
]

SUNG = {
    "Once I was dead": (2.0, 3.6),
    "But grace came down": (4.0, 5.8),
    "And I was raised": (8.0, 9.8),
}


def probe(path: str, entries: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", entries,
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def build_audio(path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=220:duration={DURATION}", "-ac", "2", str(path)],
        check=True,
    )


def build_images(directory, count=3):
    from PIL import Image, ImageDraw

    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        image = Image.new("RGB", (1280, 720), (30 + index * 60, 40, 90 - index * 20))
        draw = ImageDraw.Draw(image)
        draw.ellipse((200 + index * 80, 120, 700 + index * 80, 620), fill=(220, 200 - index * 40, 60))
        image.save(directory / f"still-{index}.png")


def fake_transcript(path, model):
    words = []
    for text, (start, end) in SUNG.items():
        tokens = text.split()
        step = (end - start) / len(tokens)
        for position, token in enumerate(tokens):
            words.append(
                {
                    "word": token,
                    "start": round(start + position * step, 3),
                    "end": round(start + (position + 1) * step, 3),
                    "probability": 1.0,
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": model, "language": "en", "words": words, "segments": []}))


@pytest.mark.slow
def test_full_pipeline_renders_a_video_the_length_of_the_song(tmp_path):
    audio = tmp_path / "song.wav"
    build_audio(audio)
    words = tmp_path / "lyrics.txt"
    words.write_text("[Verse 1]\n" + "\n".join(LINES) + "\n", encoding="utf-8")
    stills = tmp_path / "stills"
    build_images(stills)

    project = Project.create(
        tmp_path / "project.yaml",
        audio=str(audio),
        lyrics_source=str(words),
        title="Test Song",
        author="Jose Troche",
        work_dir=str(tmp_path / "work"),
        output=str(tmp_path / "out" / "test-song.mp4"),
    )
    project.video.update(
        {"width": SIZE[0], "height": SIZE[1], "fps": FPS, "font_size": 28,
         "margin_v": 30, "transition": 0.5, "title_duration": 1.5}
    )
    project.alignment.update({"model": "stub"})
    project.image_generation.update({"provider": "supplied", "source_dir": str(stills)})
    project.render_settings.update({"engine": "ffmpeg", "intermediate": "h264"})
    project.save()

    fake_transcript(project.transcript_path, "stub")

    pipeline.run(project, first="lyrics", last="render")

    # the unsung line got no cue; the other three did
    texts = [cue["text"] for cue in project.cues]
    assert texts == ["Once I was dead", "But grace came down", "And I was raised"]

    # scenes tile the song, every one has an image
    assert project.scenes[0]["start"] == 0.0
    assert project.scenes[-1]["end"] == pytest.approx(project.duration, abs=0.05)
    assert all(scene.get("image") for scene in project.scenes)

    # the title card is gone before the first lyric appears
    title = project.data["overlays"]["title"]
    assert title["end"] <= project.cues[0]["start"]

    # the bed is frame-exact
    total_frames = sum(clip["frames"] for clip in project.data["bed"])
    assert total_frames == round(project.duration * FPS)
    assert any(clip["kind"] == "transition" for clip in project.data["bed"])

    output = project.output
    assert output.is_file()
    assert probe(str(output), "stream=width,height").split("\n") == [str(SIZE[0]), str(SIZE[1])]
    rendered = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(output)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    assert rendered == pytest.approx(DURATION, abs=0.15)

    # the SRT matches the cues
    srt = project.srt_path.read_text(encoding="utf-8")
    assert "Once I was dead" in srt
    assert "never sung" not in srt

    # between the title card and the first lyric the overlay track must be fully
    # transparent - an opaque filler would black out the picture
    gap = (title["end"] + project.cues[0]["start"]) / 2
    assert mean_luma(str(output), gap) > 5.0


def mean_luma(path: str, at: float) -> float:
    """Average brightness of the frame at `at` seconds, 0-255."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", path, "-frames:v", "1",
         "-vf", "format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )
    pixels = out.stdout
    assert pixels, "no frame decoded"
    return sum(pixels) / len(pixels)
