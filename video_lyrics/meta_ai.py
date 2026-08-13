"""Generate scene images by driving meta.ai (https://www.meta.ai/) in a real browser.

Only what is particular to meta.ai lives here; the browser itself, the login done
by hand, the one-prompt-at-a-time loop and the download are all in `browser_ai.py`
(read its module docstring first - it covers what this is and is not).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import browser_ai
from .browser_ai import DEFAULT_MAX_DELAY, DEFAULT_MIN_DELAY

META_URL = "https://www.meta.ai/"
DEFAULT_PROFILE_DIR = Path.home() / ".video-lyrics" / "meta-ai-profile"

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

SITE = browser_ai.Site(
    name="meta",
    url=META_URL,
    profile_dir=DEFAULT_PROFILE_DIR,
    composer_selector=DEFAULT_COMPOSER_SELECTOR,
    image_selector=DEFAULT_IMAGE_SELECTOR,
    ready_selector=READY_SELECTOR,
    # meta.ai answers a bare description with a picture, and only shows its
    # composer to signed-in visitors - so there is nothing to add to the prompt
    # and no separate signed-out check to make.
    timeout_hint="Its controls may no longer match READY_SELECTOR in meta_ai.py.",
    selector_settings=("meta_composer_selector", "meta_image_selector"),
)


def generate(
    scenes: list[dict[str, Any]],
    *,
    raw_dir: Path,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    composer_selector: str | None = None,
    image_selector: str | None = None,
    channel: str | None = None,
) -> None:
    """Ask meta.ai for one image per scene, saving each to `raw_dir/<stem>.<ext>`."""
    browser_ai.generate(
        SITE,
        scenes,
        raw_dir=raw_dir,
        headless=headless,
        profile_dir=profile_dir,
        min_delay=min_delay,
        max_delay=max_delay,
        composer_selector=composer_selector,
        image_selector=image_selector,
        channel=channel,
    )
