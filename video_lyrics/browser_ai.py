"""Drive a consumer AI chat page in a real browser to generate one image per scene.

Two providers are built on this: meta.ai (`meta_ai.py`) and chatgpt.com
(`chatgpt.py`). Each of those modules is little more than a `Site` saying where
that page keeps its composer, what a generated image looks like, and how it
signals that it has stopped working. Everything that is the same either way -
the persistent browser profile, the login done by hand, submitting one prompt at
a time, deciding when an image is really finished, and downloading the bytes -
lives here.

This is unofficial automation of a consumer web page, not a documented API:
there is no guarantee either site's markup stays the way this module expects,
and driving them this way may not fit their terms of service - use at your own
judgement, on your own account, for your own personal project.

Login is never automated. A real, visible browser window opens (Playwright's
persistent context, so the session is remembered next time); if it is not already
signed in, this pauses and asks the user to log in by hand, then continue.
"""

from __future__ import annotations

import base64
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, ensure_dir, log, scene_stem

DEFAULT_MIN_DELAY = 1.0
DEFAULT_MAX_DELAY = 4.0
LOGIN_TIMEOUT_S = 600.0       # how long to wait once the user is told to log in
IMAGE_TIMEOUT_MS = 240_000    # how long one prompt is given to produce an image
COMPOSER_TIMEOUT_MS = 60_000
SUBMIT_ATTEMPTS = 3           # how many times to retype a prompt the page dropped
LOGGED_OUT_SETTLE_MS = 6_000  # how long a sign-in button has to appear before we
                              # accept that this is a signed-in page

# An avatar, a UI glyph and a loading placeholder are all `img` elements too. A
# generated image is the only one that is actually big, so anything whose intrinsic
# size is under this on either side is not the thing we are waiting for.
MIN_IMAGE_SIDE = 256

# Once a site says it has finished, one more confirmation that the src has
# settled, in case the signal lands a beat before the final swap.
STABLE_POLLS = 2
POLL_SECONDS = 1.0
PROGRESS_SECONDS = 20.0       # how often to say what the page is doing, while waiting

# A busy signal that never once fired is a selector that has stopped matching -
# and unlike the ready-count check, "nothing is busy" is what a renamed selector
# says too, so it would quietly hand back the first half-drawn frame. When that
# happens, fall back to the only evidence left: a src that has not moved in this
# many polls. Slower, and no longer exact - but it is a whole image.
UNCONFIRMED_STABLE_POLLS = 10

PLAYWRIGHT_HINT = (
    "Install it with `pip install -e '.[browser]'`, then run "
    "`playwright install chromium` once."
)

# Counting *visible* matches, not matches: React apps routinely keep a hidden
# duplicate of a control mounted (another breakpoint, a closed menu, a torn-down
# turn), and a hidden node must not be read as "the page is still working" or as
# "the finished image's controls are here".
_VISIBLE_COUNT_JS = """
    const visible = (el) => !!(el.offsetParent || el.getClientRects().length);
    const count = (sel) =>
        sel ? Array.from(document.querySelectorAll(sel)).filter(visible).length : 0;
"""


@dataclass(frozen=True)
class Site:
    """Everything that differs between one chat page and another.

    Selectors used from Python (`composer_selector`) go through Playwright, so
    they may use its `:visible` pseudo-class; the rest are evaluated with
    `document.querySelectorAll` in the page and must be plain CSS.
    """

    name: str                       # also the image fingerprint tag - do not rename
    url: str
    profile_dir: Path
    composer_selector: str
    image_selector: str
    # A finished image's own controls, e.g. a Download button: the wait ends once
    # *more* of these are on screen than there were before the prompt was sent.
    ready_selector: str | None = None
    # Present only while the page is working, e.g. a Stop button: the wait ends
    # once none are left. A site may set either signal, or both.
    busy_selector: str | None = None
    # If any of these are on screen, this is a signed-out page - even if a
    # composer is visible, which on some sites it is either way.
    logged_out_selector: str | None = None
    prompt_template: str = "{prompt}"
    # Start each prompt in a fresh chat. Worth it where a follow-up prompt would
    # otherwise be read as "edit the picture you just made".
    new_chat_per_prompt: bool = False
    image_timeout_ms: int = IMAGE_TIMEOUT_MS
    # Which browser to drive: a Playwright channel ("chrome" for the Google
    # Chrome already installed), or None for Playwright's bundled Chromium. It
    # has to be the same one `login` used - a profile belongs to its browser.
    channel: str | None = None
    # What to say when the wait runs out, beyond the generic advice.
    timeout_hint: str = ""
    # Names of the settings that override this site's selectors, for error text.
    selector_settings: tuple[str, str] = field(default=("composer_selector", "image_selector"))


def generate(
    site: Site,
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
    """Ask `site` for one image per scene, saving each to `raw_dir/<stem>.<ext>`.

    `scenes` should already be filtered down to the ones that actually need a
    fresh image - this always asks the browser, it never checks disk itself.
    """
    if not scenes:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise VideoLyricsError(
            f"The {site.name!r} image provider needs Playwright. {PLAYWRIGHT_HINT}"
        ) from exc

    raw_dir = ensure_dir(raw_dir)
    profile = _profile_path(site, profile_dir)
    composer_selector = composer_selector or site.composer_selector
    image_selector = image_selector or site.image_selector
    channel = channel or site.channel

    log.info(
        "%s: generating %d image(s) - a browser window will open (profile: %s).",
        site.name, len(scenes), profile,
    )
    with sync_playwright() as pw:
        context = _open_profile(pw, profile, headless=headless, channel=channel)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(site.url, wait_until="domcontentloaded")
            _ensure_logged_in(page, site, composer_selector)
            # Every image already on the page belongs to an earlier scene (or to the
            # UI); carrying the set forward across scenes is what stops a later
            # prompt from "finishing" instantly against an earlier scene's picture.
            seen = _image_sources(page, image_selector)
            for index, scene in enumerate(scenes):
                if index and site.new_chat_per_prompt:
                    page.goto(site.url, wait_until="domcontentloaded")
                    seen = _image_sources(page, image_selector)
                stem = scene_stem(scene, site.name)
                # This blocks until the new image is finished on screen AND written
                # to disk - the delay below only starts once that is done.
                src = _generate_one(
                    page, site, scene, stem, raw_dir, composer_selector, image_selector, seen
                )
                seen.add(src)
                if index + 1 < len(scenes):
                    delay = random.uniform(min_delay, max_delay)
                    log.info("  waiting %.1fs before the next prompt ...", delay)
                    time.sleep(delay)
        finally:
            context.close()


def _profile_path(site: Site, profile_dir: str | Path | None) -> Path:
    return ensure_dir(Path(profile_dir).expanduser() if profile_dir else site.profile_dir)


# Chrome keeps its cookie database encrypted, with a key it takes from the
# operating system's keychain. Playwright launches with these two flags, which
# replace that key with a fixed, publicly known one so its throwaway browsers
# never have to touch the real keychain.
#
# Either is workable. What is not workable is the two browsers disagreeing, and
# that is exactly what this program did: the user signed in through an ordinary
# Chrome (real keychain), and the driven Chrome then opened the same profile
# with Playwright's key, could not decrypt a single cookie, and quietly dropped
# every one of them. Measured on a signed-out visit: 11 cookies written, 0
# readable. Nothing errors, nothing is logged - the login simply appears not to
# have been saved.
PLAYWRIGHT_KEY_FLAGS = ("--use-mock-keychain", "--password-store=basic")


def _key_storage_args(channel: str | None) -> list[str]:
    """How the browser should protect the saved session, for both browsers alike.

    A browser the user installed (a `channel`) sits at a stable path that the
    macOS keychain already trusts, so it gets the real keychain and the session
    is properly encrypted at rest. Playwright's own bundled browser lives in a
    versioned directory that moves with every upgrade - asking the keychain about
    that earns a permission dialog in the middle of an unattended run, and
    another one after each upgrade - so it keeps Playwright's fixed key. Other
    platforms keep it too: on Linux the alternative is a keyring daemon that may
    not be running at all.
    """
    return [] if channel and sys.platform == "darwin" else list(PLAYWRIGHT_KEY_FLAGS)


def _open_profile(pw, profile: Path, *, headless: bool, channel: str | None):
    """Open a saved profile for driving. One place, so the login check and the
    generation run are looking at the browser in exactly the same state."""
    key_args = _key_storage_args(channel)
    return pw.chromium.launch_persistent_context(
        str(profile), headless=headless, channel=channel,
        viewport={"width": 1280, "height": 900},
        # Chrome announces itself as automated unless told not to, and pages do
        # read that. This hides nothing about who is signing in - it is the same
        # person, the same account, in a window they can see - it just stops the
        # browser volunteering "a robot opened me" about a session the user set
        # up by hand. The key flags go the same way when the real keychain is
        # wanted: they are defaults, so declining them means naming them.
        ignore_default_args=[
            "--enable-automation",
            *(flag for flag in PLAYWRIGHT_KEY_FLAGS if flag not in key_args),
        ],
        args=["--disable-blink-features=AutomationControlled", *key_args],
    )


def verify_login(
    site: Site, *, profile_dir: str | Path | None = None, channel: str | None = None
) -> bool:
    """Open the profile exactly as a generation run would, and report whether it
    is signed in.

    Worth the extra window. A browser only writes its session out when it shuts
    down cleanly, so a login can look completely successful and still leave
    nothing behind - and without this the news arrives much later, several
    pipeline stages in, as a login prompt nobody expected.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise VideoLyricsError(f"This needs Playwright. {PLAYWRIGHT_HINT}") from exc

    profile = _profile_path(site, profile_dir)
    with sync_playwright() as pw:
        context = _open_profile(
            pw, profile, headless=False, channel=channel or site.channel
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(site.url, wait_until="domcontentloaded")
            return _signed_in(page, site, site.composer_selector, timeout_ms=30_000)
        finally:
            context.close()


# Where each channel's browser lives, per platform. Playwright knows this too,
# but only tells you by launching one, and the whole point here is to start a
# browser it is not attached to.
CHANNEL_EXECUTABLES: dict[str, dict[str, tuple[str, ...]]] = {
    "chrome": {
        "darwin": ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",),
        "win32": (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ),
        "linux": ("google-chrome", "google-chrome-stable", "chromium"),
    },
    "msedge": {
        "darwin": ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",),
        "win32": (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",),
        "linux": ("microsoft-edge", "microsoft-edge-stable"),
    },
}


# Where `playwright install` puts its browsers, and what the executable is called
# once it is there. Read directly rather than asked for: Playwright will happily
# tell you `chromium.executable_path`, but only from inside a running driver, and
# starting one just to read a path leaves asyncio complaining about a destroyed
# task all over the user's terminal after the command has already succeeded.
BROWSERS_HOME = {
    "darwin": Path.home() / "Library" / "Caches" / "ms-playwright",
    "linux": Path.home() / ".cache" / "ms-playwright",
    "win32": Path.home() / "AppData" / "Local" / "ms-playwright",
}
BUNDLED_CHROMIUM_GLOBS = {
    "darwin": "chrome-mac*/*.app/Contents/MacOS/*",
    "linux": "chrome-linux*/chrome",
    "win32": "chrome-win*/chrome.exe",
}


def _bundled_chromium() -> str | None:
    """The newest `playwright install chromium` build on this machine, if any."""
    import os

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    home = Path(override) if override else BROWSERS_HOME.get(sys.platform)
    pattern = BUNDLED_CHROMIUM_GLOBS.get(sys.platform)
    if not home or not pattern or not home.is_dir():
        return None
    builds = sorted(
        (path for path in home.glob("chromium-*") if path.is_dir()),
        key=lambda path: int(part) if (part := path.name.rsplit("-", 1)[-1]).isdigit() else 0,
    )
    for build in reversed(builds):
        for found in sorted(build.glob(pattern)):
            if found.is_file():
                return str(found)
    return None


def _browser_executable(channel: str | None) -> str:
    """The browser binary to start by hand, for `channel`."""
    if not channel:
        found = _bundled_chromium()
        if found:
            return found
        raise VideoLyricsError(
            "Could not find Playwright's bundled browser. Run `playwright install "
            "chromium`, or sign in with the browser you already have: "
            "`video-lyrics browser-login --channel chrome`."
        )
    candidates = CHANNEL_EXECUTABLES.get(channel, {}).get(sys.platform, ())
    for candidate in candidates:
        found = candidate if Path(candidate).exists() else shutil.which(candidate)
        if found:
            return found
    raise VideoLyricsError(
        f"Could not find the {channel!r} browser on this machine"
        + (f" (looked in {', '.join(candidates)})" if candidates else "")
        + ". Install it, or set image_generation.<provider>_channel to null to "
        "use Playwright's own bundled browser."
    )


def login(site: Site, *, profile_dir: str | Path | None = None, channel: str | None = None) -> Path:
    """Open the site in an ordinary browser window and wait for the user to sign in.

    Not a Playwright window - an ordinary one, started as its own process with
    nothing driving it. That distinction is the whole reason this exists: sign in
    through Google inside an automated browser and Google stops you with "This
    browser or app may not be secure", which no selector or timeout can fix. A
    browser nobody is driving is not a workaround for that check, it is simply
    not the thing being checked - the user signs in themselves, by hand, exactly
    as they would any other day.

    What is left behind is the session, in the profile directory the image
    provider opens later. Log in once, generate for as long as it lasts.
    """
    profile = _profile_path(site, profile_dir)
    executable = _browser_executable(channel or site.channel)
    log.info(
        "Opening %s in a normal browser window (profile: %s).\n"
        "Log in there, and leave it on a working chat page.",
        site.url, profile,
    )
    process = subprocess.Popen(
        [
            executable,
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            # The session is being saved for another browser to read. Unless both
            # protect the cookie database the same way, it may as well not be.
            *_key_storage_args(channel or site.channel),
            site.url,
        ],
        # Chrome talks to its own stderr constantly (mojo, gcm, GPU); none of it
        # is this program's output, and all of it looks alarming in a terminal
        # that is otherwise showing a pipeline running.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Not `process.wait()`. On macOS a browser is an application, not a
        # window: close its last window and the process is still very much
        # running, so waiting for it to exit waits for something that is not
        # going to happen. Asking here, in the terminal, is unambiguous - and it
        # also covers the opposite case, where the browser handed the URL to an
        # instance that was already running and exited immediately.
        input("Press Enter here once you are logged in... ")
    finally:
        _close_browser(process)
    log.info("Browser closed. The session (if you signed in) is saved in %s.", profile)
    return profile


def _close_browser(process: "subprocess.Popen", timeout: float = 20.0) -> None:
    """Shut the browser down, and wait for it to finish doing so.

    Both halves matter. The session is only written out when the browser exits
    cleanly, and until the process is gone it still holds the profile's lock -
    the very next thing this program does is open that profile with Playwright,
    which fails outright while another browser has it.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("The browser did not close on its own; stopping it.")
        process.kill()
        process.wait(timeout=timeout)


def _composer(page, composer_selector: str):
    """The chat box, and never the decoy sitting behind it.

    `visible=true` is not decoration here. Both pages keep a hidden stand-in for
    the composer mounted - chatgpt.com an inert <textarea> behind its real
    ProseMirror editor, meta.ai a duplicate for another breakpoint - and it
    usually comes first in the DOM, so `.first` on its own binds to that instead.
    Nothing then reports an error: `fill()` simply waits out its whole timeout
    for an element the user can never see. Filtering the matches down to the
    visible ones before taking the first is what keeps that from happening,
    whatever a site's own selector happens to catch.
    """
    return page.locator(composer_selector).locator("visible=true").first


def _visible_count(page, selector: str | None) -> int:
    if not selector:
        return 0
    return page.evaluate(
        f"(selector) => {{ {_VISIBLE_COUNT_JS} return count(selector); }}", selector
    )


def _signed_in(page, site: Site, composer_selector: str, timeout_ms: int = 5_000) -> bool:
    """A usable composer and, where the site has one, no sign-in affordance.

    The second half matters: chatgpt.com shows a perfectly good composer to
    signed-out visitors, so "the chat box is here" alone would sail past the
    login screen and then fail much later, mid-prompt.

    And it has to be a *wait*, not a count. These pages paint the composer first
    and the surrounding chrome - the Log in and Sign up buttons among it - a good
    few seconds later, so a signed-out page really does look signed in for a
    moment. Waiting for a sign-in affordance to turn up, rather than asking
    whether one is there yet, is what stops that moment being believed.
    """
    try:
        _composer(page, composer_selector).wait_for(state="visible", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - any failure to find it means "not ready yet"
        return False
    if not site.logged_out_selector:
        return True
    try:
        page.wait_for_selector(
            site.logged_out_selector, state="visible", timeout=LOGGED_OUT_SETTLE_MS
        )
    except Exception:  # noqa: BLE001 - nothing offering a login appeared: we are in
        return True
    return False


def _ensure_logged_in(page, site: Site, composer_selector: str) -> None:
    if _signed_in(page, site, composer_selector):
        return
    log.info(
        "Not signed in to %s. Log in in the window that just opened.\n"
        "If the login page turns this window away - Google answers "
        "\"This browser or app may not be secure\" to a browser being driven by "
        "software - close it and sign in once in an ordinary window instead:\n"
        "    video-lyrics browser-login --provider %s",
        site.name, site.name,
    )
    input("Press Enter here once you are logged in and the chat box is visible... ")
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while time.monotonic() < deadline:
        if _signed_in(page, site, composer_selector):
            return
        time.sleep(POLL_SECONDS)
    raise VideoLyricsError(
        f"Still not signed in to {site.name} ({site.url}). Sign in once with "
        f"`video-lyrics browser-login --provider {site.name}` - that opens an "
        f"ordinary browser window, which login pages are happier with - then run "
        f"this again."
    )


def _page_state(page, site: Site, image_selector: str) -> dict[str, Any]:
    """Every candidate image, plus the site's own ready / busy signals.

    Intrinsic size (`naturalWidth`), not layout size: it tells a real generated
    picture apart from an avatar or a glyph however it happens to be displayed.
    """
    return page.evaluate(
        f"""([selector, readySelector, busySelector]) => {{
            {_VISIBLE_COUNT_JS}
            return {{
                images: Array.from(document.querySelectorAll(selector)).map((img) => ({{
                    src: img.currentSrc || img.src || '',
                    width: img.naturalWidth,
                    height: img.naturalHeight,
                    complete: img.complete,
                }})),
                ready: count(readySelector),
                busy: count(busySelector),
            }};
        }}""",
        [image_selector, site.ready_selector, site.busy_selector],
    )


def _image_sources(page, image_selector: str) -> set[str]:
    """Every image URL currently on the page, whatever its state."""
    return {
        info["src"]
        for info in page.evaluate(
            """(selector) => Array.from(document.querySelectorAll(selector))
                .map((img) => ({ src: img.currentSrc || img.src || '' }))""",
            image_selector,
        )
        if info["src"]
    }


def _finished(state: dict[str, Any], site: Site, ready_before: int) -> bool:
    """Has the page said, in whichever way it has, that it is done?

    Neither "a new image appeared" nor "it has decoded" is enough on its own:
    both sites show a preview at the final resolution while they are still
    working. Only the site's own signal separates the two.
    """
    if site.ready_selector and state["ready"] <= ready_before:
        return False
    if site.busy_selector and state["busy"]:
        return False
    return True


def _wait_for_new_image(
    page, site: Site, scene_index: int, image_selector: str, seen: set[str], ready_before: int
) -> str:
    """Block until this prompt's *finished* image is on screen, and return its URL."""
    started = time.monotonic()
    deadline = started + site.image_timeout_ms / 1000
    candidate: str | None = None
    stable = 0
    saw_busy = False
    state: dict[str, Any] = {"images": [], "ready": 0, "busy": 0}
    reported = 0.0
    while time.monotonic() < deadline:
        state = _page_state(page, site, image_selector)
        saw_busy = saw_busy or bool(state["busy"])
        # Waiting minutes in silence is indistinguishable from being hung, and
        # what the page is doing is exactly what someone watching would want.
        waited = time.monotonic() - started
        if waited - reported >= PROGRESS_SECONDS:
            reported = waited
            log.info("  scene %03d: %s", scene_index, _describe(state, site, ready_before))
        fresh = [
            info for info in state["images"]
            if info["src"]
            and info["src"] not in seen
            and info["complete"]
            and min(info["width"], info["height"]) >= MIN_IMAGE_SIDE
        ]
        newest = fresh[-1]["src"] if fresh else None
        if newest and _finished(state, site, ready_before):
            if newest == candidate:
                stable += 1
                trusted = saw_busy or not site.busy_selector
                if stable >= (STABLE_POLLS if trusted else UNCONFIRMED_STABLE_POLLS):
                    if not trusted:
                        log.warning(
                            "  %s never showed its busy signal (%s) - that selector "
                            "has probably been renamed. Falling back to waiting for "
                            "the picture to stop changing.",
                            site.name, site.busy_selector,
                        )
                    return newest
            else:
                candidate, stable = newest, 1
        else:
            candidate, stable = None, 0
        time.sleep(POLL_SECONDS)
    composer_setting, image_setting = site.selector_settings
    hint = f" {site.timeout_hint}" if site.timeout_hint else ""
    raise VideoLyricsError(
        f"{site.name} did not finish an image for scene {scene_index} within "
        f"{site.image_timeout_ms // 1000}s.{hint}\n"
        f"What the page looked like on the last check: "
        f"{_describe(state, site, ready_before)}\n"
        f"If the finished picture was on screen, the page's markup has moved on. "
        f"'no image matched' means image_generation.{image_setting} needs a CSS "
        f"selector that matches it (the current one is {image_selector!r}); "
        f"'still busy' with a finished picture means this site's busy selector "
        f"is matching something permanent (see {site.name}'s module). The "
        f"composer override is image_generation.{composer_setting}."
    )


def _describe(state: dict[str, Any], site: Site, ready_before: int) -> str:
    """One line saying what the page is showing, for progress and for failures."""
    images = state["images"]
    big = [
        info for info in images
        if info["complete"] and min(info["width"], info["height"]) >= MIN_IMAGE_SIDE
    ]
    parts = [
        f"{len(big)} image(s) big enough of {len(images)} matched"
        if images else "no image matched the image selector"
    ]
    if big:
        parts.append("largest " + "x".join(
            str(value) for value in max(
                ((info["width"], info["height"]) for info in big),
                key=lambda size: size[0] * size[1],
            )
        ))
    if site.busy_selector:
        parts.append("still busy" if state["busy"] else "not busy")
    if site.ready_selector:
        parts.append(f"{state['ready']} ready control(s), was {ready_before}")
    return "; ".join(parts)


def _download(page, src: str, scene_index: int) -> bytes:
    """Fetch the image bytes, whichever URL scheme the page handed back."""
    if src.startswith("data:"):
        return base64.b64decode(src.split(",", 1)[1])
    if src.startswith("blob:"):
        # A blob URL only means anything inside the page that made it.
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
    # Through the page's own request context, so its cookies come along - some of
    # these image URLs are only served to the signed-in session that made them.
    response = page.request.get(src)
    if not response.ok:
        raise VideoLyricsError(
            f"Could not download the image for scene {scene_index} ({response.status})."
        )
    return response.body()


def _suffix_for(payload: bytes) -> str:
    """Trust the bytes, not the content-type header the CDN sends."""
    if payload.startswith(b"\x89PNG"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    raise VideoLyricsError("The page returned something that is not a PNG, JPEG or WebP.")


def _composer_text(composer) -> str:
    """What the composer is actually holding - textarea or contenteditable."""
    return " ".join(
        composer.evaluate(
            "(el) => el.value !== undefined ? el.value : (el.innerText || '')"
        ).split()
    )


def _submit(page, composer_selector: str, prompt: str, scene_index: int) -> None:
    """Type the prompt in and send it, having checked that it went in at all.

    The check is the point. A React editor that is still mounting will take a
    `fill()` and then throw the text away when it swaps itself out - and Enter on
    an empty composer sends nothing at all, which looks exactly like a site that
    is taking its time to answer, right up until the image timeout runs out
    minutes later. Reading the text back turns that into one retry.
    """
    expected = " ".join(prompt.split())
    for attempt in range(SUBMIT_ATTEMPTS):
        # Re-located every scene, and every attempt: these pages re-render the
        # composer as the conversation grows, and a handle from the previous
        # scene can be detached by now.
        composer = _composer(page, composer_selector)
        composer.wait_for(state="visible", timeout=COMPOSER_TIMEOUT_MS)
        composer.fill(prompt)
        if _composer_text(composer) == expected:
            composer.press("Enter")
            return
        log.debug(
            "  scene %03d: the composer did not keep the prompt (attempt %d) - "
            "the page is probably still loading; retrying.", scene_index, attempt + 1,
        )
        time.sleep(POLL_SECONDS)
    raise VideoLyricsError(
        f"Could not type the prompt for scene {scene_index} into the page: the "
        f"composer keeps losing it. Its markup has probably changed - point "
        f"image_generation.<provider>_composer_selector at the real chat box."
    )


def _generate_one(
    page,
    site: Site,
    scene: dict[str, Any],
    stem: str,
    raw_dir: Path,
    composer_selector: str,
    image_selector: str,
    seen: set[str],
) -> str:
    """Submit one prompt and return the image URL, once it is written to disk."""
    log.info("  scene %03d: %s", scene["index"], scene["prompt"][:70])
    # Count the finished-image controls *before* asking for another one; the wait
    # below is looking for that count to rise, which is what says "this one is done".
    ready_before = _visible_count(page, site.ready_selector)
    _submit(
        page,
        composer_selector,
        site.prompt_template.format(prompt=scene["prompt"]),
        scene["index"],
    )

    started = time.monotonic()
    src = _wait_for_new_image(page, site, scene["index"], image_selector, seen, ready_before)
    log.info("  scene %03d: finished after %.0fs", scene["index"], time.monotonic() - started)
    payload = _download(page, src, scene["index"])
    target = raw_dir / f"{stem}{_suffix_for(payload)}"
    target.write_bytes(payload)
    log.info("  scene %03d: saved %s (%.0f KB)", scene["index"], target.name, len(payload) / 1024)
    return src
