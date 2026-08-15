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

import hashlib
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, ensure_dir, log, run, short_hash, which

MOTIONS: dict[str, dict[str, float]] = {
    #             zoom start  zoom end  centre x start/end   centre y start/end
    "zoom_in":   {"z0": 0.0, "z1": 1.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.50, "cy1": 0.50},
    "zoom_out":  {"z0": 1.0, "z1": 0.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.50, "cy1": 0.50},
    "pan_right": {"z0": 1.0, "z1": 1.0, "cx0": 0.12, "cx1": 0.88, "cy0": 0.50, "cy1": 0.50},
    "pan_left":  {"z0": 1.0, "z1": 1.0, "cx0": 0.88, "cx1": 0.12, "cy0": 0.50, "cy1": 0.50},
    "pan_up":    {"z0": 1.0, "z1": 1.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.88, "cy1": 0.12},
    "pan_down":  {"z0": 1.0, "z1": 1.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.12, "cy1": 0.88},
    "still":     {"z0": 0.0, "z1": 0.0, "cx0": 0.50, "cx1": 0.50, "cy0": 0.50, "cy1": 0.50},
}

# A pan needs crop room to travel through; if the configured zoom is too close to
# 1.0 there is nothing to pan across, so pans get their own floor independent of
# the zoom_in/zoom_out setting.
PAN_MIN_ZOOM = 1.22

# zoompan crops at whole-pixel positions. Spreading a small zoom range across a
# long scene moves the crop by a fraction of a pixel per frame, which rounds to
# "holds still for several frames, then jumps one pixel" - reading as both too
# subtle and jerky at once. Scaling the zoom to the scene's own duration keeps the
# rate of motion (not just the total amount) consistent whether a scene is on
# screen for 3 seconds or 20, and the higher supersample factor gives the crop
# sub-pixel precision before it's downscaled to the output size, which is what
# actually removes the stepping.
MOTION_REFERENCE_SECONDS = 6.0
MOTION_MIN_ZOOM = 1.08
MOTION_MAX_ZOOM = 1.55
DEFAULT_SUPERSAMPLE = 3


def default_jobs() -> int:
    """How many clips to bake at once. Two, and there is no point going higher.

    Every clip is an independent ffmpeg process writing to its own fingerprinted
    path, so any number of them can run at once - but ffmpeg already threads
    internally and very nearly fills the machine on its own. Measured on a 144s
    song: 279s with one worker, 229s with two, 225s with four. The second worker
    soaks up what the first leaves idle; the third and fourth buy nothing and only
    add resident memory (1.4 GB at two, 2.5 GB at four), which matters on a small
    container.

    `os.cpu_count()` is no help in picking this - on Apple Silicon it counts the
    efficiency cores too, so "half the cores" is really all of the fast ones.

    Returning 1 here turns the concurrency off completely; see the note above
    `_bake` for that and for how to remove it altogether.
    """
    return 1 if (os.cpu_count() or 2) < 4 else 2


CODECS = {
    "h264": ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p"],
    "prores": ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
}
CONTAINER = {"h264": ".mp4", "prores": ".mov"}


def scene_zoom(base_zoom: float, duration: float) -> float:
    """Scale the configured zoom to how long the scene is actually on screen.

    Without this, a fixed zoom value spread over a scene's whole span means quick
    cuts barely move and long holds crawl even slower - motion that reads as
    inconsistent rather than steady. Scaling by duration keeps the apparent speed
    similar across scenes of different lengths.
    """
    base_zoom = max(1.001, float(base_zoom))
    extra = (base_zoom - 1.0) * (max(0.0, duration) / MOTION_REFERENCE_SECONDS)
    return min(MOTION_MAX_ZOOM, max(MOTION_MIN_ZOOM, 1.0 + extra))


def _zoom_levels(motion: str, zoom: float) -> tuple[float, float]:
    """Map the 0/1 markers in MOTIONS onto real zoom factors."""
    zoom = max(1.001, float(zoom))
    spec = MOTIONS[motion]
    pan = motion.startswith("pan")
    if pan:
        zoom = max(zoom, PAN_MIN_ZOOM)
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
    zoom = scene_zoom(zoom, motion_span / fps)
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


def _image_signature(path: str) -> str:
    """A hash of the image's own bytes, so a scene's clip is re-baked when the
    picture changes and only then.

    Not the path alone: `video-lyrics images --force` regenerates under the same
    filename (it is hashed from the prompt, not the picture), so an unchanged path
    says nothing about what is in the file, and the clip would stay stale forever.
    Not the mtime either: the images stage rewrites every canonical PNG from its
    raw download on every run (see `_adopt_by_stem`), so a timestamp would re-bake
    the whole bed each time that stage is re-run, changing nothing.
    """
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _staged(path: Path) -> Iterator[Path]:
    """Write to a sibling temp file and move it into place only on success.

    A clip's name is a fingerprint of its inputs, so a half-written file left
    behind by a failed or interrupted ffmpeg would look like a valid cache hit on
    the next run and never be rebuilt. The temp file keeps the real suffix because
    ffmpeg picks its muxer from the extension.
    """
    temp = path.with_name(f"{path.stem}.part{path.suffix}")
    try:
        yield temp
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


# Concurrency here is a bolt-on, and deliberately easy to take back off. If it
# ever causes trouble - a container that runs out of memory, a machine that
# thrashes, an interrupt that takes too long to land - there are two levels of
# retreat, in order of preference:
#
#   1. Make `default_jobs` return 1. `_bake` then runs every task inline and no
#      thread pool is ever constructed. That is the whole of the concurrency,
#      off, in one line; nothing else needs touching and no output changes.
#   2. To remove the code entirely: delete `_bake`, `default_jobs` and the `jobs`
#      parameter of `render_bed`; call `_render_scene_clip` /
#      `_render_transition_clip` straight from the loop again instead of
#      collecting `partial`s into `pending`; then drop the `os`, `Callable`,
#      `ThreadPoolExecutor` and `partial` imports, which nothing else uses.
#
# Keep `_staged` either way. It is independent of all of this and fixes a bug
# that predates it: a half-written clip left behind by a failed ffmpeg still has
# a valid fingerprinted name, so the next run reads it as a cache hit and never
# rebuilds it. Deleting it alongside the concurrency would quietly restore that.
def _bake(tasks: list[Callable[[], None]], *, jobs: int) -> None:
    """Run the clip renders, several at a time.

    Nothing is shared between them and the order they finish in does not matter -
    each one's destination is already decided by its fingerprint - so the only
    thing concurrency changes is the wall clock.
    """
    if not tasks:
        return
    jobs = max(1, min(jobs, len(tasks)))
    if jobs == 1:
        for task in tasks:
            task()
        return

    log.info("Baking %d clips, %d at a time", len(tasks), jobs)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(task) for task in tasks]
        try:
            for future in futures:
                future.result()
        except BaseException:
            # Don't start any clip that hasn't begun yet; the pool still waits for
            # the ones already in flight as it shuts down.
            for future in futures:
                future.cancel()
            raise


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
    supersample: int = DEFAULT_SUPERSAMPLE,
    jobs: int | None = None,
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
    pending: list[Callable[[], None]] = []

    for position, clip in enumerate(clips, start=1):
        if clip["kind"] == "scene":
            scene = scenes[clip["scene"]]
            fingerprint = short_hash(
                scene["image"], _image_signature(scene["image"]), scene["motion"],
                clip["first_frame"], clip["frames"], fps, size, zoom, codec, supersample,
            )
            path = directory / f"bed-{position:03d}-scene-{fingerprint}{suffix}"
            if force or not path.is_file():
                pending.append(partial(
                    _render_scene_clip,
                    ffmpeg, scene, clip, path,
                    size=size, fps=fps, zoom=zoom, codec=codec, supersample=supersample,
                ))
        else:
            outgoing = scenes[clip["from_scene"]]
            incoming = scenes[clip["to_scene"]]
            fingerprint = short_hash(
                outgoing["image"], _image_signature(outgoing["image"]),
                incoming["image"], _image_signature(incoming["image"]),
                outgoing["motion"], incoming["motion"],
                clip["first_frame"], clip["frames"], fps, size, zoom, codec, supersample,
            )
            path = directory / f"bed-{position:03d}-xfade-{fingerprint}{suffix}"
            if force or not path.is_file():
                pending.append(partial(
                    _render_transition_clip,
                    ffmpeg, outgoing, incoming, clip, path,
                    size=size, fps=fps, zoom=zoom, codec=codec, supersample=supersample,
                ))
        clip["path"] = str(path)
        clip["start"] = round(clip["first_frame"] / fps, 4)
        clip["end"] = round((clip["first_frame"] + clip["frames"]) / fps, 4)

    _bake(pending, jobs=default_jobs() if jobs is None else jobs)
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
    with _staged(path) as temp:
        run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-loop", "1", "-framerate", f"{fps:g}", "-i", scene["image"],
                "-vf", f"{chain},format=yuv420p",
                "-frames:v", str(clip["frames"]),
                "-r", f"{fps:g}", "-an",
                *CODECS[codec],
                str(temp),
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
    with _staged(path) as temp:
        run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-loop", "1", "-framerate", f"{fps:g}", "-t", f"{seconds:.4f}",
                "-i", outgoing["image"],
                "-loop", "1", "-framerate", f"{fps:g}", "-t", f"{seconds:.4f}",
                "-i", incoming["image"],
                "-filter_complex", graph, "-map", "[v]",
                "-frames:v", str(frames),
                "-r", f"{fps:g}", "-an",
                *CODECS[codec],
                str(temp),
            ],
            timeout=1800,
        )


def concat_clips(clips: list[dict[str, Any]], out: Path) -> Path:
    """Join the bed clips into one file (used by the ffmpeg render engine).

    Always re-concatenated: a `force` bed rebuild can overwrite a clip's content
    without changing its (fingerprinted) filename, which a manifest-based cache
    would miss, leaving a stale `bed.mp4` behind. The concat itself is a stream
    copy and takes well under a second, so there is no cost to just doing it
    every time.
    """
    listing = out.with_suffix(".txt")
    manifest = "".join(f"file '{Path(clip['path']).as_posix()}'\n" for clip in clips)
    listing.write_text(manifest, encoding="utf-8")
    run(
        [
            which("ffmpeg"), "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(out),
        ],
        timeout=1800,
    )
    return out
