"""Produce one still image per scene.

Four providers:
  * ``codex``    - the Codex CLI's built-in image_gen tool, run in full-auto mode.
  * ``supplied`` - images the user already has, taken in filename order.
  * ``manual``   - no generator at all: write every scene's prompt and expected
                   filename to a manifest so the user can create each image by hand
                   (ChatGPT, Midjourney, ...) and drop it into the images folder.
  * ``meta``     - drives meta.ai in a real browser (see meta_ai.py). Raw downloads
                   land in `images/images.src/` and are converted into the
                   canonical PNG in `images/` itself, same as `manual`.
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
RAW_SUBDIR = "images.src"

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
    meta_headless: bool = False,
    meta_profile_dir: str | None = None,
    meta_min_delay: float = 8.0,
    meta_max_delay: float = 20.0,
    meta_composer_selector: str | None = None,
    meta_image_selector: str | None = None,
) -> list[dict[str, Any]]:
    """Attach an `image` path to every scene."""
    images_dir = ensure_dir(images_dir)

    if provider == "supplied" or source_dir:
        return _assign_supplied(scenes, source_dir, images_dir, size)
    if provider == "manual":
        return _generate_manual(scenes, images_dir, size, force)
    if provider == "meta":
        return _generate_meta(
            scenes, images_dir, size, force,
            headless=meta_headless, profile_dir=meta_profile_dir,
            min_delay=meta_min_delay, max_delay=meta_max_delay,
            composer_selector=meta_composer_selector, image_selector=meta_image_selector,
        )
    if provider != "codex":
        raise VideoLyricsError(
            f"Unknown image provider {provider!r} (use codex, manual, meta, or supplied)."
        )

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


def _stem_for(scene: dict[str, Any], tag: str) -> str:
    fingerprint = short_hash(scene["prompt"], tag)
    return f"scene-{scene['index']:03d}-{fingerprint}"


def _find_image_by_stem(directory: Path, stem: str) -> Path | None:
    """Any of IMAGE_SUFFIXES counts - the format doesn't matter, only the stem."""
    for suffix in IMAGE_SUFFIXES:
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _adopt_by_stem(
    scene: dict[str, Any],
    stem: str,
    *,
    search_dir: Path,
    images_dir: Path,
    size: tuple[int, int],
    delete_source: bool,
) -> bool:
    """If `search_dir` holds a readable file matching `stem`, convert it into the
    canonical PNG in `images_dir` and attach it to the scene. Returns whether that
    succeeded - a missing or unreadable file both count as not adopted."""
    found = _find_image_by_stem(search_dir, stem)
    if found is None:
        return False
    if not _valid(found):
        log.warning("  scene %03d: %s is not a readable image, regenerating", scene["index"], found.name)
        return False
    canonical = images_dir / f"{stem}.png"
    if found != canonical:
        with Image.open(found) as image:
            image.convert("RGB").save(canonical, "PNG")
        if delete_source:
            found.unlink()
    _postprocess(canonical, size)
    scene["image"] = str(canonical)
    return True


def _generate_manual(
    scenes: list[dict[str, Any]],
    images_dir: Path,
    size: tuple[int, int],
    force: bool,
) -> list[dict[str, Any]]:
    """Hand the prompts to the user instead of generating anything.

    Writes every outstanding scene's prompt and expected filename (stem) to
    ``prompts.txt``. Once the user has created each image by hand - in any of
    IMAGE_SUFFIXES - and saved it under that stem, re-running this (same command)
    picks the files up from disk and normalises them to PNG - nothing to type back in.
    """
    pending: list[tuple[dict[str, Any], str]] = []
    for scene in scenes:
        stem = _stem_for(scene, "manual")
        if not force and _adopt_by_stem(
            scene, stem, search_dir=images_dir, images_dir=images_dir,
            size=size, delete_source=True,
        ):
            continue
        pending.append((scene, stem))

    if not pending:
        log.info("All %d scene images already generated.", len(scenes))
        return scenes

    manifest = images_dir / "prompts.txt"
    suffixes = "/".join(suffix.lstrip(".") for suffix in IMAGE_SUFFIXES)
    blocks = [
        f"File: {stem}.<{suffixes}>\nPrompt: {scene['prompt']}" for scene, stem in pending
    ]
    manifest.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    log.info(
        "Manual mode: wrote %d prompt(s) to %s.\n"
        "Generate each image yourself (ChatGPT, Midjourney, ...), save it under the exact "
        "filename shown (any of .%s), into %s, then re-run `video-lyrics images`.",
        len(pending), manifest, suffixes, images_dir,
    )
    return scenes


def _generate_meta(
    scenes: list[dict[str, Any]],
    images_dir: Path,
    size: tuple[int, int],
    force: bool,
    *,
    headless: bool,
    profile_dir: str | None,
    min_delay: float,
    max_delay: float,
    composer_selector: str | None = None,
    image_selector: str | None = None,
) -> list[dict[str, Any]]:
    """Drive meta.ai in a real browser to generate each outstanding scene's image.

    Raw downloads land in `images_dir/images.src/<stem>.<ext>` and are kept there
    (whatever format meta.ai served); each is also converted into the canonical
    PNG in `images_dir` itself. A run interrupted partway through only asks the
    browser for what is still missing - anything already in images.src is reused.
    """
    raw_dir = ensure_dir(images_dir / RAW_SUBDIR)

    pending: list[tuple[dict[str, Any], str]] = []
    for scene in scenes:
        stem = _stem_for(scene, "meta")
        if not force and _adopt_by_stem(
            scene, stem, search_dir=raw_dir, images_dir=images_dir,
            size=size, delete_source=False,
        ):
            continue
        pending.append((scene, stem))

    if not pending:
        log.info("All %d scene images already generated.", len(scenes))
        return scenes

    from . import meta_ai as meta_mod

    meta_kwargs: dict[str, Any] = dict(
        raw_dir=raw_dir, headless=headless, profile_dir=profile_dir,
        min_delay=min_delay, max_delay=max_delay,
    )
    if composer_selector:
        meta_kwargs["composer_selector"] = composer_selector
    if image_selector:
        meta_kwargs["image_selector"] = image_selector
    meta_mod.generate([scene for scene, _ in pending], **meta_kwargs)

    missing = []
    for scene, stem in pending:
        if not _adopt_by_stem(
            scene, stem, search_dir=raw_dir, images_dir=images_dir,
            size=size, delete_source=False,
        ):
            missing.append(scene["index"])
    if missing:
        log.warning("meta.ai produced no usable image for scene(s): %s", missing)
    return scenes


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
