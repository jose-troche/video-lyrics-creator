"""Produce one still image per scene.

Two providers:
  * ``codex``    - the Codex CLI's built-in image_gen tool, run in full-auto mode.
  * ``supplied`` - images the user already has, taken in filename order.
"""

from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PIL import Image

from .util import VideoLyricsError, ensure_dir, log, run, short_hash, which

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
GENERATION_SIZE = "1536x1024"
CODEX_TIMEOUT = 900.0

INSTRUCTION = """Generate exactly one image using your built-in image_gen tool.

Image prompt:
{prompt}

Rules:
- Request size {size} (landscape).
- Save the generated image to exactly this path: {target}
- Overwrite it if it already exists. Create no other files.
- Do not ask questions, do not write code, do not explain. When the file exists, reply
  with the single word DONE.
"""


def scene_image_path(images_dir: Path, scene: dict[str, Any], fingerprint: str) -> Path:
    return images_dir / f"scene-{scene['index']:03d}-{fingerprint}.png"


def generate(
    scenes: list[dict[str, Any]],
    *,
    images_dir: Path,
    provider: str = "codex",
    model: str = "gpt-image-2",
    quality: str = "medium",
    source_dir: str | Path | None = None,
    size: tuple[int, int] = (1920, 1080),
    force: bool = False,
    jobs: int = 1,
) -> list[dict[str, Any]]:
    """Attach an `image` path to every scene."""
    images_dir = ensure_dir(images_dir)

    if provider == "supplied" or source_dir:
        return _assign_supplied(scenes, source_dir, images_dir, size)
    if provider != "codex":
        raise VideoLyricsError(f"Unknown image provider {provider!r} (use codex or supplied).")

    pending: list[tuple[dict[str, Any], Path]] = []
    for scene in scenes:
        fingerprint = short_hash(scene["prompt"], model, quality)
        target = scene_image_path(images_dir, scene, fingerprint)
        if target.is_file() and _valid(target) and not force:
            scene["image"] = str(target)
            continue
        existing = scene.get("image")
        if existing and Path(existing).is_file() and _valid(Path(existing)) and not force:
            continue
        pending.append((scene, target))

    if not pending:
        log.info("All %d scene images already generated.", len(scenes))
        return scenes

    log.info("Generating %d scene image(s) with codex image_gen ...", len(pending))
    codex = which("codex")

    def worker(item: tuple[dict[str, Any], Path]) -> None:
        scene, target = item
        _generate_one(codex, scene, target, images_dir, model, quality, size)
        scene["image"] = str(target)

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(worker, pending))
    else:
        for item in pending:
            worker(item)
    return scenes


def _generate_one(
    codex: str,
    scene: dict[str, Any],
    target: Path,
    images_dir: Path,
    model: str,
    quality: str,
    size: tuple[int, int],
) -> None:
    instruction = INSTRUCTION.format(
        prompt=scene["prompt"], size=GENERATION_SIZE, target=target
    )
    started = time.time()
    log.info("  scene %03d: %s", scene["index"], _summary(scene))
    run(
        [
            codex, "exec", "--full-auto", "--skip-git-repo-check",
            "-C", str(images_dir),
            instruction,
        ],
        timeout=CODEX_TIMEOUT,
    )
    if not target.is_file():
        rescued = _rescue(images_dir, started)
        if rescued is None:
            raise VideoLyricsError(
                f"codex did not produce an image for scene {scene['index']}. "
                f"Expected {target}. Check `codex login` and image_gen availability."
            )
        log.info("  scene %03d: recovered generated file %s", scene["index"], rescued.name)
        shutil.move(str(rescued), target)
    _postprocess(target, size)
    log.info("  scene %03d: %s (%.0fs)", scene["index"], target.name, time.time() - started)


def _summary(scene: dict[str, Any]) -> str:
    if scene.get("lines"):
        return " / ".join(scene["lines"])[:70]
    return "(instrumental)"


def _rescue(images_dir: Path, since: float) -> Path | None:
    """codex sometimes saves under its own filename; find the newest new image."""
    candidates: list[Path] = []
    search_dirs = [images_dir, Path.home() / ".codex" / "generated_images"]
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.suffix.lower() in IMAGE_SUFFIXES and path.stat().st_mtime >= since - 1:
                candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _valid(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:  # noqa: BLE001 - any unreadable file is simply invalid
        return False


def _postprocess(path: Path, size: tuple[int, int]) -> None:
    """Normalise to RGB, 16:9, and at least the target resolution."""
    width, height = size
    with Image.open(path) as image:
        image = image.convert("RGB")
        target_ratio = width / height
        source_ratio = image.width / image.height
        if abs(source_ratio - target_ratio) > 0.01:
            if source_ratio > target_ratio:  # too wide -> crop sides
                new_width = int(round(image.height * target_ratio))
                left = (image.width - new_width) // 2
                image = image.crop((left, 0, left + new_width, image.height))
            else:                            # too tall -> crop top/bottom
                new_height = int(round(image.width / target_ratio))
                top = (image.height - new_height) // 2
                image = image.crop((0, top, image.width, top + new_height))
        if image.width < width:
            image = image.resize((width, int(round(width / target_ratio))), Image.LANCZOS)
        image.save(path, "PNG")


def _assign_supplied(
    scenes: list[dict[str, Any]],
    source_dir: str | Path | None,
    images_dir: Path,
    size: tuple[int, int],
) -> list[dict[str, Any]]:
    if not source_dir:
        raise VideoLyricsError("provider 'supplied' needs --images-dir pointing at a folder.")
    source = Path(source_dir).expanduser()
    if not source.is_dir():
        raise VideoLyricsError(f"Images folder not found: {source}")
    files = sorted(
        path for path in source.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not files:
        raise VideoLyricsError(f"No images found in {source}")
    if len(files) < len(scenes):
        log.warning(
            "%d supplied images for %d scenes; images will repeat.", len(files), len(scenes)
        )
    for index, scene in enumerate(scenes):
        chosen = files[index % len(files)]
        target = images_dir / f"scene-{scene['index']:03d}-{short_hash(chosen.name)}.png"
        if not target.is_file():
            with Image.open(chosen) as image:
                image.convert("RGB").save(target, "PNG")
            _postprocess(target, size)
        scene["image"] = str(target)
    return scenes
