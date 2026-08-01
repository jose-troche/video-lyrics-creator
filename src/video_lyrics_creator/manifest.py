from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import ManifestError
from .media import probe_media

DEFAULT_VIDEO = {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "transition": 0.75,
    "title_duration": 12.0,
    "title_fade": 0.75,
    "font": "Avenir Next Demi Bold",
    "font_size": 58,
    "margin_v": 72,
    "zoom": 1.08,
    "lyric_lead": 0.35,
    "lyric_fade": 0.2,
}


def _absolute(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def load_manifest(path: str | Path) -> tuple[Path, dict[str, Any]]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"Manifest does not exist: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Invalid JSON in {manifest_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("Manifest root must be a JSON object")

    base = manifest_path.parent
    for key in ("audio", "lyrics_source"):
        if data.get(key):
            data[key] = _absolute(str(data[key]), base)
    data["work_dir"] = _absolute(str(data.get("work_dir", "work")), base)
    data.setdefault("render", {})
    output = data["render"].get("output", "output/lyric-video.mp4")
    data["render"]["output"] = _absolute(str(output), base)
    data["video"] = {**DEFAULT_VIDEO, **data.get("video", {})}
    for scene in data.get("scenes", []):
        if scene.get("image"):
            scene["image"] = _absolute(str(scene["image"]), base)
    overlays = data.get("overlays", {})
    if overlays.get("title"):
        overlays["title"] = _absolute(str(overlays["title"]), base)
    for item in overlays.get("lyrics", []):
        if item.get("image"):
            item["image"] = _absolute(str(item["image"]), base)
    return manifest_path, data


def save_manifest(path: str | Path, data: dict[str, Any]) -> None:
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)


def new_manifest(
    *, title: str, audio: str, lyrics_source: str, visual_style: str, base: Path
) -> dict[str, Any]:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")
    slug = "-".join(part for part in slug.split("-") if part) or "lyric-video"
    return {
        "schema_version": 1,
        "title": title,
        "author": "José Troche",
        "audio": _absolute(audio, base),
        "lyrics_source": _absolute(lyrics_source, base),
        "visual_style": visual_style,
        "work_dir": str((base / "work").resolve()),
        "video": dict(DEFAULT_VIDEO),
        "image_generation": {
            "provider": "codex",
            "model": "gpt-image-2",
            "quality": "medium",
            "codex_timeout": 900,
        },
        "render": {
            "output": str((base / "output" / f"{slug}.mp4").resolve()),
            "format": "mp4",
            "codec": "H264",
            "audio_codec": "aac",
            "replace_existing": True,
        },
        "lyrics": [],
        "scenes": [],
        "overlays": {"title": "", "lyrics": []},
    }


def validate_manifest(
    data: dict[str, Any],
    *,
    require_timing: bool = True,
    require_images: bool = True,
    probe_audio: bool = True,
) -> float:
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not str(data.get("title", "")).strip():
        errors.append("title is required")
    if not str(data.get("author", "")).strip():
        errors.append("author is required")

    audio = Path(str(data.get("audio", "")))
    if not audio.is_file():
        errors.append(f"audio file does not exist: {audio}")
        duration = 0.0
    elif not probe_audio:
        try:
            duration = float(data.get("duration", 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            errors.append("duration must be positive in a staged Resolve job")
    else:
        duration = probe_media(audio)["duration"]

    video = data.get("video", {})
    for key in ("width", "height", "fps"):
        value = video.get(key)
        if not isinstance(value, (int, float)) or value <= 0:
            errors.append(f"video.{key} must be positive")
    transition = video.get("transition", 0)
    if not isinstance(transition, (int, float)) or transition < 0:
        errors.append("video.transition must be zero or positive")
    for key in ("title_fade", "lyric_lead", "lyric_fade"):
        value = video.get(key, 0)
        if not isinstance(value, (int, float)) or value < 0:
            errors.append(f"video.{key} must be zero or positive")
    title_duration = video.get("title_duration", 0)
    if not isinstance(title_duration, (int, float)) or title_duration <= 0:
        errors.append("video.title_duration must be positive")

    lyrics = data.get("lyrics", [])
    scenes = data.get("scenes", [])
    if require_timing and not lyrics:
        errors.append("lyrics timing is empty; run `video-lyrics prepare` first")
    if require_timing and not scenes:
        errors.append("scene plan is empty; run `video-lyrics prepare` first")
    _validate_ranges(lyrics, "lyrics", duration, errors, contiguous=False)
    _validate_ranges(scenes, "scenes", duration, errors, contiguous=True)

    if scenes and duration:
        if abs(float(scenes[0]["start"])) > 0.05:
            errors.append("first scene must start at 0")
        if abs(float(scenes[-1]["end"]) - duration) > 0.10:
            errors.append("final scene must end at the audio duration within 0.10 seconds")
        shortest = min(float(x["end"]) - float(x["start"]) for x in scenes)
        if len(scenes) > 1 and transition >= shortest / 2:
            errors.append("video.transition must be less than half the shortest scene")

    if require_images:
        for index, scene in enumerate(scenes, 1):
            image = Path(str(scene.get("image", "")))
            if not image.is_file():
                errors.append(f"scene {index} image does not exist: {image}")
        overlays = data.get("overlays", {})
        title = Path(str(overlays.get("title", "")))
        if not title.is_file():
            errors.append(f"title overlay does not exist: {title}")
        lyric_overlays = overlays.get("lyrics", [])
        if len(lyric_overlays) != len(lyrics):
            errors.append("lyric overlay count does not match lyric cue count")
        for index, item in enumerate(lyric_overlays, 1):
            image = Path(str(item.get("image", "")))
            if not image.is_file():
                errors.append(f"lyric overlay {index} does not exist: {image}")

    if errors:
        raise ManifestError("Manifest validation failed:\n- " + "\n- ".join(errors))
    return duration


def _validate_ranges(
    entries: list[dict[str, Any]],
    label: str,
    duration: float,
    errors: list[str],
    *,
    contiguous: bool,
) -> None:
    previous_end: float | None = None
    for index, entry in enumerate(entries, 1):
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label} {index} must have numeric start and end")
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            errors.append(f"{label} {index} times must be finite")
        if start < 0 or end <= start:
            errors.append(f"{label} {index} must satisfy 0 <= start < end")
        if duration and end > duration + 0.10:
            errors.append(f"{label} {index} ends after the audio")
        if previous_end is not None:
            if start < previous_end - 0.01:
                errors.append(f"{label} {index} overlaps the previous entry")
            if contiguous and abs(start - previous_end) > 0.05:
                errors.append(f"{label} {index} is not contiguous with the previous scene")
        previous_end = end
