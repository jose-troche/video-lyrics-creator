from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import VideoLyricsError
from .resolve import timeline_plan


def default_handoff_root() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Videos"
    elif sys.platform == "darwin":
        base = Path.home() / "Movies"
    else:
        base = Path.home() / "Videos"
    return base / "Video Lyrics Creator"


def slugify(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in slug.split("-") if part) or "lyric-video"


def stage_workspace_job(
    manifest_path: str | Path,
    manifest: dict[str, Any],
    *,
    project_name: str,
    timeline_name: str,
    replace_timeline: bool,
    render: bool,
    handoff_root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    root = Path(handoff_root).expanduser().resolve() if handoff_root else default_handoff_root()
    slug = slugify(str(manifest["title"]))
    job_dir = root / "Jobs" / slug
    media_dir = job_dir / "Media"
    scene_dir = media_dir / "Scenes"
    overlay_dir = media_dir / "Overlays"
    output_dir = root / "Output"
    for directory in (media_dir, scene_dir, overlay_dir, output_dir):
        directory.mkdir(parents=True, exist_ok=True)

    staged = deepcopy(manifest)
    plan = timeline_plan(staged)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoLyricsError("ffmpeg is required to stage duration-safe scene media")
    audio_source = Path(staged["audio"])
    audio_target = media_dir / f"audio{audio_source.suffix.lower()}"
    _copy_file(audio_source, audio_target)
    staged["audio"] = str(audio_target.resolve())

    for index, (scene, planned_scene) in enumerate(zip(staged["scenes"], plan["scenes"]), 1):
        source = Path(scene["image"])
        target = scene_dir / f"scene-{index:03d}.mp4"
        _render_still_video(
            ffmpeg,
            source,
            target,
            fps=float(plan["fps"]),
            frames=int(planned_scene["duration_frames"]),
        )
        scene["image"] = str(target.resolve())

    title_source = Path(staged["overlays"]["title"])
    title_target = overlay_dir / f"title{title_source.suffix.lower()}"
    _copy_file(title_source, title_target)
    staged["overlays"]["title"] = str(title_target.resolve())
    for index, overlay in enumerate(staged["overlays"]["lyrics"], 1):
        source = Path(overlay["image"])
        target = overlay_dir / f"lyric-{index:03d}{source.suffix.lower()}"
        _copy_file(source, target)
        overlay["image"] = str(target.resolve())

    output = output_dir / f"{slug}.mp4"
    staged["work_dir"] = str(job_dir.resolve())
    staged["render"]["output"] = str(output.resolve())
    staged["resolve_job"] = {
        "project_name": project_name,
        "timeline_name": timeline_name,
        "replace_timeline": bool(replace_timeline),
        "render": bool(render),
        "ffmpeg": ffmpeg,
    }

    job = {
        "job_schema_version": 1,
        "job_id": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_manifest": str(Path(manifest_path).expanduser().resolve()),
        "manifest": staged,
    }
    job_path = job_dir / "resolve-job.json"
    latest_path = root / "latest-job.json"
    _atomic_json(job_path, job)
    _atomic_json(latest_path, job)
    return job_path, job


def load_workspace_job(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        job = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VideoLyricsError(f"Resolve job does not exist: {source}") from exc
    except json.JSONDecodeError as exc:
        raise VideoLyricsError(f"Resolve job is not valid JSON: {source}: {exc}") from exc
    if job.get("job_schema_version") != 1 or not isinstance(job.get("manifest"), dict):
        raise VideoLyricsError(f"Unsupported Resolve job format: {source}")
    return job


def result_path_for_job(job_path: str | Path) -> Path:
    return Path(job_path).expanduser().resolve().with_name("resolve-result.json")


def _copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise VideoLyricsError(f"Cannot stage missing file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == target.resolve():
        return
    shutil.copy2(source, target)


def _render_still_video(
    ffmpeg: str,
    source: Path,
    target: Path,
    *,
    fps: float,
    frames: int,
) -> None:
    if not source.is_file():
        raise VideoLyricsError(f"Cannot stage missing scene image: {source}")
    if frames <= 0:
        raise VideoLyricsError(f"Scene video duration must be positive: {frames} frame(s)")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(source),
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-tune",
        "stillimage",
        "-crf",
        "1",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode or not target.is_file():
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise VideoLyricsError(f"Could not stage scene video {target.name}: {detail}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
