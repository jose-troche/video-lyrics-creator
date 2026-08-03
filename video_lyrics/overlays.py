"""Build the lyric/title assets: transparent PNGs, alpha movie clips, and an SRT."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .text import draw_block, load_font, wrap
from .util import ensure_dir, format_timecode, log, run, short_hash, which

# QuickTime Animation keeps a straight alpha channel and stays tiny for text on
# transparency; DaVinci Resolve and ffmpeg both read it without extra setup.
ALPHA_CODEC_ARGS = ["-c:v", "qtrle", "-pix_fmt", "argb"]


def cue_display_times(
    cue: dict[str, Any], *, lead: float, previous_end: float | None
) -> tuple[float, float]:
    """Apply the lead-in without letting a cue collide with the one before it."""
    start = max(0.0, cue["start"] - lead)
    if previous_end is not None:
        start = max(start, previous_end)
    return start, max(start, cue["end"])


def render_lyrics(
    cues: list[dict[str, Any]],
    *,
    directory: Path,
    size: tuple[int, int],
    font_name: str,
    font_size: int,
    margin_v: int,
    lead: float = 0.0,
    force: bool = False,
) -> list[dict[str, Any]]:
    """One transparent PNG per lyric cue. Returns [{start, end, text, image}]."""
    directory = ensure_dir(directory)
    width, height = size
    margin_h = int(round(width * 0.08))
    max_width = width - 2 * margin_h

    results: list[dict[str, Any]] = []
    previous_end: float | None = None
    for index, cue in enumerate(cues, start=1):
        start, end = cue_display_times(cue, lead=lead, previous_end=previous_end)
        previous_end = end
        path = directory / f"lyric-{index:03d}-{short_hash(cue['text'], font_name, font_size, size)}.png"
        if force or not path.is_file():
            _draw_lyric(cue["text"], path, size, font_name, font_size, margin_v, max_width)
        results.append(
            {"start": round(start, 3), "end": round(end, 3), "text": cue["text"], "image": str(path)}
        )
    log.info("Lyric overlays ready: %d", len(results))
    return results


def _draw_lyric(
    text: str,
    path: Path,
    size: tuple[int, int],
    font_name: str,
    font_size: int,
    margin_v: int,
    max_width: int,
) -> None:
    width, height = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    point = font_size
    font = load_font(font_name, point)
    lines = wrap(text, font, max_width)
    while len(lines) > 2 and point > font_size * 0.6:
        point = int(point * 0.9)
        font = load_font(font_name, point)
        lines = wrap(text, font, max_width)
    ascent, descent = font.getmetrics()
    line_height = int(round((ascent + descent) * 1.25))
    block_height = line_height * len(lines)
    center_y = height - margin_v - block_height // 2
    draw_block(canvas, lines, font, center_y=center_y)
    canvas.save(path, "PNG")


def render_title(
    *,
    title: str,
    author: str,
    directory: Path,
    size: tuple[int, int],
    font_name: str,
    font_size: int,
    force: bool = False,
) -> Path:
    """The opening title card: song title with the author underneath."""
    directory = ensure_dir(directory)
    path = directory / f"title-{short_hash(title, author, font_name, font_size, size)}.png"
    if path.is_file() and not force:
        return path

    width, height = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    max_width = width - int(round(width * 0.12)) * 2

    title_font = load_font(font_name, int(font_size * 2.0))
    title_lines = wrap(title, title_font, max_width)
    while len(title_lines) > 2 and title_font.size > font_size:
        title_font = load_font(font_name, int(title_font.size * 0.9))
        title_lines = wrap(title, title_font, max_width)

    author_font = load_font(font_name, int(font_size * 0.85))
    ascent, descent = title_font.getmetrics()
    title_line_height = int(round((ascent + descent) * 1.2))
    title_block = title_line_height * len(title_lines)

    center_y = int(height * 0.46)
    draw_block(canvas, title_lines, title_font, center_y=center_y, line_spacing=1.2)
    draw_block(
        canvas,
        [author],
        author_font,
        center_y=center_y + title_block // 2 + int(font_size * 1.1),
        fill=(240, 240, 240, 235),
        line_spacing=1.0,
        stroke_width=1,
    )
    canvas.save(path, "PNG")
    return path


def title_window(
    cues: list[dict[str, Any]], *, duration: float, requested: float, fade: float, lead: float
) -> tuple[float, float]:
    """Title starts with the song and must be gone before the first lyric appears."""
    first_lyric = (cues[0]["start"] - lead) if cues else duration
    latest_end = max(0.0, first_lyric - fade * 0.75)
    end = min(requested, latest_end) if latest_end > 0 else 0.0
    return 0.0, round(end, 3)


def write_srt(cues: list[dict[str, Any]], path: Path, *, lead: float = 0.0) -> Path:
    """A standard SRT, ready for a DaVinci Resolve subtitle track or any player."""
    blocks: list[str] = []
    previous_end: float | None = None
    for index, cue in enumerate(cues, start=1):
        start, end = cue_display_times(cue, lead=lead, previous_end=previous_end)
        previous_end = end
        blocks.append(
            f"{index}\n{format_timecode(start)} --> {format_timecode(end)}\n{cue['text']}\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks), encoding="utf-8")
    log.info("Wrote %s (%d cues)", path, len(cues))
    return path


def bake_items(
    items: list[dict[str, Any]],
    *,
    directory: Path,
    fps: float,
    fade: float,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Bake every overlay PNG into an alpha movie clip with its fades.

    Both render engines call this, so the clips are generated once and shared.
    """
    directory = ensure_dir(directory)
    for item in items:
        length = float(item["end"]) - float(item["start"])
        if length <= 0:
            continue
        fade_length = min(float(item.get("fade", fade)), length / 2)
        png = Path(item["image"])
        out = directory / f"{png.stem}-{short_hash(round(length, 3), fade_length, fps)}.mov"
        bake_alpha_clip(
            png, out,
            duration=length, fps=fps,
            fade_in=fade_length, fade_out=fade_length,
            force=force,
        )
        item["clip"] = str(out)
    return items


def bake_alpha_clip(
    png: Path,
    out: Path,
    *,
    duration: float,
    fps: float,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    force: bool = False,
) -> Path:
    """Turn a transparent PNG into a movie clip with the fades already baked in.

    Resolve's scripting API cannot keyframe opacity, so the fade has to live in the
    media itself.
    """
    if out.is_file() and not force:
        return out
    duration = max(duration, 2.0 / fps)
    filters = ["format=rgba"]
    if fade_in > 0:
        filters.append(f"fade=t=in:st=0:d={fade_in:.3f}:alpha=1")
    if fade_out > 0:
        filters.append(f"fade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}:alpha=1")
    out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            which("ffmpeg"), "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}", "-i", str(png),
            "-vf", ",".join(filters),
            "-r", str(fps),
            *ALPHA_CODEC_ARGS,
            str(out),
        ]
    )
    return out
