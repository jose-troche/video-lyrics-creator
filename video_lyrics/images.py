"""Produce one still image per scene.

Four providers:
  * ``chatgpt``  - drives chatgpt.com in a real browser (see chatgpt.py).
  * ``meta``     - drives meta.ai the same way (see meta_ai.py).
  * ``supplied`` - images the user already has, taken in filename order.
  * ``manual``   - no generator at all: write every scene's prompt and expected
                   filename to a manifest so the user can create each image by hand
                   (Midjourney, an image site, ...) and drop it into the images folder.

Both browser providers keep their raw downloads in `images.src/` (a sibling of
`images/`) and convert each into the canonical PNG in `images/`, same as `manual`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .util import VideoLyricsError, ensure_dir, log, scene_stem, short_hash

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
RAW_SUBDIR = "images.src"

# provider -> the module that drives that site. Each exposes a `generate` taking
# `raw_dir` plus BROWSER_OPTIONS as keyword arguments; the provider name is also
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
    raw_dir: Path | None = None,
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
            scenes, provider, images_dir, raw_dir, size, force, limit=limit,
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
    return scene_stem(scene, tag)


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


def _site_module(provider: str):
    """Import the driver for a browser provider only when it is actually used."""
    from importlib import import_module

    return import_module(f".{BROWSER_MODULES[provider]}", __package__)


def _generate_browser(
    scenes: list[dict[str, Any]],
    provider: str,
    images_dir: Path,
    raw_dir: Path | None,
    size: tuple[int, int],
    force: bool,
    *,
    limit: int | None = None,
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    """Drive a chat site in a real browser to generate each outstanding scene's image.

    Raw downloads land in `raw_dir/<stem>.<ext>` and are kept there (whatever
    format the site served); each is also converted into the canonical PNG in
    `images_dir`. A run interrupted partway through only asks the browser for what
    is still missing - anything already downloaded is reused.
    """
    raw_dir = ensure_dir(raw_dir if raw_dir is not None else images_dir.parent / RAW_SUBDIR)

    pending: list[tuple[dict[str, Any], str]] = []
    for scene in scenes:
        stem = _stem_for(scene, provider)
        if not force and _adopt_by_stem(
            scene, stem, search_dir=raw_dir, images_dir=images_dir,
            size=size, delete_source=False,
        ):
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

    _site_module(provider).generate(
        [scene for scene, _ in pending], raw_dir=raw_dir, **options
    )

    missing = []
    for scene, stem in pending:
        if not _adopt_by_stem(
            scene, stem, search_dir=raw_dir, images_dir=images_dir,
            size=size, delete_source=False,
        ):
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
        target = images_dir / f"scene-{scene['index']:03d}-{short_hash(chosen.name)}.png"
        if not target.is_file():
            with Image.open(chosen) as image:
                image.convert("RGB").save(target, "PNG")
            _postprocess(target, size)
        scene["image"] = str(target)
    return scenes
