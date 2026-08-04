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
IMAGE_TIMEOUT_MS = 90_000     # how long one prompt is given to produce an image

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
            composer = _ensure_logged_in(page, composer_selector)
            for index, scene in enumerate(scenes):
                stem = f"scene-{scene['index']:03d}-{short_hash(scene['prompt'], 'meta')}"
                # _generate_one blocks until this scene's image is fully downloaded to
                # disk - the delay below only starts once that is done, never before.
                _generate_one(page, composer, scene, stem, raw_dir, image_selector)
                if index + 1 < len(scenes):
                    delay = random.uniform(min_delay, max_delay)
                    log.info("  waiting %.0fs before the next prompt ...", delay)
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


def _generate_one(
    page, composer, scene: dict[str, Any], stem: str, raw_dir: Path, image_selector: str
) -> None:
    log.info("  scene %s: %s", stem, scene["prompt"][:70])
    composer.fill(scene["prompt"])
    composer.press("Enter")

    image = page.locator(image_selector).last
    image.wait_for(state="visible", timeout=IMAGE_TIMEOUT_MS)
    src = image.get_attribute("src")
    if not src:
        raise VideoLyricsError(f"meta.ai produced no image for scene {scene['index']}.")

    response = page.request.get(src)
    if not response.ok:
        raise VideoLyricsError(
            f"Could not download meta.ai's image for scene {scene['index']} "
            f"({response.status})."
        )
    content_type = response.headers.get("content-type", "")
    suffix = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
    (raw_dir / f"{stem}{suffix}").write_bytes(response.body())
