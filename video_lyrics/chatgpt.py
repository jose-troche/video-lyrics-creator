"""Generate scene images by driving chatgpt.com in a real browser.

Only what is particular to chatgpt.com lives here; the browser itself, the login
done by hand, the one-prompt-at-a-time loop and the download are all in
`browser_ai.py` (read its module docstring first - it covers what this is and is
not). This replaces the old `codex` provider, which asked the Codex CLI's
image_gen tool for the same pictures: same account, one fewer thing to install.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import browser_ai
from .browser_ai import DEFAULT_MAX_DELAY, DEFAULT_MIN_DELAY

CHATGPT_URL = "https://chatgpt.com/"
DEFAULT_PROFILE_DIR = Path.home() / ".video-lyrics" / "chatgpt-profile"

# Scene prompts are written for an image generator, so ChatGPT is just as likely
# to *discuss* one as to draw it - describing the shot back, or asking which of
# two readings of the lyric is meant. This says, once, which of the two it is.
PROMPT_TEMPLATE = (
    "Generate exactly one image for the description below. Do not ask questions, "
    "do not describe it, do not reply with text - just the image.\n\n{prompt}"
)

# The composer: a contenteditable ProseMirror div, not a textarea - despite the
# id. Deliberately not falling back to `textarea`, tempting as that is: until the
# editor hydrates, chatgpt.com shows an inert <textarea name="prompt-textarea">
# in its place, and a fallback that matches it turns a loud "composer not found"
# into a silent one - the prompt typed into a decoy, Enter pressed on nothing,
# and a wait that only ends when the whole image timeout runs out. Overridable
# (image_generation.chatgpt_composer_selector) if the id ever changes.
DEFAULT_COMPOSER_SELECTOR = "#prompt-textarea, div[contenteditable='true']"
# The generated image. Deliberately as broad as `main` and no broader: it is
# tempting to say `[data-message-author-role='assistant'] img` and be precise,
# but an image reply is not laid out like a text reply - ChatGPT renders the
# picture in a container of its own, and pinning the selector to the message-role
# attribute means the finished image is never seen at all. Everything else in
# `main` is either smaller than MIN_IMAGE_SIDE or was already on screen before
# the prompt was sent, and each scene gets a fresh chat, so there is nothing else
# in there to confuse it with. Overridable
# (image_generation.chatgpt_image_selector).
DEFAULT_IMAGE_SELECTOR = "main img"

# What "finished" looks like here. ChatGPT streams the picture in: the same <img>
# is swapped through a series of increasingly sharp versions, each of them a
# complete, full-size, decoded image - so "an image appeared" is true long before
# the run is over. What is unambiguous is the Stop button, which is on screen for
# exactly as long as the turn is still being produced. Waiting for it to go, and
# only then for the src to hold still, is what says the last version is the one.
# (The finished image's own Download control would be the closer parallel to
# meta.ai, but on ChatGPT it only renders while the pointer is over the picture.)
BUSY_SELECTOR = "[data-testid='stop-button'], button[aria-label*='Stop' i]"

# chatgpt.com shows a working composer to signed-out visitors, so the composer
# alone cannot say whether we are logged in - these can.
LOGGED_OUT_SELECTOR = (
    "[data-testid='login-button'], [data-testid='signup-button'], "
    "a[href*='/auth/login'], a[href*='/auth/signup']"
)

# Image generation takes appreciably longer here than on meta.ai, and a busy
# account can queue.
IMAGE_TIMEOUT_MS = 420_000

SITE = browser_ai.Site(
    name="chatgpt",
    url=CHATGPT_URL,
    profile_dir=DEFAULT_PROFILE_DIR,
    composer_selector=DEFAULT_COMPOSER_SELECTOR,
    image_selector=DEFAULT_IMAGE_SELECTOR,
    busy_selector=BUSY_SELECTOR,
    logged_out_selector=LOGGED_OUT_SELECTOR,
    prompt_template=PROMPT_TEMPLATE,
    # The Google Chrome already on this machine, not Playwright's bundled
    # Chromium: signing in to ChatGPT often means signing in through Google, and
    # Google is markedly happier about a browser it recognises. Set
    # image_generation.chatgpt_channel to null to use the bundled one instead -
    # but then log in with that same browser (`video-lyrics browser-login`).
    channel="chrome",
    # Each scene gets its own chat. Asked for a second picture in a conversation
    # that already has one, ChatGPT tends to treat it as an edit of the first -
    # keeping the palette, the subject, sometimes most of the frame. Twenty
    # variations on scene one is not what a lyric video needs.
    new_chat_per_prompt=True,
    image_timeout_ms=IMAGE_TIMEOUT_MS,
    timeout_hint=(
        "Look at the browser window: ChatGPT may have answered in words instead "
        "of drawing (a refusal, or an image limit reached on this account)."
    ),
    selector_settings=("chatgpt_composer_selector", "chatgpt_image_selector"),
)


def generate(
    scenes: list[dict[str, Any]],
    *,
    images_dir: Path,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    composer_selector: str | None = None,
    image_selector: str | None = None,
    channel: str | None = None,
) -> None:
    """Ask chatgpt.com for one image per scene, saving each to `images_dir/<stem>.<ext>`."""
    browser_ai.generate(
        SITE,
        scenes,
        images_dir=images_dir,
        headless=headless,
        profile_dir=profile_dir,
        min_delay=min_delay,
        max_delay=max_delay,
        composer_selector=composer_selector,
        image_selector=image_selector,
        channel=channel,
    )
