"""Deciding when a generated image is actually finished.

Both sites show something that looks like the answer well before it is one:
meta.ai a full-resolution preview, chatgpt.com a series of increasingly sharp
versions of the same picture. Every one of those is a new, decoded, full-size
image, so the only thing separating them from the real thing is the site's own
signal - a finished image's controls appearing (meta) or the Stop button going
away (chatgpt). These tests drive that decision with a fake page, no browser.
"""

from __future__ import annotations

import dataclasses
import importlib
from pathlib import Path

import pytest

from video_lyrics import browser_ai, chatgpt, images, meta_ai
from video_lyrics.util import VideoLyricsError, scene_stem


class FakePage:
    """Replays one `_page_state` result per poll, holding on the last one."""

    def __init__(self, states):
        self.states = list(states)
        self.polls = 0

    def evaluate(self, script, arg=None):
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return state


def state(*srcs, ready=0, busy=0, width=1024, height=576, complete=True):
    return {
        "images": [
            {"src": src, "width": width, "height": height, "complete": complete}
            for src in srcs
        ],
        "ready": ready,
        "busy": busy,
    }


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(browser_ai, "POLL_SECONDS", 0)


def wait(page, site, seen=(), ready_before=0):
    return browser_ai._wait_for_new_image(
        page, site, 1, site.image_selector, set(seen), ready_before
    )


# ------------------------------------------------------- meta.ai: ready rises

def test_a_full_size_preview_is_not_mistaken_for_the_finished_image():
    page = FakePage([
        state("preview.jpg", ready=0),   # the preview: same size, already decoded
        state("preview.jpg", ready=0),
        state("final.jpg", ready=1),     # controls appear as the real src lands
        state("final.jpg", ready=1),
    ])
    assert wait(page, meta_ai.SITE) == "final.jpg"


def test_an_image_left_over_from_an_earlier_scene_is_ignored():
    page = FakePage([
        state("scene-one.jpg", ready=1),          # already on screen, already ready
        state("scene-one.jpg", "new.jpg", ready=2),
        state("scene-one.jpg", "new.jpg", ready=2),
    ])
    assert wait(page, meta_ai.SITE, seen=["scene-one.jpg"], ready_before=1) == "new.jpg"


def test_a_glyph_or_avatar_is_too_small_to_be_the_generated_image():
    small = state("icon.png", ready=1, width=64, height=64)
    page = FakePage([small, small, state("icon.png", "real.jpg", ready=2), state("icon.png", "real.jpg", ready=2)])
    # the 64px image never qualifies, so only the big one can end the wait
    assert wait(page, meta_ai.SITE) == "real.jpg"


# ------------------------------------------------ chatgpt.com: busy goes away

def test_a_streaming_image_is_not_taken_until_the_stop_button_goes():
    page = FakePage([
        state("blurry.png", busy=1),   # ChatGPT streams the picture in, sharpening
        state("sharper.png", busy=1),
        state("final.png", busy=0),
        state("final.png", busy=0),
    ])
    assert wait(page, chatgpt.SITE) == "final.png"


def test_the_src_must_settle_even_after_the_page_says_it_is_done():
    page = FakePage([
        state("nearly.png", busy=1),
        state("nearly.png", busy=0),   # signal first, final swap a beat later
        state("final.png", busy=0),
        state("final.png", busy=0),
    ])
    assert wait(page, chatgpt.SITE) == "final.png"


def test_a_busy_signal_that_never_fires_is_not_trusted():
    """If the Stop button is never seen, its selector has been renamed - and a
    renamed selector reads exactly like "finished" from the first frame on. The
    fallback waits for the picture itself to stop changing instead."""
    page = FakePage(
        [state("half-drawn.png", busy=0)] * 3 + [state("final.png", busy=0)]
    )
    assert wait(page, chatgpt.SITE) == "final.png"
    assert page.polls > browser_ai.STABLE_POLLS + 1


# ------------------------------------------------------------------ timeouts

def test_running_out_of_time_names_the_setting_that_overrides_the_selector():
    site = dataclasses.replace(chatgpt.SITE, image_timeout_ms=1)
    page = FakePage([state("still-working.png", busy=1)])
    with pytest.raises(VideoLyricsError) as error:
        wait(page, site)
    assert "chatgpt_image_selector" in str(error.value)
    assert site.timeout_hint in str(error.value)


# ------------------------------------------------------- naming a scene in the log

def test_a_scene_is_named_by_its_own_lyric_lines():
    """Not the prompt: every prompt in a song opens with the same paragraph of
    style and framing, so the first 70 characters of one are the first 70 of them
    all."""
    scene = {"index": 3, "prompt": "cinematic photographic realism. Create a ...",
             "lines": ["Praise the Lord with sounding anthem", "Bend your knees"]}
    assert browser_ai._scene_label(scene) == (
        "Praise the Lord with sounding anthem / Bend your knees"
    )


def test_a_long_line_is_cut_short_at_a_readable_width():
    scene = {"index": 3, "lines": ["a line that runs on " * 10]}
    described = browser_ai._scene_label(scene, width=30)
    assert len(described) == 30
    assert described.endswith("…")


def test_an_instrumental_scene_is_named_by_where_it_falls():
    """It has no lines of its own, and a song can hold several in a row - so the
    one thing that tells them apart is when they are."""
    first = {"index": 1, "lines": [], "start": 0.0, "end": 10.505}
    second = {"index": 2, "lines": [], "start": 10.505, "end": 21.01}
    assert browser_ai._scene_label(first) == "(instrumental, 0:00.00-0:10.51)"
    assert browser_ai._scene_label(second) == "(instrumental, 0:10.51-0:21.01)"
    assert browser_ai._scene_label(first) != browser_ai._scene_label(second)


def test_a_scene_with_neither_lines_nor_timing_still_says_something():
    assert browser_ai._scene_label({"index": 1}) == "(instrumental)"


# ------------------------------------------------------------------- signing in

class FakeLoginPage:
    """A page where the composer is always there and the sign-in buttons may be.

    Modelled on what chatgpt.com actually does: it hands a signed-out visitor a
    working composer, and paints the Log in button several seconds later.
    """

    def __init__(self, *, signed_out: bool):
        self.signed_out = signed_out

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        return None                       # the composer is always visible

    def wait_for_selector(self, selector, state=None, timeout=None):
        if self.signed_out:
            return object()               # a Log in button turned up
        raise TimeoutError("no sign-in affordance appeared")


def test_a_composer_alone_does_not_mean_signed_in():
    site = chatgpt.SITE
    assert not browser_ai._signed_in(
        FakeLoginPage(signed_out=True), site, site.composer_selector
    )
    assert browser_ai._signed_in(
        FakeLoginPage(signed_out=False), site, site.composer_selector
    )


def test_a_site_with_no_signed_out_marker_trusts_its_composer():
    """meta.ai only shows one to members, so there is nothing else to check."""
    assert meta_ai.SITE.logged_out_selector is None
    assert browser_ai._signed_in(
        FakeLoginPage(signed_out=True), meta_ai.SITE, meta_ai.SITE.composer_selector
    )


# ----------------------------------------------------------- signing in once

class FakeBrowserProcess:
    """A browser that, like a real one on macOS, outlives its last window."""

    def __init__(self, argv, alive=True):
        self.argv = argv
        self.alive = alive
        self.terminated = False
        self.waited = False

    @classmethod
    def spawning(cls, record, alive=True):
        def spawn(argv, **kwargs):
            process = cls(argv, alive=alive)
            record.update(argv=argv, process=process)
            return process
        return spawn

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        self.waited = True
        return 0

def test_login_opens_a_browser_that_nothing_is_driving(tmp_path, monkeypatch):
    """The one-time login is a plain browser process, not a Playwright one.

    Sign in to Google inside an automated browser and Google refuses with "This
    browser or app may not be secure" - so this starts an ordinary window and
    waits for the user to close it. No automation switches, no debugging port:
    there is nothing attached to it at all.
    """
    launched = {}
    monkeypatch.setattr(browser_ai, "_browser_executable", lambda channel: "/bin/browser")
    monkeypatch.setattr(browser_ai.subprocess, "Popen", FakeBrowserProcess.spawning(launched))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    profile = browser_ai.login(chatgpt.SITE, profile_dir=tmp_path / "profile")

    argv = launched["argv"]
    assert argv[0] == "/bin/browser"
    assert f"--user-data-dir={profile}" in argv
    assert argv[-1] == chatgpt.SITE.url
    assert profile.is_dir()
    assert not any("automation" in arg or "remote-debugging" in arg for arg in argv)


def test_login_waits_for_the_terminal_not_for_the_window_to_close(tmp_path, monkeypatch):
    """Closing the last window does not end the process on macOS, so waiting on
    the process waits forever. The answer comes from the terminal instead - and
    the browser is then shut down here, because until it exits it still holds
    the profile lock that the image run needs."""
    launched = {}
    monkeypatch.setattr(browser_ai, "_browser_executable", lambda channel: "/bin/browser")
    monkeypatch.setattr(browser_ai.subprocess, "Popen", FakeBrowserProcess.spawning(launched))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    browser_ai.login(chatgpt.SITE, profile_dir=tmp_path / "profile")

    process = launched["process"]
    assert process.terminated
    assert process.waited          # ... and waited for, not just signalled


def test_login_leaves_a_browser_that_already_quit_alone(tmp_path, monkeypatch):
    launched = {}
    monkeypatch.setattr(browser_ai, "_browser_executable", lambda channel: "/bin/browser")
    monkeypatch.setattr(
        browser_ai.subprocess, "Popen", FakeBrowserProcess.spawning(launched, alive=False)
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    browser_ai.login(chatgpt.SITE, profile_dir=tmp_path / "profile")
    assert not launched["process"].terminated


def test_the_two_browsers_protect_the_saved_session_the_same_way(monkeypatch):
    """An installed browser gets the real keychain; Playwright's bundled one,
    whose path moves with every upgrade, keeps Playwright's fixed key."""
    monkeypatch.setattr(browser_ai.sys, "platform", "darwin")
    assert browser_ai._key_storage_args("chrome") == []
    assert browser_ai._key_storage_args(None) == list(browser_ai.PLAYWRIGHT_KEY_FLAGS)
    # Elsewhere the alternative is a keyring daemon that may not be running.
    monkeypatch.setattr(browser_ai.sys, "platform", "linux")
    assert browser_ai._key_storage_args("chrome") == list(browser_ai.PLAYWRIGHT_KEY_FLAGS)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_login_writes_the_session_the_way_the_driver_will_read_it(
    tmp_path, monkeypatch, platform
):
    """The bug this exists to prevent: sign in through a browser using one
    encryption key and drive one using another, and every cookie is silently
    discarded - a login that looks perfect and saves nothing."""
    launched = {}
    monkeypatch.setattr(browser_ai.sys, "platform", platform)
    monkeypatch.setattr(browser_ai, "_browser_executable", lambda channel: "/bin/browser")
    monkeypatch.setattr(browser_ai.subprocess, "Popen", FakeBrowserProcess.spawning(launched))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    browser_ai.login(chatgpt.SITE, profile_dir=tmp_path / "profile")

    expected = browser_ai._key_storage_args(chatgpt.SITE.channel)
    assert [flag for flag in browser_ai.PLAYWRIGHT_KEY_FLAGS
            if flag in launched["argv"]] == expected


def test_login_opens_the_same_browser_the_driver_will_use(tmp_path, monkeypatch):
    """A profile belongs to the browser that made it: log in with one and drive
    with another and the session is simply not there."""
    asked = []
    monkeypatch.setattr(
        browser_ai, "_browser_executable", lambda channel: asked.append(channel) or "/bin/b"
    )
    monkeypatch.setattr(browser_ai.subprocess, "Popen", FakeBrowserProcess.spawning({}))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    browser_ai.login(chatgpt.SITE, profile_dir=tmp_path / "a")
    browser_ai.login(meta_ai.SITE, profile_dir=tmp_path / "b")

    assert asked == [chatgpt.SITE.channel, meta_ai.SITE.channel]


def test_a_browser_that_is_not_installed_points_at_the_bundled_one(monkeypatch):
    monkeypatch.setattr(browser_ai.sys, "platform", "linux")
    monkeypatch.setattr(browser_ai.shutil, "which", lambda name: None)
    with pytest.raises(VideoLyricsError) as error:
        browser_ai._browser_executable("chrome")
    assert "null" in str(error.value)


def test_the_bundled_browser_is_found_without_starting_playwright(tmp_path, monkeypatch):
    """Read off disk, newest build first. Asking Playwright means starting its
    driver, which then litters the terminal on the way out."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(browser_ai.sys, "platform", "linux")
    for build in ("chromium-980", "chromium-1234"):
        binary = tmp_path / build / "chrome-linux" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")

    assert browser_ai._bundled_chromium() == str(
        tmp_path / "chromium-1234" / "chrome-linux" / "chrome"
    )


def test_no_bundled_browser_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(browser_ai.sys, "platform", "linux")
    with pytest.raises(VideoLyricsError) as error:
        browser_ai._browser_executable(None)
    assert "playwright install" in str(error.value)


def test_an_installed_browser_is_found_on_the_path(monkeypatch):
    monkeypatch.setattr(browser_ai.sys, "platform", "linux")
    monkeypatch.setattr(
        browser_ai.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "google-chrome" else None,
    )
    assert browser_ai._browser_executable("chrome") == "/usr/bin/google-chrome"


@pytest.mark.parametrize(
    "argv,expected_channel,expected_site",
    [
        (["browser-login"], "chrome", "chatgpt"),
        (["browser-login", "--provider", "meta"], None, "meta"),
        (["browser-login", "--channel", "bundled"], None, "chatgpt"),
        (["browser-login", "--channel", "msedge"], "msedge", "chatgpt"),
    ],
)
def test_the_browser_login_command_picks_the_browser(
    monkeypatch, tmp_path, argv, expected_channel, expected_site
):
    """`--channel bundled` is how you ask for Playwright's own browser from a
    command line, where there is no way to type null."""
    from video_lyrics import cli

    seen = {}

    def fake_login(site, **kwargs):
        seen.update(site=site, **kwargs)
        return tmp_path

    monkeypatch.setattr(browser_ai, "login", fake_login)
    monkeypatch.setattr(browser_ai, "verify_login", lambda site, **kwargs: True)
    assert cli.main(argv) == 0
    assert seen["channel"] == expected_channel
    assert seen["site"].name == expected_site


def test_a_login_that_did_not_save_the_session_is_reported_as_a_failure(
    monkeypatch, tmp_path, capsys
):
    """The whole point of checking: a login can look fine and leave nothing
    behind, and the alternative is finding out several stages later."""
    from video_lyrics import cli

    monkeypatch.setattr(browser_ai, "login", lambda site, **kwargs: tmp_path)
    monkeypatch.setattr(browser_ai, "verify_login", lambda site, **kwargs: False)

    assert cli.main(["browser-login"]) == 1
    assert "still signed out" in capsys.readouterr().out


# ------------------------------------------------------ typing the prompt in

class FakeComposer:
    """A chat box that either keeps what is typed into it, or quietly loses it."""

    def __init__(self, keeps: bool):
        self.keeps = keeps
        self.text = ""
        self.sent = 0

    def wait_for(self, state=None, timeout=None):
        return None

    def fill(self, text):
        self.text = text if self.keeps else ""

    def evaluate(self, script):
        return self.text

    def press(self, key):
        self.sent += 1


class FakeSubmitPage:
    """Hands out a different composer each time one is located."""

    def __init__(self, *composers):
        self.composers = list(composers)
        self.handed = []

    def locator(self, selector):
        return self

    @property
    def first(self):
        composer = self.composers[min(len(self.handed), len(self.composers) - 1)]
        self.handed.append(composer)
        return composer


def test_a_prompt_the_composer_dropped_is_typed_again():
    """Half-mounted editors take a fill() and throw the text away; Enter on an
    empty box sends nothing, and nothing is indistinguishable from a slow site."""
    decoy, real = FakeComposer(keeps=False), FakeComposer(keeps=True)
    page = FakeSubmitPage(decoy, real)

    browser_ai._submit(page, "#composer", "draw me a field at dawn", 1)

    assert decoy.sent == 0    # never pressed Enter on the one that lost the prompt
    assert real.sent == 1
    assert real.text == "draw me a field at dawn"


def test_a_composer_that_never_keeps_the_prompt_is_an_error_not_a_long_wait():
    page = FakeSubmitPage(FakeComposer(keeps=False))
    with pytest.raises(VideoLyricsError) as error:
        browser_ai._submit(page, "#composer", "draw me a field at dawn", 4)
    assert "scene 4" in str(error.value)
    assert len(page.handed) == browser_ai.SUBMIT_ATTEMPTS


def test_the_prompt_is_sent_once_when_it_lands_first_time():
    real = FakeComposer(keeps=True)
    page = FakeSubmitPage(real)
    browser_ai._submit(page, "#composer", "a prompt\n\nover two paragraphs", 2)
    assert real.sent == 1
    assert len(page.handed) == 1


# ---------------------------------------------------- what came down the wire

@pytest.mark.parametrize(
    "payload,suffix",
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, ".png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, ".jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp"),
    ],
)
def test_the_suffix_comes_from_the_bytes_not_the_content_type(payload, suffix):
    assert browser_ai._suffix_for(payload) == suffix


def test_an_html_error_page_is_not_saved_as_an_image():
    with pytest.raises(VideoLyricsError):
        browser_ai._suffix_for(b"<!doctype html><title>Sorry</title>")


# ------------------------------------------ reading what the page said instead
#
# A site fails to draw a scene for one of two reasons, and they want opposite
# answers: it will not draw *this prompt* (reword it and ask again), or it is
# busy / rate limited / erroring (leave the scene for a later run). Everything
# below is about telling those apart, and about the rest of the song surviving
# either - before this, one refusal in the middle cost every scene after it.

def test_a_capacity_notice_is_not_read_as_a_refusal():
    """"I can't generate images right now, please try again later" is a busy
    page wearing a refusal's clothes; rewording the prompt would not touch it."""
    assert browser_ai._diagnose(
        "I can't create images right now. Please try again later."
    )[0] == "busy"
    assert browser_ai._diagnose(
        "I can't create that image - it violates our content policy."
    )[0] == "refused"
    assert browser_ai._diagnose("Here is the image you asked for.") is None


def test_a_curly_apostrophe_is_still_a_refusal():
    """Both sites typeset "I can't" with U+2019, and a pattern list typed on a
    keyboard matches not one word of it."""
    kind, quote = browser_ai._diagnose("I can’t create that image.")
    assert kind == "refused"
    assert quote == "I can’t create that image."   # quoted as the page wrote it


def test_the_quoted_line_is_the_one_that_matched():
    kind, quote = browser_ai._diagnose(
        "Sure, let me try.\nI can't create that image.\nAnything else?"
    )
    assert (kind, quote) == ("refused", "I can't create that image.")


def test_only_what_the_prompt_added_is_read():
    """The previous scene's refusal is still on screen on a site that keeps one
    long conversation; read as new, it would condemn every scene after it."""
    before = ["I can't create that image."]
    now = ["I can't create that image.\nHere is the image you asked for."]
    assert browser_ai._diagnose(browser_ai._unseen_text(now, before)) is None


def test_the_same_refusal_twice_is_read_the_second_time_too():
    """Each earlier block is subtracted once, not everywhere - a site that says
    exactly the same no to the reworded prompt is still saying no."""
    before = ["I can't create that image."]
    now = ["I can't create that image.", "I can't create that image."]
    assert browser_ai._diagnose(browser_ai._unseen_text(now, before))[0] == "refused"


# --------------------------------------------------- one prompt after another

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def data_url(payload: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(payload).decode()


class ScriptedComposer:
    def __init__(self, page):
        self.page = page
        self.text = ""

    def wait_for(self, state=None, timeout=None):
        return None

    def fill(self, text):
        self.text = text

    def evaluate(self, script):
        return self.text

    def press(self, key):
        self.page.submitted(self.text)


class ScriptedPage:
    """A chat page that answers each prompt from a script.

    One entry per prompt sent: `image` draws one, `refuse` says no in words,
    `busy` pleads capacity, `junk` hands back something that is not an image, and
    `silent` does nothing at all (which is what a moved selector looks like).
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.replies: list[str] = []
        self.images: list[str] = []
        self.gotos = 0

    def evaluate(self, script, arg=None):
        if "images:" in script:                       # _page_state
            return {
                "images": [
                    {"src": src, "width": 1024, "height": 576, "complete": True}
                    for src in self.images
                ],
                "ready": 0,
                "busy": 0,
                "replies": list(self.replies),
            }
        if "return texts(" in script:                 # _reply_texts
            return list(self.replies)
        if "return count(" in script:                 # _visible_count
            return 0
        return [{"src": src} for src in self.images]  # _image_sources

    def goto(self, url, wait_until=None):
        self.gotos += 1
        self.replies.clear()
        self.images.clear()

    def locator(self, selector):
        return self

    @property
    def first(self):
        return ScriptedComposer(self)

    def submitted(self, text):
        self.prompts.append(text)
        answer = self.answers.pop(0) if self.answers else "silent"
        if answer == "image":
            self.images.append(data_url(PNG_HEADER + str(len(self.prompts)).encode()))
        elif answer == "junk":
            self.images.append(data_url(b"<!doctype html><title>Sorry</title>"))
        elif answer == "refuse":
            self.replies.append("I can't create that image.")
        elif answer == "busy":
            self.replies.append("Something went wrong. Please try again later.")


def scenes(count: int) -> list[dict]:
    return [
        {"index": index, "prompt": f"a lantern in the rain, take {index}"}
        for index in range(1, count + 1)
    ]


def run(page, scene_list, images_dir, *, site=None, **overrides):
    """Drive the per-scene loop with no browser and no waiting."""
    site = site or dataclasses.replace(
        chatgpt.SITE,
        ready_selector=None,   # this fake page has neither signal, so "finished"
        busy_selector=None,    # rests on the src holding still, as it does live
        image_timeout_ms=overrides.pop("image_timeout_ms", 200),
    )
    return browser_ai._run_scenes(
        page, site, scene_list,
        images_dir=images_dir,
        composer_selector=site.composer_selector,
        image_selector=site.image_selector,
        min_delay=0, max_delay=0,
    )


@pytest.fixture(autouse=True)
def no_cooling_off(monkeypatch):
    monkeypatch.setattr(browser_ai, "BUSY_BACKOFF_S", 0)


def test_a_refused_prompt_is_reworded_and_asked_again(tmp_path):
    page = ScriptedPage("refuse", "image")
    assert run(page, scenes(1), tmp_path) == []

    assert len(page.prompts) == 2
    # Same scene, described the same way - only the asking changed.
    assert all("a lantern in the rain" in prompt for prompt in page.prompts)
    assert page.prompts[0] != page.prompts[1]
    assert len(list(tmp_path.iterdir())) == 1


def test_a_scene_refused_every_way_is_skipped_and_the_song_carries_on(tmp_path):
    """The bug this exists to prevent: one refusal mid-song used to raise, and
    every scene after it was never even asked for."""
    page = ScriptedPage(*["refuse"] * len(browser_ai.PROMPT_SOFTENERS), "image")
    assert run(page, scenes(2), tmp_path) == [1]

    assert len(page.prompts) == len(browser_ai.PROMPT_SOFTENERS) + 1
    assert len(list(tmp_path.iterdir())) == 1   # scene 2's image, and only it


def test_a_busy_site_is_not_argued_with_only_skipped(tmp_path):
    """No wording helps a site that is out of capacity, so the prompt is not
    reworded: the scene is left for a later run, which asks only for what is
    missing."""
    page = ScriptedPage("busy", "image")
    assert run(page, scenes(2), tmp_path) == [1]
    assert len(page.prompts) == 2


def test_a_download_that_hands_back_something_else_is_skipped_too(tmp_path):
    """The picture was drawn and accepted - the bytes just did not arrive. That
    says nothing about the prompt, so it is a later run's problem."""
    page = ScriptedPage("junk", "image")
    assert run(page, scenes(2), tmp_path) == [1]


def test_a_site_that_keeps_saying_it_is_busy_ends_the_run(tmp_path):
    """Three capacity notices in a row is not bad luck, and the scenes left
    unasked are better off waiting than burning a timeout each."""
    page = ScriptedPage(*["busy"] * 6)
    assert run(page, scenes(5), tmp_path) == [1, 2, 3]
    assert len(page.prompts) == browser_ai.MAX_BUSY_SKIPS


def test_one_unexplained_failure_is_survived(tmp_path):
    page = ScriptedPage("silent", "image")
    assert run(page, scenes(2), tmp_path, image_timeout_ms=1) == [1]


def test_two_unexplained_failures_in_a_row_stop_the_run(tmp_path):
    """Nothing on the page to explain it, twice over, is the markup having moved -
    and the error naming the selector settings is the only useful thing left."""
    page = ScriptedPage("silent", "silent", "image")
    with pytest.raises(VideoLyricsError) as error:
        run(page, scenes(3), tmp_path, image_timeout_ms=1)
    assert "chatgpt_image_selector" in str(error.value)


def test_a_refusal_does_not_count_towards_giving_up(tmp_path):
    """Dark subject matter can have several scenes refused in a row; the site is
    answering fine, and the next prompt is a different question."""
    refusals = ["refuse"] * len(browser_ai.PROMPT_SOFTENERS)
    page = ScriptedPage(*refusals, *refusals, *refusals, "image")
    assert run(page, scenes(4), tmp_path) == [1, 2, 3]


def test_a_reworded_prompt_is_asked_in_a_fresh_chat(tmp_path):
    """Asked again in the same conversation, a site that has just said no is
    answering its own last turn as much as the new prompt."""
    page = ScriptedPage("refuse", "image")
    run(page, scenes(1), tmp_path)
    assert page.gotos == 1          # ... and only for the retry: scene 1 starts where it is


def test_a_bad_response_is_a_skip_not_the_end_of_the_run():
    class Response:
        ok = False
        status = 429

    class Page:
        request = type("Request", (), {"get": staticmethod(lambda src: Response())})()

    with pytest.raises(browser_ai.SiteBusy):
        browser_ai._download(Page(), "https://example.test/image.png", 7)


# ----------------------------------------------------------- site definitions

@pytest.mark.parametrize("provider", images.BROWSER_PROVIDERS)
def test_the_driver_writes_the_file_the_images_stage_goes_looking_for(provider):
    """The driver names its download after `site.name` and the images stage looks
    it up by provider, so a site whose name drifts from its provider key would
    download every picture and then report every scene as missing."""
    site = importlib.import_module(
        f"video_lyrics.{images.BROWSER_MODULES[provider]}"
    ).SITE
    scene = {"index": 3, "prompt": "a lantern in the rain"}
    assert scene_stem(scene, site.name) == images._stem_for(scene, provider)


@pytest.mark.parametrize("site", [chatgpt.SITE, meta_ai.SITE])
def test_every_site_can_read_its_own_words(site):
    """Without somewhere to read the page's text, a refusal is indistinguishable
    from a slow site and costs the whole image timeout before anyone finds out."""
    assert site.reply_selector


@pytest.mark.parametrize("softener", browser_ai.PROMPT_SOFTENERS)
def test_every_rewording_keeps_the_scene_s_own_description(softener):
    assert "{prompt}" in softener


@pytest.mark.parametrize("site", [chatgpt.SITE, meta_ai.SITE])
def test_every_site_can_say_when_it_has_finished(site):
    """Without at least one of the two signals, `_finished` is true the moment any
    image exists - which is exactly the preview trap these sites set."""
    assert site.ready_selector or site.busy_selector
    assert isinstance(site.profile_dir, Path)
    # The prompt template has to keep the scene's own prompt intact.
    assert "{prompt}" in site.prompt_template
