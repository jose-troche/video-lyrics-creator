"""Produce one still image per scene.

Four providers:
  * ``chatgpt``  - drives chatgpt.com in a real browser (see chatgpt.py).
  * ``meta``     - drives meta.ai the same way (see meta_ai.py).
  * ``supplied`` - images the user already has, taken in filename order.
  * ``manual``   - no generator at all: write every scene's prompt and expected
                   filename to a manifest so the user can create each image by hand
                   (Midjourney, an image site, ...) and drop it into the images folder.

Every provider writes into the one `images/` folder, under the scene's own stem and
in whatever format it was given - the file that lands there is the generator's own,
kept byte for byte unless its shape or colour mode actually needs fixing.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from .util import VideoLyricsError, ensure_dir, log, scene_stem, short_hash

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

# How far below the render's own width an image may be before it is worth saying
# so. The motion stage upscales whatever it is handed, so a little under is
# invisible; half is a 2x interpolation and will look soft.
SOFT_WIDTH_RATIO = 0.5

# provider -> the module that drives that site. Each exposes a `generate` taking
# `images_dir` plus BROWSER_OPTIONS as keyword arguments; the provider name is also
# the tag in every image's fingerprint, so renaming one orphans its downloads.
BROWSER_MODULES = {"chatgpt": "chatgpt", "meta": "meta_ai"}
BROWSER_PROVIDERS = tuple(BROWSER_MODULES)
BROWSER_OPTIONS = (
    "headless", "profile_dir", "min_delay", "max_delay",
    "composer_selector", "image_selector", "channel",
)
PROVIDERS = BROWSER_PROVIDERS + ("manual", "supplied")


def generate(
    scenes: list[dict[str, Any]],
    *,
    images_dir: Path,
    provider: str = "chatgpt",
    source_dir: str | Path | None = None,
    size: tuple[int, int] = (1920, 1080),
    force: bool = False,
    limit: int | None = None,
    browser: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Attach an `image` path to every scene."""
    images_dir = ensure_dir(images_dir)

    if provider == "supplied" or source_dir:
        return _assign_supplied(scenes, source_dir, images_dir, size)
    if provider == "manual":
        return _generate_manual(scenes, images_dir, size, force)
    if provider in BROWSER_PROVIDERS:
        return _generate_browser(
            scenes, provider, images_dir, size, force, limit=limit,
            options={key: value for key, value in (browser or {}).items()
                     if key in BROWSER_OPTIONS and value is not None},
        )
    raise VideoLyricsError(
        f"Unknown image provider {provider!r} (use {', '.join(PROVIDERS)})."
    )


def _valid(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:  # noqa: BLE001 - any unreadable file is simply invalid
        return False


def _normalise(path: Path, size: tuple[int, int]) -> Path:
    """Make the file something the render can use, touching it as little as possible.

    An image that is already RGB and already the target's aspect ratio is left
    exactly as the generator served it - format, bytes and all - and its own path
    comes back. Only a wrong shape or an unusable colour mode is worth a re-encode,
    and that result is written as PNG so a lossy source is never compressed twice.

    Resolution is deliberately not touched. The motion stage scales every image to
    `size * supersample` on its way into ffmpeg (motion.zoompan_filter), so
    upscaling here would interpolate twice and store the larger of the two for
    nothing.
    """
    width, height = size
    target_ratio = width / height
    with Image.open(path) as opened:
        source_ratio = opened.width / opened.height
        needs_crop = abs(source_ratio - target_ratio) > 0.01
        if opened.width < width * SOFT_WIDTH_RATIO:
            log.warning(
                "  %s is %dx%d, well under the %dx%d render - it will look soft.",
                path.name, opened.width, opened.height, width, height,
            )
        if not needs_crop and opened.mode == "RGB":
            return path
        image = opened.convert("RGB")
        if needs_crop:
            if source_ratio > target_ratio:  # too wide -> crop sides
                new_width = int(round(image.height * target_ratio))
                left = (image.width - new_width) // 2
                image = image.crop((left, 0, left + new_width, image.height))
            else:                            # too tall -> crop top/bottom
                new_height = int(round(image.width / target_ratio))
                top = (image.height - new_height) // 2
                image = image.crop((0, top, image.width, top + new_height))

    canonical = path.with_suffix(".png")
    image.save(canonical, "PNG")
    if canonical != path:
        path.unlink()
    return canonical


def _stem_for(scene: dict[str, Any], tag: str) -> str:
    return scene_stem(scene, tag)


def _find_image_by_stem(directory: Path, stem: str) -> Path | None:
    """Any of IMAGE_SUFFIXES counts - the format doesn't matter, only the stem."""
    for suffix in IMAGE_SUFFIXES:
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _clear_stem(directory: Path, stem: str, *, keep: Path | None = None) -> None:
    """Remove every file held under `stem` except `keep`, whatever format it is in.

    One folder means the same scene can end up in two formats at once - a fresh
    download beside the image it replaces - and only the first of them is ever
    reachable, since _find_image_by_stem picks by IMAGE_SUFFIXES order. So the stem
    is emptied before a scene is asked for again, and swept of everything but the
    winner after one is adopted.
    """
    for suffix in IMAGE_SUFFIXES:
        candidate = directory / f"{stem}{suffix}"
        if candidate != keep and candidate.is_file():
            candidate.unlink()


def _adopt_by_stem(
    scene: dict[str, Any],
    stem: str,
    *,
    images_dir: Path,
    size: tuple[int, int],
) -> bool:
    """If `images_dir` holds a readable file matching `stem`, make it this scene's
    image. Returns whether that succeeded - a missing or unreadable file both count
    as not adopted."""
    found = _find_image_by_stem(images_dir, stem)
    if found is None:
        return False
    if not _valid(found):
        log.warning("  scene %03d: %s is not a readable image, regenerating", scene["index"], found.name)
        return False
    final = _normalise(found, size)
    _clear_stem(images_dir, stem, keep=final)
    scene["image"] = str(final)
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
    picks the files up from disk - nothing to type back in.
    """
    pending: list[tuple[dict[str, Any], str]] = []
    for scene in scenes:
        stem = _stem_for(scene, "manual")
        if not force and _adopt_by_stem(scene, stem, images_dir=images_dir, size=size):
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


def _site_module(provider: str):
    """Import the driver for a browser provider only when it is actually used."""
    from importlib import import_module

    return import_module(f".{BROWSER_MODULES[provider]}", __package__)


def _generate_browser(
    scenes: list[dict[str, Any]],
    provider: str,
    images_dir: Path,
    size: tuple[int, int],
    force: bool,
    *,
    limit: int | None = None,
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    """Drive a chat site in a real browser to generate each outstanding scene's image.

    Downloads land in `images_dir/<stem>.<ext>`, in whatever format the site served,
    and stay there. A run interrupted partway through only asks the browser for what
    is still missing - anything already downloaded is reused.
    """
    pending: list[tuple[dict[str, Any], str]] = []
    for scene in scenes:
        stem = _stem_for(scene, provider)
        if not force and _adopt_by_stem(scene, stem, images_dir=images_dir, size=size):
            continue
        # Nothing downloaded under this provider's own stem, but the scene may
        # still have a perfectly good image from another one - a song generated
        # with codex before this replaced it, or images adopted by hand. Asking a
        # browser to redraw those would throw away work for no reason.
        if not force and _keep_existing(scene):
            continue
        pending.append((scene, stem))

    if not pending:
        log.info("All %d scene images already generated.", len(scenes))
        return scenes

    if limit is not None and limit < len(pending):
        log.info("Limiting this run to %d of %d missing image(s).", limit, len(pending))
        pending = pending[:limit]

    # Whatever is under these stems is about to be replaced - an unreadable file,
    # or (under --force) a perfectly good one being redrawn on purpose. Clearing it
    # first keeps a download in a new format from landing beside the old one.
    for _, stem in pending:
        _clear_stem(images_dir, stem)

    _site_module(provider).generate(
        [scene for scene, _ in pending], images_dir=images_dir, **options
    )

    missing = []
    for scene, stem in pending:
        if not _adopt_by_stem(scene, stem, images_dir=images_dir, size=size):
            missing.append(scene["index"])
    if missing:
        log.warning(
            "%s produced no usable image for scene(s): %s. Run the same command "
            "again to ask for just those - everything else is kept.", provider, missing
        )
    return scenes


def _keep_existing(scene: dict[str, Any]) -> bool:
    """Is this scene already pointing at a readable image on disk?"""
    existing = scene.get("image")
    return bool(existing) and Path(existing).is_file() and _valid(Path(existing))


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
        stem = f"scene-{scene['index']:03d}-{short_hash(chosen.name)}"
        if _find_image_by_stem(images_dir, stem) is None:
            shutil.copyfile(chosen, images_dir / f"{stem}{chosen.suffix.lower()}")
        _adopt_by_stem(scene, stem, images_dir=images_dir, size=size)
    return scenes
