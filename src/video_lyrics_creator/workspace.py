from __future__ import annotations

import json
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import VideoLyricsError
from .handoff import default_handoff_root, load_workspace_job, result_path_for_job
from .manifest import validate_manifest
from .resolve import ResolveTimelineBuilder


def run_latest_workspace_job(resolve: Any) -> dict[str, Any]:
    return run_workspace_job(resolve, default_handoff_root() / "latest-job.json")


def run_workspace_job(resolve: Any, job_path: str | Path) -> dict[str, Any]:
    source = Path(job_path).expanduser().resolve()
    job = load_workspace_job(source)
    result_path = result_path_for_job(source)
    existing = _read_result(result_path)
    if existing.get("job_id") == job["job_id"] and existing.get("status") == "complete":
        raise VideoLyricsError(
            "The latest Resolve job has already completed. Run `video-lyrics build` in the "
            "terminal to stage a new job before launching this script again."
        )

    manifest = job["manifest"]
    manifest["duration"] = validate_manifest(manifest, probe_audio=False)
    settings = manifest.get("resolve_job", {})
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        builder = ResolveTimelineBuilder(resolve, manifest)
        build = builder.build(
            project_name=str(settings.get("project_name") or f"{manifest['title']} - Lyric Video"),
            timeline_name=str(settings.get("timeline_name") or manifest["title"]),
            replace_timeline=bool(settings.get("replace_timeline", False)),
            render=bool(settings.get("render", True)),
            wait=bool(settings.get("render", True)),
        )
        if settings.get("render", True):
            _mux_original_audio(
                manifest["render"]["output"],
                manifest["audio"],
                str(settings.get("ffmpeg") or ""),
            )
        result = {
            "job_id": job["job_id"],
            "status": "complete",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project_name": build.project_name,
            "timeline_name": build.timeline_name,
            "render_job_id": build.render_job_id,
            "render_status": build.render_status,
            "output": manifest["render"]["output"] if settings.get("render", True) else None,
        }
        _write_result(result_path, result)
        return result
    except Exception as exc:
        result = {
            "job_id": job["job_id"],
            "status": "failed",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        _write_result(result_path, result)
        raise


def _read_result(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _mux_original_audio(output_path: str | Path, audio_path: str | Path, ffmpeg: str) -> None:
    output = Path(output_path).expanduser().resolve()
    audio = Path(audio_path).expanduser().resolve()
    if not output.is_file():
        raise VideoLyricsError(f"Resolve render is missing before audio finalization: {output}")
    if not audio.is_file():
        raise VideoLyricsError(f"Original staged audio is missing: {audio}")
    executable = Path(ffmpeg).expanduser() if ffmpeg else None
    if not executable or not executable.is_file():
        raise VideoLyricsError(
            "The staged ffmpeg executable is unavailable; restage with `video-lyrics build`."
        )

    temporary = output.with_name(f".{output.stem}.audio-mux{output.suffix}")
    command = [
        str(executable),
        "-v",
        "error",
        "-y",
        "-i",
        str(output),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode or not temporary.is_file():
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise VideoLyricsError(f"Could not mux the original WAV into the final video: {detail}")
    temporary.replace(output)
