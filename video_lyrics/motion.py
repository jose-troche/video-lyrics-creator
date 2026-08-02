"""Ken Burns motion and cross dissolves, baked with ffmpeg.

The result is a contiguous chain of clips that tiles the whole song:

    [scene 1 body][dissolve][scene 2 body][dissolve][scene 3 body] ...

Each dissolve clip contains the blend of the two neighbouring images *with their
motion continuing through the overlap*, so the chain can be laid end to end on a
single video track and still look like a real cross dissolve.  That matters
because DaVinci Resolve's scripting API can neither add transitions nor keyframe
a clip, so anything animated has to arrive as media.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import VideoLyricsError, ensure_dir, log, run, short_hash, which

MOTIONS: dict[str, dict[str, float]] = {
    #             zoom start  zoom end  centre x start/end   centre y start/end
    "zoom_in":   {"z0": 0.0, "z1": 1.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.50, "cy1": 0.50},
    "zoom_out":  {"z0": 1.0, "z1": 0.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.50, "cy1": 0.50},
    "pan_right": {"z0": 1.0, "z1": 1.0, "cx0": 0.25, "cx1": 0.75, "cy0": 0.50, "cy1": 0.50},
    "pan_left":  {"z0": 1.0, "z1": 1.0, "cx0": 0.75, "cx1": 0.25, "cy0": 0.50, "cy1": 0.50},
    "pan_up":    {"z0": 1.0, "z1": 1.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.75, "cy1": 0.25},
    "pan_down":  {"z0": 1.0, "z1": 1.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.25, "cy1": 0.75},
    "still":     {"z0": 0.0, "z1": 0.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.50, "cy1": 0.50},
}

CODECS = {
    "h264": ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p"],
    "prores": ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
}
CONTAINER = {"h264": ".mp4", "prores": ".mov"}


def _zoom_levels(motion: str, zoom: float) -> tuple[float, float]:
    """Map the 0/1 markers in MOTIONS onto real zoom factors."""
    zoom = max(1.001, float(zoom))
    spec = MOTIONS[motion]
    pan = motion.startswith("pan")
    low, high = (zoom, zoom) if pan else (1.0, zoom)
    return (low if spec["z0"] == 0.0 else high, low if spec["z1"] == 0.0 else high)


def _expression(start: float, end: float, first_frame: int, motion_start: int, span: int) -> str:
    """Linear interpolation between two values across the scene's motion span."""
    if abs(end - start) < 1e-6:
        return f"{start:.6f}"
    denominator = max(1, span - 1)
    progress = f"(({first_frame}+on-{motion_start})/{denominator})"
    return f"({start:.6f}+({end - start:.6f})*{progress})"


def zoompan_filter(
    *,
    motion: str,
    zoom: float,
    size: tuple[int, int],
    fps: float,
    first_frame: int,
    motion_start: int,
    motion_span: int,
    supersample: int,
) -> str:
    """A scale+zoompan chain that renders this clip's slice of a scene's motion."""
    spec = MOTIONS.get(motion) or MOTIONS["zoom_in"]
    z0, z1 = _zoom_levels(motion if motion in MOTIONS else "zoom_in", zoom)
    width, height = size

    z_expr = _expression(z0, z1, first_frame, motion_start, motion_span)
    cx_expr = _expression(spec["cx0"], spec["cx1"], first_frame, motion_start, motion_span)
    cy_expr = _expression(spec["cy0"], spec["cy1"], first_frame, motion_start, motion_span)

    scaled_w = width * supersample
    scaled_h = height * supersample
    return (
        f"scale={scaled_w}:{scaled_h}:flags=lanczos,setsar=1,"
        f"zoompan=z='{z_expr}'"
        f":x='(iw-iw/zoom)*{cx_expr}'"
        f":y='(ih-ih/zoom)*{cy_expr}'"
        f":d=1:s={width}x{height}:fps={fps:g}"
    )


def plan_bed(
    scenes: list[dict[str, Any]], *, fps: float, duration: float, transition: float
) -> list[dict[str, Any]]:
    """Lay out the clip chain in frames: scene bodies separated by dissolves."""
    if not scenes:
        raise VideoLyricsError("No scenes to render. Run `video-lyrics plan` first.")

    total = int(round(duration * fps))
    starts = [int(round(scene["start"] * fps)) for scene in scenes]
    starts[0] = 0
    ends = [starts[index + 1] for index in range(len(scenes) - 1)] + [total]

    half = max(0, int(round(transition * fps / 2)))
    halves = [0] + [half] * (len(scenes) - 1) + [0]

    # Shrink dissolves that would eat a whole scene.
    for _ in range(6):
        adjusted = False
        for index in range(len(scenes)):
            body = (ends[index] - halves[index + 1]) - (starts[index] + halves[index])
            if body >= 1:
                continue
            adjusted = True
            shortfall = 1 - body
            for side in (index, index + 1):
                if 0 < side < len(halves) - 1 and halves[side] > 0:
                    halves[side] = max(0, halves[side] - (shortfall + 1) // 2)
        if not adjusted:
            break

    clips: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes):
        if halves[index] > 0:
            first = starts[index] - halves[index]
            clips.append(
                {
                    "kind": "transition",
                    "first_frame": first,
                    "frames": halves[index] * 2,
                    "from_scene": index - 1,
                    "to_scene": index,
                }
            )
        body_first = starts[index] + halves[index]
        body_frames = (ends[index] - halves[index + 1]) - body_first
        if body_frames > 0:
            clips.append(
                {
                    "kind": "scene",
                    "first_frame": body_first,
                    "frames": body_frames,
                    "scene": index,
                }
            )

    for index, scene in enumerate(scenes):
        scene["motion_first_frame"] = starts[index] - halves[index]
        scene["motion_frames"] = (ends[index] + halves[index + 1]) - (starts[index] - halves[index])

    covered = sum(clip["frames"] for clip in clips)
    if covered != total:
        log.debug("Bed covers %d frames, audio is %d frames; padding last clip.", covered, total)
        clips[-1]["frames"] += total - covered
    return clips


def render_bed(
    scenes: list[dict[str, Any]],
    *,
    directory: Path,
    size: tuple[int, int],
    fps: float,
    duration: float,
    transition: float,
    zoom: float,
    codec: str = "h264",
    supersample: int = 2,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Render every clip of the image bed. Returns the clip chain with paths."""
    directory = ensure_dir(directory)
    if codec not in CODECS:
        raise VideoLyricsError(f"Unknown intermediate codec {codec!r} (use h264 or prores).")
    for scene in scenes:
        if not scene.get("image"):
            raise VideoLyricsError(
                f"Scene {scene['index']} has no image. Run `video-lyrics images` first."
            )

    clips = plan_bed(scenes, fps=fps, duration=duration, transition=transition)
    ffmpeg = which("ffmpeg")
    suffix = CONTAINER[codec]

    for position, clip in enumerate(clips, start=1):
        if clip["kind"] == "scene":
            scene = scenes[clip["scene"]]
            fingerprint = short_hash(
                scene["image"], scene["motion"], clip["first_frame"], clip["frames"],
                fps, size, zoom, codec, supersample,
            )
            path = directory / f"bed-{position:03d}-scene-{fingerprint}{suffix}"
            if force or not path.is_file():
                _render_scene_clip(
                    ffmpeg, scene, clip, path,
                    size=size, fps=fps, zoom=zoom, codec=codec, supersample=supersample,
                )
        else:
            outgoing = scenes[clip["from_scene"]]
            incoming = scenes[clip["to_scene"]]
            fingerprint = short_hash(
                outgoing["image"], incoming["image"], outgoing["motion"], incoming["motion"],
                clip["first_frame"], clip["frames"], fps, size, zoom, codec, supersample,
            )
            path = directory / f"bed-{position:03d}-xfade-{fingerprint}{suffix}"
            if force or not path.is_file():
                _render_transition_clip(
                    ffmpeg, outgoing, incoming, clip, path,
                    size=size, fps=fps, zoom=zoom, codec=codec, supersample=supersample,
                )
        clip["path"] = str(path)
        clip["start"] = round(clip["first_frame"] / fps, 4)
        clip["end"] = round((clip["first_frame"] + clip["frames"]) / fps, 4)

    log.info("Image bed ready: %d clips in %s", len(clips), directory)
    return clips


def _render_scene_clip(
    ffmpeg: str,
    scene: dict[str, Any],
    clip: dict[str, Any],
    path: Path,
    *,
    size: tuple[int, int],
    fps: float,
    zoom: float,
    codec: str,
    supersample: int,
) -> None:
    chain = zoompan_filter(
        motion=scene["motion"],
        zoom=zoom,
        size=size,
        fps=fps,
        first_frame=clip["first_frame"],
        motion_start=scene["motion_first_frame"],
        motion_span=scene["motion_frames"],
        supersample=supersample,
    )
    log.info("  clip %s (%d frames) from scene %d", path.name, clip["frames"], scene["index"])
    run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", f"{fps:g}", "-i", scene["image"],
            "-vf", f"{chain},format=yuv420p",
            "-frames:v", str(clip["frames"]),
            "-r", f"{fps:g}", "-an",
            *CODECS[codec],
            str(path),
        ],
        timeout=1800,
    )


def _render_transition_clip(
    ffmpeg: str,
    outgoing: dict[str, Any],
    incoming: dict[str, Any],
    clip: dict[str, Any],
    path: Path,
    *,
    size: tuple[int, int],
    fps: float,
    zoom: float,
    codec: str,
    supersample: int,
) -> None:
    frames = clip["frames"]
    seconds = frames / fps
    chain_a = zoompan_filter(
        motion=outgoing["motion"], zoom=zoom, size=size, fps=fps,
        first_frame=clip["first_frame"],
        motion_start=outgoing["motion_first_frame"],
        motion_span=outgoing["motion_frames"],
        supersample=supersample,
    )
    chain_b = zoompan_filter(
        motion=incoming["motion"], zoom=zoom, size=size, fps=fps,
        first_frame=clip["first_frame"],
        motion_start=incoming["motion_first_frame"],
        motion_span=incoming["motion_frames"],
        supersample=supersample,
    )
    graph = (
        f"[0:v]{chain_a}[a];[1:v]{chain_b}[b];"
        f"[a][b]xfade=transition=fade:duration={seconds:.4f}:offset=0,format=yuv420p[v]"
    )
    log.info("  clip %s (%d frames) cross dissolve", path.name, frames)
    run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", f"{fps:g}", "-t", f"{seconds:.4f}", "-i", outgoing["image"],
            "-loop", "1", "-framerate", f"{fps:g}", "-t", f"{seconds:.4f}", "-i", incoming["image"],
            "-filter_complex", graph, "-map", "[v]",
            "-frames:v", str(frames),
            "-r", f"{fps:g}", "-an",
            *CODECS[codec],
            str(path),
        ],
        timeout=1800,
    )


def concat_clips(clips: list[dict[str, Any]], out: Path, *, force: bool = False) -> Path:
    """Join the bed clips into one file (used by the ffmpeg render engine)."""
    if out.is_file() and not force:
        return out
    listing = out.with_suffix(".txt")
    listing.write_text(
        "".join(f"file '{Path(clip['path']).as_posix()}'\n" for clip in clips), encoding="utf-8"
    )
    run(
        [
            which("ffmpeg"), "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(out),
        ],
        timeout=1800,
    )
    return out
