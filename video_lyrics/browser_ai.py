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

from .util import VideoLyricsError, ensure_dir, human_time, log, scene_stem

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


# --------------------------------------------------------- when one scene fails
#
# A site can fail to draw a scene for two very different reasons, and the right
# answer to each is the opposite of the other's. If it will not draw *this
# prompt*, the prompt is the only thing worth changing - so it is reworded and
# asked again. If the site itself is busy, rate limited or erroring, no wording
# helps and asking harder only makes it worse - so that scene is left for a later
# run, which asks for exactly what is still missing. Neither is a reason to throw
# away the scenes that have not been tried yet, which is what raising here used
# to do: one refusal in the middle of a song cost every image after it.


class ImageAttemptFailed(VideoLyricsError):
    """One scene's image did not arrive; the rest of the run may still be fine.

    `notice` is the page's own words, already trimmed to something loggable.
    """

    def __init__(self, message: str, notice: str = ""):
        super().__init__(message)
        self.notice = notice


class PromptRefused(ImageAttemptFailed):
    """The site answered in words that it will not draw this prompt."""


class SiteBusy(ImageAttemptFailed):
    """Capacity, a rate limit, a quota, or bytes that are not an image."""


# What the page says when *it* is the problem. Checked before the refusal
# patterns on purpose: "I can't generate images right now, please try again
# later" is a capacity notice wearing a refusal's clothes, and rewording the
# prompt would be answering the wrong question.
BUSY_PATTERNS = (
    "try again later", "try again in", "please try again", "come back later",
    "too many requests", "rate limit", "high demand", "at capacity",
    "overloaded", "servers are busy", "temporarily unavailable",
    "service unavailable", "reached your limit", "limit for", "quota",
    "something went wrong", "error generating",
    # Deliberately not "couldn't generate": that is how a refusal usually opens
    # ("I couldn't generate that image because it violates ..."), and reading one
    # as a capacity notice would skip a scene that a reworded prompt would have got.
)

# What the page says when the *prompt* is the problem.
REFUSAL_PATTERNS = (
    "can't create", "cannot create", "can't generate", "cannot generate",
    "can't make", "cannot make", "can't produce", "cannot produce",
    "can't help with", "cannot help with", "won't be able to",
    "unable to create", "unable to generate", "not able to create",
    "not able to generate", "content policy", "usage polic", "against our",
    "violates", "isn't allowed", "i won't create", "i won't generate",
)

# Slight rewordings, tried in order when a site says the prompt itself is the
# problem. Each keeps the scene's own description intact - the picture still has
# to match its lyric - and changes only how it is asked for: less literal, less
# graphic, nobody recognisable. Anything cleverer would mean a second model
# rewriting the prompt; this is enough for the refusals a lyric video actually
# runs into (a cross, a chain, blood, a named figure).
PROMPT_SOFTENERS = (
    "{prompt}",
    "{prompt}\n\nKeep it entirely non-graphic and suitable for all audiences: "
    "symbolic rather than literal, no injury or gore, no recognisable real people.",
    "A gentle, symbolic illustration evoking this idea - nothing literal, graphic "
    "or violent in frame, and no recognisable people: {prompt}",
)

# How many scenes in a row a site may turn away before the run gives up on it.
# Three consecutive capacity notices is not a run of bad luck, it is the account
# or the site being done for now.
MAX_BUSY_SKIPS = 3
BUSY_BACKOFF_S = 30.0

# ... and how many may fail with nothing on the page to explain it. One scene can
# be unlucky; twice in a row is the markup having moved, and every scene left
# would burn its whole timeout finding that out.
MAX_STALLED_SCENES = 2

# Where a page's own words appear: an answer in text, a capacity notice, an error
# toast. `main` plus the ARIA live regions covers both sites without either
# having to describe its own markup, and it is only ever read - never clicked,
# never matched for the picture itself.
DEFAULT_REPLY_SELECTOR = "main, [role='alert'], [role='status']"

# Counting *visible* matches, not matches: React apps routinely keep a hidden
# duplicate of a control mounted (another breakpoint, a closed menu, a torn-down
# turn), and a hidden node must not be read as "the page is still working" or as
# "the finished image's controls are here". `texts` reads the same way, and
# deliberately does not trim what it returns: the whole of a block is what makes
# it possible to subtract the part that was already there (see `_unseen_text`).
_PAGE_JS = """
    const visible = (el) => !!(el.offsetParent || el.getClientRects().length);
    const count = (sel) =>
        sel ? Array.from(document.querySelectorAll(sel)).filter(visible).length : 0;
    const texts = (sel) =>
        sel ? Array.from(document.querySelectorAll(sel)).filter(visible)
                  .map((el) => (el.innerText || '').trim()).filter(Boolean)
            : [];
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
    # Where this page's own words show up, so a refusal or a capacity notice can
    # be told apart from a picture that is simply still coming. Set to None to
    # read nothing, in which case every failure is just a timeout again.
    reply_selector: str | None = DEFAULT_REPLY_SELECTOR
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
    images_dir: Path,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    composer_selector: str | None = None,
    image_selector: str | None = None,
    channel: str | None = None,
) -> None:
    """Ask `site` for one image per scene, saving each to `images_dir/<stem>.<ext>`.

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

    images_dir = ensure_dir(images_dir)
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
            _run_scenes(
                page, site, scenes,
                images_dir=images_dir,
                composer_selector=composer_selector,
                image_selector=image_selector,
                min_delay=min_delay,
                max_delay=max_delay,
            )
        finally:
            context.close()


def _run_scenes(
    page,
    site: Site,
    scenes: list[dict[str, Any]],
    *,
    images_dir: Path,
    composer_selector: str,
    image_selector: str,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> list[int]:
    """One prompt at a time, on a page that is open and signed in.

    Returns the scenes left without an image. A scene that fails does not end the
    run: the images stage asks only for what is missing next time round, so the
    worst a skipped scene costs is another go at it later. What does end the run
    is evidence that the *next* scene would fail the same way - see MAX_BUSY_SKIPS
    and MAX_STALLED_SCENES.
    """
    # Every image already on the page belongs to an earlier scene (or to the
    # UI); carrying the set forward across scenes is what stops a later
    # prompt from "finishing" instantly against an earlier scene's picture.
    seen = _image_sources(page, image_selector)
    skipped: list[int] = []
    busy_runs = 0     # scenes the site has turned away, back to back
    stalled_runs = 0  # ... and ones that failed with no explanation at all
    for index, scene in enumerate(scenes):
        if index:
            # However the last scene ended: requests should not land in an
            # obvious, throttle-inviting pattern.
            delay = random.uniform(min_delay, max_delay)
            log.info("  waiting %.1fs before the next prompt ...", delay)
            time.sleep(delay)
            if site.new_chat_per_prompt:
                _new_chat(page, site, image_selector, seen)
        stem = scene_stem(scene, site.name)
        try:
            # This blocks until the new image is finished on screen AND written
            # to disk.
            src = _generate_one(
                page, site, scene, stem, images_dir, composer_selector, image_selector, seen,
            )
        except PromptRefused as refusal:
            # Never a reason to stop: the site is answering perfectly well, it
            # just will not draw this one. The next scene's prompt is a different
            # question entirely.
            log.warning("  scene %03d: skipped - %s", scene["index"], refusal)
            skipped.append(scene["index"])
            busy_runs = stalled_runs = 0
            continue
        except SiteBusy as busy:
            log.warning("  scene %03d: skipped - %s", scene["index"], busy)
            skipped.append(scene["index"])
            busy_runs += 1
            if busy_runs >= MAX_BUSY_SKIPS:
                log.warning(
                    "%s has turned away %d scenes in a row - it is not going to "
                    "serve this run. Stopping here; run the same command again "
                    "later and it asks only for what is still missing.",
                    site.name, busy_runs,
                )
                break
            if index + 1 < len(scenes):
                _cool_off(busy_runs)
            continue
        except VideoLyricsError as failure:
            # Undiagnosed: a timeout with nothing on the page to explain it, or a
            # composer that cannot be typed into. Let one scene be unlucky, but
            # not two - see MAX_STALLED_SCENES.
            stalled_runs += 1
            if stalled_runs >= MAX_STALLED_SCENES:
                raise
            log.warning("  scene %03d: skipped - %s", scene["index"], failure)
            skipped.append(scene["index"])
            continue
        seen.add(src)
        busy_runs = stalled_runs = 0

    if skipped:
        log.warning(
            "%s: %d scene(s) left without an image this run: %s",
            site.name, len(skipped), skipped,
        )
    return skipped


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
        f"(selector) => {{ {_PAGE_JS} return count(selector); }}", selector
    )


def _reply_texts(page, site: Site) -> tuple[str, ...]:
    """Every visible block of text on the page as it stands right now.

    Taken once just before a prompt is sent, so that whatever turns up afterwards
    can be read on its own - see `_unseen_text`.
    """
    if not site.reply_selector:
        return ()
    return tuple(
        page.evaluate(
            f"(selector) => {{ {_PAGE_JS} return texts(selector); }}", site.reply_selector
        )
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
        f"""([selector, readySelector, busySelector, replySelector]) => {{
            {_PAGE_JS}
            return {{
                images: Array.from(document.querySelectorAll(selector)).map((img) => ({{
                    src: img.currentSrc || img.src || '',
                    width: img.naturalWidth,
                    height: img.naturalHeight,
                    complete: img.complete,
                }})),
                ready: count(readySelector),
                busy: count(busySelector),
                replies: texts(replySelector),
            }};
        }}""",
        [image_selector, site.ready_selector, site.busy_selector, site.reply_selector],
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


def _new_chat(page, site: Site, image_selector: str, seen: set[str]) -> None:
    """Start the next prompt in an empty conversation.

    `seen` is added to rather than replaced: an image URL that was on the last
    page is no less stale for having gone off screen, and one that comes back
    must not be mistaken for this prompt's answer.
    """
    page.goto(site.url, wait_until="domcontentloaded")
    seen.update(_image_sources(page, image_selector))


def _cool_off(busy_runs: int) -> None:
    """Wait out a site that has just said it is too busy, a little longer each time."""
    delay = BUSY_BACKOFF_S * busy_runs
    log.info("  waiting %.0fs before asking for another scene ...", delay)
    time.sleep(delay)


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


def _unseen_text(replies, before) -> str:
    """The page's text with everything that was already on it taken out.

    Each earlier block is removed once, not everywhere: a site that refuses the
    same prompt twice in one conversation shows the identical sentence twice, and
    the second one is news. Removing rather than diffing by position also survives
    a toast appearing or a turn being torn down between the two readings.
    """
    text = "\n".join(replies)
    for earlier in before:
        if earlier:
            text = text.replace(earlier, " ", 1)
    return text


def _plain(text: str) -> str:
    """Lowercased, with the apostrophes these pages actually use folded to ASCII.

    Not a nicety: both sites typeset "I can't" with a curly apostrophe, so a
    pattern list typed on a keyboard matches not one word of a real refusal.
    """
    return text.lower().replace("’", "'").replace("ʼ", "'")


def _quote(text: str, pattern: str, limit: int = 200) -> str:
    """The line a match landed in, tidied down to something worth logging."""
    for line in text.splitlines():
        if pattern in _plain(line):
            line = " ".join(line.split())
            return line if len(line) <= limit else line[:limit] + "..."
    return ""


def _diagnose(text: str) -> tuple[str, str] | None:
    """Classify what the page has said, and quote the line that says it."""
    lowered = _plain(text)
    for kind, patterns in (("busy", BUSY_PATTERNS), ("refused", REFUSAL_PATTERNS)):
        for pattern in patterns:
            if pattern in lowered:
                return kind, _quote(text, pattern)
    return None


def _check_notice(state: dict[str, Any], site: Site, scene_index: int, said_before) -> None:
    """Raise if the page has answered this prompt in words instead of drawing it.

    Worth doing on every poll, not just when the wait runs out: a refusal lands in
    seconds, and the alternative is sitting in front of it for the whole image
    timeout - seven minutes, on ChatGPT - before anyone finds out.
    """
    found = _diagnose(_unseen_text(state.get("replies", ()), said_before))
    if found is None:
        return
    kind, quote = found
    if kind == "busy":
        raise SiteBusy(f'{site.name} is not drawing anything right now: "{quote}"', notice=quote)
    raise PromptRefused(f'{site.name} would not draw this one: "{quote}"', notice=quote)


def _wait_for_new_image(
    page,
    site: Site,
    scene_index: int,
    image_selector: str,
    seen: set[str],
    ready_before: int,
    said_before: tuple[str, ...] = (),
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
            # Nothing is on its way in, so whatever the page has said since the
            # prompt went in is worth reading. Only here: a page part-way through
            # handing over a picture is not refusing it, whatever else is on screen.
            _check_notice(state, site, scene_index, said_before)
        time.sleep(POLL_SECONDS)
    composer_setting, image_setting = site.selector_settings
    hint = f" {site.timeout_hint}" if site.timeout_hint else ""
    said = _unseen_text(state.get("replies", ()), said_before)
    raise VideoLyricsError(
        f"{site.name} did not finish an image for scene {scene_index} within "
        f"{site.image_timeout_ms // 1000}s.{hint}\n"
        f"What the page looked like on the last check: "
        f"{_describe(state, site, ready_before)}\n"
        + (f"What it said, which matched neither a refusal nor a capacity notice: "
           f"{' '.join(said.split())[:300]}\n" if said.strip() else "")
        +
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
    # SiteBusy, not a plain error: the picture was drawn and accepted, so nothing
    # about the prompt is in question - the CDN simply did not hand it over. That
    # is a later run's problem, not this whole run's.
    if not response.ok:
        raise SiteBusy(
            f"the image for scene {scene_index} would not download ({response.status})",
            notice=f"HTTP {response.status}",
        )
    return response.body()


def _suffix_for(payload: bytes) -> str:
    """Trust the bytes, not the content-type header the CDN sends.

    An error page served in an image's place is the same kind of failure as a
    refused download, and is skipped the same way (SiteBusy is a VideoLyricsError).
    """
    if payload.startswith(b"\x89PNG"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    raise SiteBusy("the page returned something that is not a PNG, JPEG or WebP")


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


def _scene_label(scene: dict[str, Any], width: int = 70) -> str:
    """What to call this scene in the log, from the point of view of someone
    watching a song being drawn.

    Its own lyric lines: every prompt opens with the same paragraph of style and
    framing instructions, so printing the prompt showed the part that is identical
    for every scene in the song and cut off before the part that is not. A scene
    covering an instrumental stretch has no lines of its own, and is named by where
    it falls instead - there can be several in a row.
    """
    lines = " / ".join(scene.get("lines") or ())
    if not lines:
        start, end = scene.get("start"), scene.get("end")
        if start is None or end is None:
            return "(instrumental)"
        return f"(instrumental, {human_time(start)}-{human_time(end)})"
    return lines if len(lines) <= width else f"{lines[:width - 1].rstrip()}…"


def _generate_one(
    page,
    site: Site,
    scene: dict[str, Any],
    stem: str,
    images_dir: Path,
    composer_selector: str,
    image_selector: str,
    seen: set[str],
) -> str:
    """Submit one prompt and return the image URL, once it is written to disk.

    A prompt the site turns down is reworded and asked again (PROMPT_SOFTENERS);
    run out of wordings and this raises PromptRefused, for the caller to skip.
    """
    log.info("  scene %03d: %s", scene["index"], _scene_label(scene))
    refusal: PromptRefused | None = None
    for attempt, softener in enumerate(PROMPT_SOFTENERS):
        if attempt:
            log.warning(
                "  scene %03d: %s Rewording the prompt (%d of %d) and asking again.",
                scene["index"], refusal, attempt + 1, len(PROMPT_SOFTENERS),
            )
            if site.new_chat_per_prompt:
                # A site that has just said no says it again when asked in the same
                # conversation: it is answering its own last turn as much as the
                # new prompt.
                _new_chat(page, site, image_selector, seen)
        # Count the finished-image controls, and note what the page already says,
        # *before* asking for another one; the wait below is looking for that count
        # to rise - which is what says "this one is done" - and for words that were
        # not there a moment ago.
        ready_before = _visible_count(page, site.ready_selector)
        said_before = _reply_texts(page, site)
        _submit(
            page,
            composer_selector,
            # Only the template and the softener are formatted, never the scene's
            # own prompt - a stray brace in it would otherwise blow up here.
            site.prompt_template.format(prompt=softener.format(prompt=scene["prompt"])),
            scene["index"],
        )

        started = time.monotonic()
        try:
            src = _wait_for_new_image(
                page, site, scene["index"], image_selector, seen, ready_before, said_before
            )
        except PromptRefused as exc:
            refusal = exc
            continue
        log.info("  scene %03d: finished after %.0fs", scene["index"], time.monotonic() - started)
        payload = _download(page, src, scene["index"])
        target = images_dir / f"{stem}{_suffix_for(payload)}"
        target.write_bytes(payload)
        log.info("  scene %03d: saved %s (%.0f KB)", scene["index"], target.name, len(payload) / 1024)
        return src

    notice = refusal.notice if refusal else ""
    raise PromptRefused(
        f'{site.name} turned down all {len(PROMPT_SOFTENERS)} wordings of this '
        f'prompt ("{notice}"). Rewrite the scene\'s `prompt:` in the project file '
        f"and run `video-lyrics images` again - it asks only for the images that "
        f"are still missing.",
        notice=notice,
    )
