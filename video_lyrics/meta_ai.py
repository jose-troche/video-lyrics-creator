"""Generate scene images by driving meta.ai (https://www.meta.ai/) in a real browser.

This is unofficial automation of a consumer web page, not a documented API: there is
no guarantee meta.ai's markup stays the way this module expects, and driving it this
way may not fit meta.ai's terms of service - use at your own judgement, on your own
account, for your own personal project.

Login is never automated. A real, visible browser window opens (Playwright's
persistent context, so the session is remembered next time); if it is not already
signed in, this pauses and asks the user to log in by hand, then continue.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, ensure_dir, log, short_hash

META_URL = "https://www.meta.ai/"
DEFAULT_PROFILE_DIR = Path.home() / ".video-lyrics" / "meta-ai-profile"
DEFAULT_MIN_DELAY = 1.0
DEFAULT_MAX_DELAY = 4.0
LOGIN_TIMEOUT_MS = 600_000    # how long to wait once the user is told to log in
IMAGE_TIMEOUT_MS = 240_000    # how long one prompt is given to produce an image

# An avatar, a UI glyph and a loading placeholder are all `img` elements too. A
# generated image is the only one that is actually big, so anything whose intrinsic
# size is under this on either side is not the thing we are waiting for.
MIN_IMAGE_SIDE = 256

# What "finished" actually looks like, and why nothing simpler works.
#
# While it is still generating, meta.ai already shows a preview at the *final*
# resolution: a new <img>, 2048x1152, `complete` - and it sits there unchanged for
# the best part of ten seconds before being swapped for the real thing. So neither
# "a new image appeared", nor "it has decoded", nor "its src stopped changing"
# separates the preview from the finished picture; all three are true of both.
#
# What does change, at the exact moment the final src is swapped in, is that the
# finished image gains its own controls: a Download affordance and an image tile.
# Counting those before submitting and waiting for the count to go *up* is the
# signal used here - one that means "this image is ready", not "pixels exist".
READY_SELECTOR = (
    "[data-testid='ur-image-tile'], "
    "[aria-label*='Download' i], [aria-label*='Save image' i]"
)
# Once ready fires, one more confirmation that the src has settled, in case the
# controls appear a beat before the swap.
STABLE_POLLS = 2
POLL_SECONDS = 1.0

# The prompt composer. `get_by_role("textbox")` alone is too broad - meta.ai also
# exposes a one-line "Conversation title" <input> with that same accessible role,
# and being first in the DOM it can win over the actual composer. Textarea /
# contenteditable narrows it down to the real multi-line chat box - but meta.ai's
# React app can keep more than one such element mounted at once (e.g. a hidden
# duplicate for another breakpoint/layout), so `:visible` is needed too: without
# it, `.first` can end up pointing at a hidden match instead of the one actually
# on screen, and every fill() then times out waiting for it to become visible.
# Overridable (image_generation.meta_composer_selector) in case meta.ai's markup
# has moved on again by the time you read this.
DEFAULT_COMPOSER_SELECTOR = "textarea:visible, [contenteditable='true']:visible"
# The generated image. Overridable (image_generation.meta_image_selector).
DEFAULT_IMAGE_SELECTOR = "main img"


def generate(
    scenes: list[dict[str, Any]],
    *,
    raw_dir: Path,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    composer_selector: str = DEFAULT_COMPOSER_SELECTOR,
    image_selector: str = DEFAULT_IMAGE_SELECTOR,
) -> None:
    """Ask meta.ai for one image per scene, saving each to `raw_dir/<stem>.<ext>`.

    `scenes` should already be filtered down to the ones that actually need a
    fresh image - this always asks the browser, it never checks disk itself.
    """
    if not scenes:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise VideoLyricsError(
            "The 'meta' image provider needs Playwright. Install it with "
            "`pip install -e '.[meta]'`, then run `playwright install chromium` once."
        ) from exc

    raw_dir = ensure_dir(raw_dir)
    profile = ensure_dir(Path(profile_dir).expanduser() if profile_dir else DEFAULT_PROFILE_DIR)

    log.info(
        "meta.ai: generating %d image(s) - a browser window will open "
        "(profile: %s).", len(scenes), profile,
    )
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile), headless=headless, viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(META_URL, wait_until="domcontentloaded")
            _ensure_logged_in(page, composer_selector)
            # Every image already on the page belongs to an earlier scene (or to the
            # UI); carrying the set forward across scenes is what stops a later
            # prompt from "finishing" instantly against an earlier scene's picture.
            seen = _image_sources(page, image_selector)
            for index, scene in enumerate(scenes):
                stem = f"scene-{scene['index']:03d}-{short_hash(scene['prompt'], 'meta')}"
                # This blocks until the new image is finished on screen AND written
                # to disk - the delay below only starts once that is done.
                src = _generate_one(
                    page, scene, stem, raw_dir, composer_selector, image_selector, seen
                )
                seen.add(src)
                if index + 1 < len(scenes):
                    delay = random.uniform(min_delay, max_delay)
                    log.info("  waiting %.1fs before the next prompt ...", delay)
                    time.sleep(delay)
        finally:
            context.close()


def _composer(page, composer_selector: str):
    return page.locator(composer_selector).first


def _ensure_logged_in(page, composer_selector: str):
    composer = _composer(page, composer_selector)
    try:
        composer.wait_for(timeout=5_000)
        return composer
    except Exception:  # noqa: BLE001 - any failure to find it means "not ready yet"
        pass
    log.info("Log into meta.ai in the window that just opened.")
    input("Press Enter here once you are logged in and the chat box is visible... ")
    composer.wait_for(timeout=LOGIN_TIMEOUT_MS)
    return composer


def _image_sources(page, image_selector: str = DEFAULT_IMAGE_SELECTOR) -> set[str]:
    """Every image URL currently on the page, whatever its state."""
    return {info["src"] for info in _image_info(page, image_selector) if info["src"]}


def _page_state(page, image_selector: str) -> dict[str, Any]:
    """Every candidate image, plus how many finished-image controls are on screen.

    Intrinsic size (`naturalWidth`), not layout size: it tells a real generated
    picture apart from an avatar or a glyph however it happens to be displayed.
    """
    return page.evaluate(
        """([selector, readySelector]) => ({
            images: Array.from(document.querySelectorAll(selector)).map((img) => ({
                src: img.currentSrc || img.src || '',
                width: img.naturalWidth,
                height: img.naturalHeight,
                complete: img.complete,
            })),
            ready: document.querySelectorAll(readySelector).length,
        })""",
        [image_selector, READY_SELECTOR],
    )


def _image_info(page, image_selector: str) -> list[dict[str, Any]]:
    return _page_state(page, image_selector)["images"]


def _wait_for_new_image(
    page, scene_index: int, image_selector: str, seen: set[str], ready_before: int
) -> str:
    """Block until this prompt's *finished* image is on screen, and return its URL.

    `ready_before` is how many finished-image controls existed before the prompt
    was submitted; this returns only once that count has gone up, which is what
    rules out the identically-sized, already-decoded preview meta.ai shows first.
    """
    deadline = time.monotonic() + IMAGE_TIMEOUT_MS / 1000
    candidate: str | None = None
    stable = 0
    while time.monotonic() < deadline:
        state = _page_state(page, image_selector)
        fresh = [
            info for info in state["images"]
            if info["src"]
            and info["src"] not in seen
            and info["complete"]
            and min(info["width"], info["height"]) >= MIN_IMAGE_SIDE
        ]
        newest = fresh[-1]["src"] if fresh else None
        if newest and state["ready"] > ready_before:
            if newest == candidate:
                stable += 1
                if stable >= STABLE_POLLS:
                    return newest
            else:
                candidate, stable = newest, 1
        else:
            candidate, stable = None, 0
        time.sleep(POLL_SECONDS)
    raise VideoLyricsError(
        f"meta.ai did not finish an image for scene {scene_index} within "
        f"{IMAGE_TIMEOUT_MS // 1000}s. If the finished picture is on screen, its "
        f"controls no longer match READY_SELECTOR in meta_ai.py, or "
        f"image_generation.meta_image_selector needs to match the image itself."
    )


def _ready_count(page) -> int:
    return page.evaluate(
        "(selector) => document.querySelectorAll(selector).length", READY_SELECTOR
    )


def _download(page, src: str, scene_index: int) -> bytes:
    """Fetch the image bytes, whichever URL scheme meta.ai handed back."""
    if src.startswith("data:"):
        import base64
        return base64.b64decode(src.split(",", 1)[1])
    if src.startswith("blob:"):
        # A blob URL only means anything inside the page that made it.
        import base64
        encoded = page.evaluate(
            """async (url) => {
                const buffer = await (await fetch(url)).arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = '';
                for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                return btoa(binary);
            }""",
            src,
        )
        return base64.b64decode(encoded)
    response = page.request.get(src)
    if not response.ok:
        raise VideoLyricsError(
            f"Could not download the image for scene {scene_index} ({response.status})."
        )
    return response.body()


def _suffix_for(payload: bytes) -> str:
    """Trust the bytes, not the content-type header meta.ai's CDN sends."""
    if payload.startswith(b"\x89PNG"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    raise VideoLyricsError("meta.ai returned something that is not a PNG, JPEG or WebP.")


def _generate_one(
    page,
    scene: dict[str, Any],
    stem: str,
    raw_dir: Path,
    composer_selector: str,
    image_selector: str,
    seen: set[str],
) -> str:
    """Submit one prompt and return the image URL, once it is written to disk."""
    log.info("  scene %03d: %s", scene["index"], scene["prompt"][:70])
    # Re-located every scene: meta.ai re-renders the composer as the conversation
    # grows, and a handle from the previous scene can be detached by now.
    composer = _composer(page, composer_selector)
    composer.wait_for(state="visible", timeout=60_000)
    # Count the finished-image controls *before* asking for another one; the wait
    # below is looking for that count to rise, which is what says "this one is done".
    ready_before = _ready_count(page)
    composer.fill(scene["prompt"])
    composer.press("Enter")

    started = time.monotonic()
    src = _wait_for_new_image(page, scene["index"], image_selector, seen, ready_before)
    log.info("  scene %03d: finished after %.0fs", scene["index"], time.monotonic() - started)
    payload = _download(page, src, scene["index"])
    target = raw_dir / f"{stem}{_suffix_for(payload)}"
    target.write_bytes(payload)
    log.info("  scene %03d: saved %s (%.0f KB)", scene["index"], target.name, len(payload) / 1024)
    return src
