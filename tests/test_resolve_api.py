"""Guard against calling Resolve API methods that do not exist.

Resolve's Python binding answers an unknown method with None rather than raising,
so a typo does not fail where it is written - it fails later as
"'NoneType' object is not callable", often after the script has already changed
the user's project.  `GetTimelineByName` was exactly that: plausible, and absent.

These tests read the API reference that ships with Resolve and cross-check every
Resolve call in render_resolve.py against it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from video_lyrics import render_resolve

SDK_README = Path(render_resolve._api_paths()[0]) / "README.txt"
SOURCE = Path(render_resolve.__file__).read_text(encoding="utf-8")

# PascalCase calls in the module that are ours, not Resolve's.
NOT_RESOLVE_API = {"Path", "VideoLyricsError", "GetResolve"}


def documented_methods() -> set[str]:
    """Every method name the shipped API reference defines."""
    text = SDK_README.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"^\s{2}([A-Z]\w+)\(", text, flags=re.MULTILINE))


def called_methods() -> set[str]:
    """Every Resolve method the module reaches for, written either way.

    `obj.SomeMethod(...)` directly, or `_api(obj, "SomeMethod")(...)`.
    """
    direct = set(re.findall(r"\.([A-Z]\w+)\(", SOURCE))
    looked_up = set(re.findall(r"_api\([^,]+,\s*\"(\w+)\"\)", SOURCE))
    return (direct | looked_up) - NOT_RESOLVE_API


def test_every_resolve_call_is_declared_in_used_api():
    """USED_API is the module's own list; keep it honest."""
    undeclared = called_methods() - render_resolve.USED_API
    assert not undeclared, (
        f"Resolve calls missing from USED_API: {sorted(undeclared)}. "
        "Add them there so the next test can check them against the SDK."
    )


@pytest.mark.skipif(not SDK_README.is_file(), reason="DaVinci Resolve SDK not installed")
def test_every_method_used_exists_in_the_resolve_api():
    documented = documented_methods()
    assert "GetTimelineByIndex" in documented          # the reference parsed sensibly
    assert "GetTimelineByName" not in documented       # the method that caused the bug

    missing = sorted(render_resolve.USED_API - documented)
    assert not missing, f"Not in the Resolve scripting API: {missing}"


@pytest.mark.skipif(not SDK_README.is_file(), reason="DaVinci Resolve SDK not installed")
def test_the_test_double_only_implements_methods_resolve_really_has():
    """A fake that invents methods hides exactly the bug this file exists for."""
    from . import fakes

    documented = documented_methods()
    invented = set()
    for name in dir(fakes):
        obj = getattr(fakes, name)
        if not isinstance(obj, type):
            continue
        invented |= {
            attribute for attribute in vars(obj)
            if attribute[:1].isupper() and attribute not in documented
        }
    assert not invented, f"The fake implements methods Resolve does not have: {sorted(invented)}"


def test_a_missing_api_method_is_reported_clearly():
    class OldBuild:
        GetTimelineCount = None      # how Resolve reports a method it does not have

    with pytest.raises(Exception, match="does not provide GetTimelineCount"):
        render_resolve._api(OldBuild(), "GetTimelineCount")


def test_timelines_are_found_by_walking_the_index():
    class Timeline:
        def __init__(self, name):
            self.name = name

        def GetName(self):  # noqa: N802
            return self.name

    class Project:
        def __init__(self, names):
            self.timelines = [Timeline(name) for name in names]

        def GetTimelineCount(self):  # noqa: N802
            return len(self.timelines)

        def GetTimelineByIndex(self, index):  # noqa: N802
            return self.timelines[index - 1]  # Resolve indexes from 1

    project = Project(["Other", "Song - lyrics"])
    assert render_resolve._find_timeline(project, "Song - lyrics").GetName() == "Song - lyrics"
    assert render_resolve._find_timeline(project, "Nope") is None
    assert render_resolve._find_timeline(Project([]), "Nope") is None
