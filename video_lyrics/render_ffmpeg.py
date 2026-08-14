"""Render the finished video with ffmpeg.

This is the default render engine. It consumes exactly the same prepared assets
as the Resolve engine (the optional alternative), so both produce the same edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .motion import concat_clips
from .overlays import ALPHA_CODEC_ARGS, bake_items
from .util import ensure_dir, log, run, short_hash, which


def build_overlay_track(
    items: list[dict[str, Any]],
    *,
    directory: Path,
    out: Path,
    size: tuple[int, int],
    fps: float,
    duration: float,
    fade: float,
    force: bool = False,
) -> Path:
    """One transparent movie holding the title and every lyric line, with fades.

    `items` are {start, end, image} in time order and must not overlap.
    """
    directory = ensure_dir(directory)
    width, height = size
    ffmpeg = which("ffmpeg")
    pieces: list[Path] = []
    cursor = 0.0

    def filler(length: float) -> Path:
        path = directory / f"gap-{short_hash(round(length, 3), size, fps)}.mov"
        if not path.is_file():
            # format=rgba has to sit inside the lavfi graph: the colour source only
            # emits an alpha channel when the filter downstream of it asks for one.
            run(
                [
                    ffmpeg, "-y", "-loglevel", "error",
                    "-f", "lavfi",
                    "-i", (
                        f"color=c=black@0.0:s={width}x{height}:r={fps:g}:"
                        f"d={length:.3f},format=rgba"
                    ),
                    *ALPHA_CODEC_ARGS,
                    str(path),
                ]
            )
        return path

    usable: list[dict[str, Any]] = []
    for item in items:
        start = max(0.0, float(item["start"]))
        end = min(duration, float(item["end"]))
        if end > start:
            usable.append({**item, "start": start, "end": end})
    bake_items(usable, directory=directory, fps=fps, fade=fade, force=force)

    for item in usable:
        if item["start"] > cursor + 1.0 / fps:
            pieces.append(filler(item["start"] - cursor))
        pieces.append(Path(item["clip"]))
        cursor = item["end"]
    if duration - cursor > 1.0 / fps:
        pieces.append(filler(duration - cursor))

    listing = out.with_suffix(".txt")
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in pieces), encoding="utf-8"
    )
    run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(out),
        ],
        timeout=1800,
    )
    log.info("Overlay track ready: %s (%d pieces)", out.name, len(pieces))
    return out


def render(
    *,
    clips: list[dict[str, Any]],
    overlay_items: list[dict[str, Any]],
    audio: Path,
    output: Path,
    work_dir: Path,
    size: tuple[int, int],
    fps: float,
    duration: float,
    fade: float,
    video_bitrate: str | None = None,
    force: bool = False,
) -> Path:
    """Bed + overlays + audio -> final H.264/AAC file, exactly as long as the song."""
    work_dir = ensure_dir(work_dir)
    bed = concat_clips(clips, work_dir / "bed.mp4")
    overlay_track = build_overlay_track(
        overlay_items,
        directory=ensure_dir(work_dir / "overlay-clips"),
        out=work_dir / "overlay-track.mov",
        size=size,
        fps=fps,
        duration=duration,
        fade=fade,
        force=force,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    quality = ["-b:v", video_bitrate] if video_bitrate else ["-crf", "18"]
    log.info("Rendering %s with ffmpeg ...", output)
    run(
        [
            which("ffmpeg"), "-y", "-loglevel", "error", "-stats",
            "-i", str(bed),
            "-i", str(overlay_track),
            "-i", str(audio),
            "-filter_complex", "[0:v][1:v]overlay=format=auto:shortest=0[v]",
            "-map", "[v]", "-map", "2:a",
            "-c:v", "libx264", "-preset", "slow", *quality, "-pix_fmt", "yuv420p",
            "-r", f"{fps:g}",
            "-c:a", "aac", "-b:a", "320k",
            "-movflags", "+faststart",
            "-t", f"{duration:.3f}",
            str(output),
        ],
        capture=False,
        timeout=7200,
    )
    log.info("Wrote %s", output)
    return output
