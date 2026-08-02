"""Assemble and render the video in DaVinci Resolve (free edition, v18+).

Timeline layout built by `assemble`:

    V3  Title      one transparent clip, fades in/out over the intro
    V2  Lyrics     one transparent clip per confirmed lyric line, fades in/out
    V1  Images     the Ken Burns bed: scene clips separated by cross dissolves
    A1  Music      the song

Resolve's scripting API exposes no transition and no keyframe calls, so motion and
fades arrive pre-baked as media (see motion.py / overlays.py) and this module does
the edit: import, place on tracks at exact frames, then render.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

from .util import VideoLyricsError, log, run

MAC_API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
MAC_LIB = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
LINUX_API = "/opt/resolve/Developer/Scripting"
LINUX_LIB = "/opt/resolve/libs/Fusion/fusionscript.so"
WINDOWS_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
WINDOWS_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"

TRACK_IMAGES = 1
TRACK_LYRICS = 2
TRACK_TITLE = 3


# ---------------------------------------------------------------- connection


def _api_paths() -> tuple[str, str]:
    api = os.environ.get("RESOLVE_SCRIPT_API")
    lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    if sys.platform == "darwin":
        return api or MAC_API, lib or MAC_LIB
    if sys.platform.startswith("win"):
        return api or WINDOWS_API, lib or WINDOWS_LIB
    return api or LINUX_API, lib or LINUX_LIB


def _load_module():
    api, lib = _api_paths()
    modules = Path(api) / "Modules"
    if not modules.is_dir():
        raise VideoLyricsError(
            f"DaVinci Resolve scripting modules not found at {modules}. "
            "Install Resolve, or set RESOLVE_SCRIPT_API."
        )
    os.environ.setdefault("RESOLVE_SCRIPT_API", api)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib)
    if str(modules) not in sys.path:
        sys.path.append(str(modules))
    spec = importlib.util.find_spec("DaVinciResolveScript")
    if spec is None:
        raise VideoLyricsError(f"DaVinciResolveScript.py not importable from {modules}")
    import DaVinciResolveScript as dvr  # noqa: PLC0415 - path is set up above

    return dvr


SCRIPTING_MODES = {0: "None", 1: "Local", 2: "Local and network"}


def scripting_preference() -> int | None:
    """Read Resolve's 'External scripting using' preference, or None if unknown.

    Resolve ships with scripting switched off; every API call then silently returns
    nothing, which is otherwise very hard to diagnose.
    """
    candidates = [
        Path.home() / "Library/Preferences/Blackmagic Design/DaVinci Resolve/config.dat",
        Path.home() / ".local/share/DaVinciResolve/configs/config.dat",
        Path(os.environ.get("APPDATA", "")) / "Blackmagic Design/DaVinci Resolve/Support/config.dat",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(errors="replace").splitlines():
                if line.strip().startswith("System.Scripting.Mode"):
                    return int(line.split("=")[-1].strip())
        except (OSError, ValueError):
            continue
    return None


HANDOFF_HINT = (
    "Run `video-lyrics render` and finish from Resolve's Workspace > Scripts > "
    "Video Lyrics Creator, or render without Resolve using --engine ffmpeg."
)


def scripting_hint() -> str:
    mode = scripting_preference()
    if mode == 0:
        return (
            "External scripting is off (System.Scripting.Mode = 0). The free edition "
            "has no switch for it; Studio exposes one at Preferences > System > "
            "General > 'External scripting using' > Local. " + HANDOFF_HINT
        )
    if mode is None:
        return (
            "Resolve is not answering. Make sure it is running with a project or the "
            "Project Manager open. " + HANDOFF_HINT
        )
    return (
        f"External scripting is set to {SCRIPTING_MODES.get(mode, mode)!r} but Resolve "
        "is not answering; make sure it has finished loading. " + HANDOFF_HINT
    )


def is_running() -> bool:
    if sys.platform != "darwin":
        return True
    proc = run(["pgrep", "-x", "Resolve"], check=False)
    if proc.returncode == 0:
        return True
    proc = run(["pgrep", "-f", "DaVinci Resolve.app"], check=False)
    return proc.returncode == 0


def launch_and_wait(timeout: float = 180.0) -> None:
    """Open Resolve and wait until it answers scripting calls."""
    if sys.platform != "darwin":
        raise VideoLyricsError("Automatic launch is only implemented on macOS.")
    log.info("Launching DaVinci Resolve ...")
    run(["open", "-a", "DaVinci Resolve"], check=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        try:
            connect()
            return
        except VideoLyricsError:
            continue
    raise VideoLyricsError(
        "DaVinci Resolve did not become scriptable in time. Open it, dismiss any "
        "dialogs, and make sure Preferences > System > General > External scripting "
        "using is set to Local."
    )


def connect(existing=None):
    """Return the Resolve application object.

    `existing` is what Resolve itself hands to a script started from
    Workspace > Scripts; outside Resolve we go through the scripting module, which
    only answers when external scripting is available (see scripting_hint).
    """
    if existing is not None:
        return existing
    dvr = _load_module()
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise VideoLyricsError(f"Could not reach DaVinci Resolve. {scripting_hint()}")
    return resolve


def check() -> str:
    """Report which of the two routes into Resolve is open on this machine."""
    from . import handoff

    mode = scripting_preference()
    lines = [
        f"Resolve running   : {'yes' if is_running() else 'no'}",
        f"scripting pref    : {SCRIPTING_MODES.get(mode, mode)}",
        f"menu launcher     : {'installed' if handoff.is_installed() else 'not installed'}",
        f"                    {handoff.scripts_dir() / handoff.LAUNCHER_NAME}",
    ]
    try:
        resolve = connect()
        project = resolve.GetProjectManager().GetCurrentProject()
        lines += [
            f"external scripting: {resolve.GetProductName()} {resolve.GetVersionString()}",
            f"current project   : {project.GetName() if project else '(none open)'}",
            "",
            "`video-lyrics render` can drive Resolve directly.",
        ]
    except VideoLyricsError as error:
        lines += [
            "external scripting: unavailable",
            f"                    {error}",
            "",
            "That is normal on the free edition, which has no external scripting",
            "preference. `video-lyrics render` prepares everything and installs a",
            "launcher; finish with Workspace > Scripts > Video Lyrics Creator.",
        ]
    return "\n".join(lines)


# ------------------------------------------------------------------ helpers


# Every Resolve API method this module calls. `tests/test_resolve_api.py` checks the
# list against the SDK reference, because Resolve's binding answers an unknown
# method with None instead of raising - a typo shows up much later as
# "'NoneType' object is not callable".
USED_API = frozenset(
    {
        "GetProductName", "GetVersionString", "GetProjectManager",
        "GotoRootFolder", "GetCurrentProject", "LoadProject", "CreateProject",
        "GetName", "GetMediaPool", "SetSetting", "SetCurrentTimeline",
        "GetTimelineCount", "GetTimelineByIndex",
        "GetRootFolder", "SetCurrentFolder", "ImportMedia", "GetClipProperty",
        "CreateEmptyTimeline", "DeleteTimelines", "AppendToTimeline",
        "GetStartFrame", "GetTrackCount", "AddTrack", "SetTrackName",
        "SetCurrentRenderFormatAndCodec", "SetCurrentRenderMode", "SetRenderSettings",
        "AddRenderJob", "StartRendering", "IsRenderingInProgress", "GetRenderJobStatus",
        "GetRenderFormats", "GetRenderCodecs",
    }
)


def _api(obj, name: str):
    """Look up a Resolve API method, failing loudly when this build has not got it."""
    method = getattr(obj, name, None)
    if method is None or not callable(method):
        raise VideoLyricsError(
            f"This DaVinci Resolve build does not provide {name}(). "
            "Check the scripting README for your version."
        )
    return method


def _find_timeline(project, name: str):
    """There is no GetTimelineByName in the API, so walk the timelines."""
    count = int(_api(project, "GetTimelineCount")() or 0)
    for index in range(1, count + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline is not None and timeline.GetName() == name:
            return timeline
    return None


def _open_project(resolve, name: str):
    """Reuse the Resolve project of that name, or make one. Never deletes a project."""
    manager = _api(resolve, "GetProjectManager")()
    manager.GotoRootFolder()
    current = manager.GetCurrentProject()
    if current is not None and current.GetName() == name:
        return manager, current

    project = manager.LoadProject(name)
    if project is None:
        project = manager.CreateProject(name)
    if project is None:
        # Resolve can switch to a freshly created project while still answering
        # None, and it refuses to create one that already exists; look again.
        project = manager.LoadProject(name)
    if project is None:
        current = manager.GetCurrentProject()
        if current is not None and current.GetName() == name:
            project = current
    if project is None and current is not None:
        # Better to build in the project that is open than to give up: the user
        # can always move the timeline afterwards.
        log.warning(
            "Could not open or create a Resolve project called %r; building in the "
            "open project %r instead.", name, current.GetName(),
        )
        project = current
    if project is None:
        raise VideoLyricsError(
            f"Could not create or open Resolve project {name!r}, and no project is "
            "open. Open any project in Resolve and run this again."
        )
    return manager, project


def _apply_project_settings(project, *, width: int, height: int, fps: float) -> None:
    settings = {
        "timelineResolutionWidth": str(width),
        "timelineResolutionHeight": str(height),
        "timelineFrameRate": f"{fps:g}",
        "timelinePlaybackFrameRate": f"{fps:g}",
        "videoMonitorFormat": f"HD {height}p {fps:g}",
        "superScale": "0",
    }
    for key, value in settings.items():
        if not project.SetSetting(key, value):
            log.debug("Resolve rejected project setting %s=%s", key, value)


def _import(media_pool, paths: list[str]) -> dict[str, Any]:
    """Import files and map absolute path -> MediaPoolItem."""
    unique = list(dict.fromkeys(paths))
    items = media_pool.ImportMedia(unique) or []
    mapping: dict[str, Any] = {}
    for item in items:
        file_path = item.GetClipProperty("File Path")
        if file_path:
            mapping[str(Path(file_path).resolve())] = item
    missing = [path for path in unique if str(Path(path).resolve()) not in mapping]
    if missing:
        # Fall back to importing the stragglers one by one.
        for path in missing:
            single = media_pool.ImportMedia([path]) or []
            if not single:
                raise VideoLyricsError(f"Resolve could not import {path}")
            mapping[str(Path(path).resolve())] = single[0]
    return mapping


def _append(timeline, media_pool, entries: list[dict[str, Any]]) -> None:
    """Place clips at exact timeline frames. `entries` need item/track/record/frames."""
    start_frame = timeline.GetStartFrame()
    payload = []
    for entry in entries:
        clip_info = {
            "mediaPoolItem": entry["item"],
            "startFrame": 0,
            "endFrame": max(0, int(entry["frames"]) - 1),
            "trackIndex": int(entry["track"]),
            "recordFrame": start_frame + int(entry["record"]),
        }
        if entry.get("media_type"):
            clip_info["mediaType"] = int(entry["media_type"])
        payload.append(clip_info)
    placed = media_pool.AppendToTimeline(payload)
    if not placed or len(placed) < len(payload):
        raise VideoLyricsError(
            f"Resolve placed {len(placed or [])} of {len(payload)} clips on the timeline."
        )


def _unused_timeline_name(project, name: str) -> str:
    for suffix in range(2, 100):
        candidate = f"{name} {suffix}"
        if _find_timeline(project, candidate) is None:
            return candidate
    return name


def _ensure_tracks(timeline, video: int, audio: int = 1) -> None:
    while timeline.GetTrackCount("video") < video:
        if not timeline.AddTrack("video"):
            raise VideoLyricsError("Could not add a video track to the timeline.")
    while timeline.GetTrackCount("audio") < audio:
        if not timeline.AddTrack("audio"):
            raise VideoLyricsError("Could not add an audio track to the timeline.")


# ----------------------------------------------------------------- assembly


def assemble(
    *,
    clips: list[dict[str, Any]],
    lyric_items: list[dict[str, Any]],
    title_item: dict[str, Any] | None,
    audio: Path,
    subtitle_file: Path | None,
    project_name: str,
    timeline_name: str,
    size: tuple[int, int],
    fps: float,
    duration: float,
    replace: bool = True,
    resolve=None,
) -> tuple[Any, Any, Any]:
    """Build the timeline. Returns (resolve, project, timeline)."""
    resolve = connect(resolve)
    width, height = size
    _manager, project = _open_project(resolve, project_name)
    _apply_project_settings(project, width=width, height=height, fps=fps)

    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    media_pool.SetCurrentFolder(root)

    paths = [clip["path"] for clip in clips]
    paths += [item["clip"] for item in lyric_items]
    if title_item:
        paths.append(title_item["clip"])
    paths.append(str(audio))
    log.info("Importing %d files into the media pool ...", len(set(paths)))
    pool = _import(media_pool, paths)

    existing = _find_timeline(project, timeline_name)
    if existing is not None:
        if replace:
            media_pool.DeleteTimelines([existing])
        else:
            # Keep the old edit around and build alongside it.
            timeline_name = _unused_timeline_name(project, timeline_name)
    timeline = _api(media_pool, "CreateEmptyTimeline")(timeline_name)
    if timeline is None:
        raise VideoLyricsError(
            f"Could not create timeline {timeline_name!r}. If one of that name is open, "
            "close it in Resolve and try again."
        )
    project.SetCurrentTimeline(timeline)
    timeline.SetSetting("useCustomSettings", "1")
    timeline.SetSetting("timelineResolutionWidth", str(width))
    timeline.SetSetting("timelineResolutionHeight", str(height))
    timeline.SetSetting("timelineFrameRate", f"{fps:g}")

    _ensure_tracks(timeline, video=TRACK_TITLE if title_item else TRACK_LYRICS)
    timeline.SetTrackName("video", TRACK_IMAGES, "Images")
    if timeline.GetTrackCount("video") >= TRACK_LYRICS:
        timeline.SetTrackName("video", TRACK_LYRICS, "Lyrics")
    if title_item and timeline.GetTrackCount("video") >= TRACK_TITLE:
        timeline.SetTrackName("video", TRACK_TITLE, "Title")
    timeline.SetTrackName("audio", 1, "Music")

    entries: list[dict[str, Any]] = []
    for clip in clips:
        entries.append(
            {
                "item": pool[str(Path(clip["path"]).resolve())],
                "track": TRACK_IMAGES,
                "record": clip["first_frame"],
                "frames": clip["frames"],
                "media_type": 1,
            }
        )
    for item in lyric_items:
        entries.append(
            {
                "item": pool[str(Path(item["clip"]).resolve())],
                "track": TRACK_LYRICS,
                "record": int(round(item["start"] * fps)),
                "frames": max(1, int(round((item["end"] - item["start"]) * fps))),
                "media_type": 1,
            }
        )
    if title_item:
        entries.append(
            {
                "item": pool[str(Path(title_item["clip"]).resolve())],
                "track": TRACK_TITLE,
                "record": int(round(title_item["start"] * fps)),
                "frames": max(1, int(round((title_item["end"] - title_item["start"]) * fps))),
                "media_type": 1,
            }
        )
    entries.append(
        {
            "item": pool[str(Path(audio).resolve())],
            "track": 1,
            "record": 0,
            "frames": int(round(duration * fps)),
            "media_type": 2,
        }
    )

    log.info("Placing %d clips on the timeline ...", len(entries))
    _append(timeline, media_pool, entries)

    if subtitle_file is not None:
        _add_subtitle_track(media_pool, timeline, subtitle_file)

    log.info(
        "Timeline %r built: %d video tracks, %s frames.",
        timeline_name,
        timeline.GetTrackCount("video"),
        int(round(duration * fps)),
    )
    return resolve, project, timeline


def _add_subtitle_track(media_pool, timeline, subtitle_file: Path) -> None:
    """Best effort: bring the SRT in as a real subtitle track."""
    try:
        if timeline.GetTrackCount("subtitle") < 1:
            timeline.AddTrack("subtitle")
        items = media_pool.ImportMedia([str(subtitle_file)]) or []
        if not items:
            raise RuntimeError("import returned nothing")
        placed = media_pool.AppendToTimeline(
            [{"mediaPoolItem": items[0], "trackIndex": 1, "mediaType": 3}]
        )
        if not placed:
            raise RuntimeError("append returned nothing")
        log.info("Subtitle track loaded from %s", subtitle_file.name)
    except Exception as exc:  # noqa: BLE001 - subtitles are a bonus, never fatal
        log.warning(
            "Could not attach %s as a subtitle track (%s). The burned-in Lyrics track "
            "is already on the timeline; import the SRT by hand if you want both.",
            subtitle_file.name,
            exc,
        )


# ------------------------------------------------------------------- render


def render(
    project,
    *,
    output: Path,
    fmt: str = "mp4",
    codec: str = "H264",
    audio_codec: str = "aac",
    width: int = 1920,
    height: int = 1080,
    fps: float = 30,
    poll: float = 5.0,
    progress: Any = None,
) -> Path:
    """Queue a single render job for the current timeline and wait for it."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    _select_format(project, fmt, codec, progress)
    _api(project, "SetCurrentRenderMode")(1)  # single clip
    settings = {
        "SelectAllFrames": True,
        "TargetDir": str(output.parent),
        "CustomName": output.stem,
        "FormatWidth": int(width),
        "FormatHeight": int(height),
        "FrameRate": float(fps),
        "ExportVideo": True,
        "ExportAudio": True,
        "AudioCodec": audio_codec,
        "AudioBitDepth": 24,
        "AudioSampleRate": 48000,
    }
    if not project.SetRenderSettings(settings):
        log.warning("Some render settings were rejected; continuing with Resolve defaults.")

    say = progress or (lambda message: log.info("%s", message))
    job_id = _api(project, "AddRenderJob")()
    if not job_id:
        raise VideoLyricsError(
            "Resolve refused to queue the render job. Check the Deliver page for the "
            "reason - an unwritable target directory is the usual one."
        )
    say("rendering in DaVinci Resolve -> {}".format(output))
    started = time.time()
    if not _api(project, "StartRendering")([job_id], isInteractiveMode=False):
        raise VideoLyricsError("Resolve could not start rendering.")

    # Rendering starts asynchronously, so give it a moment to report itself busy
    # before treating "not in progress" as "finished".
    deadline = time.time() + 15
    while not project.IsRenderingInProgress() and time.time() < deadline:
        if (project.GetRenderJobStatus(job_id) or {}).get("JobStatus") == "Complete":
            break
        time.sleep(0.5)

    last_percent = -1
    while project.IsRenderingInProgress():
        status = project.GetRenderJobStatus(job_id) or {}
        percent = int(status.get("CompletionPercentage", 0) or 0)
        if percent != last_percent:
            say("render {}%".format(percent))
            last_percent = percent
        time.sleep(poll)

    status = project.GetRenderJobStatus(job_id) or {}
    job_status = status.get("JobStatus")
    if job_status not in (None, "Complete"):
        raise VideoLyricsError(
            "Render did not complete: {} ({})".format(
                job_status, status.get("Error") or status
            )
        )

    produced = _find_output(output, since=started)
    say("Resolve wrote {}".format(produced))
    return produced


def _select_format(project, fmt: str, codec: str, progress: Any = None) -> tuple[str, str]:
    """Set the render format/codec, matching loosely against what this build offers."""
    say = progress or (lambda message: log.info("%s", message))
    setter = _api(project, "SetCurrentRenderFormatAndCodec")
    if setter(fmt, codec):
        return fmt, codec

    formats = _api(project, "GetRenderFormats")() or {}     # description -> extension
    extension = next(
        (str(value) for value in formats.values() if str(value).lower() == fmt.lower()),
        None,
    )
    if extension:
        codecs = project.GetRenderCodecs(extension) or {}   # description -> codec name
        wanted = codec.lower().replace(".", "")
        match = next(
            (
                str(value) for description, value in codecs.items()
                if wanted in (str(value).lower().replace(".", ""),
                              str(description).lower().replace(".", ""))
            ),
            None,
        )
        if match and setter(extension, match):
            say("using render codec {!r} for {}".format(match, extension))
            return extension, match

    raise VideoLyricsError(
        "Resolve rejected format/codec {}/{}. This build offers formats {} - run "
        "`video-lyrics resolve-formats` for the codecs of each.".format(
            fmt, codec, ", ".join(sorted(str(v) for v in formats.values())) or "(none)"
        )
    )


def _find_output(output: Path, *, since: float | None = None) -> Path:
    """Locate what Resolve just wrote, ignoring anything left over from before.

    Resolve may add its own suffix, and an older file with the right name must not
    be mistaken for a fresh render.
    """
    candidates = [output] if output.is_file() else []
    candidates += [
        path for path in sorted(output.parent.glob(f"{output.stem}*"))
        if path.is_file() and path != output
    ]
    fresh = [
        path for path in candidates
        if since is None or path.stat().st_mtime >= since - 5
    ]
    if fresh:
        return fresh[0]
    if candidates:
        raise VideoLyricsError(
            f"{candidates[0]} was not written by this render (it is older than the "
            "job). Check the Deliver page - the render probably failed or was "
            "cancelled."
        )
    raise VideoLyricsError(
        f"Render reported success but no file appeared at {output}. "
        "Check the Deliver page for the job's status."
    )


def build_and_render(project, *, resolve=None, progress: Any = None, replace: bool | None = None) -> str:
    """Assemble the timeline for a prepared project and render it.

    This is the whole Resolve side of the pipeline, callable two ways: from the CLI
    when external scripting is available, and from the launcher script running
    inside Resolve, which passes its own `resolve` object.
    """
    say = progress or (lambda message: log.info("%s", message))
    settings = project.data.get("render", {})
    if replace is None:
        replace = bool(settings.get("replace_existing", True))

    clips = project.data.get("bed")
    if not clips:
        raise VideoLyricsError("No image bed prepared. Run `video-lyrics bed` first.")
    overlay_data = project.data.get("overlays") or {}
    lyric_items = [item for item in overlay_data.get("lyrics", []) if item.get("clip")]
    title_item = overlay_data.get("title")
    if title_item and not title_item.get("clip"):
        title_item = None

    missing = [
        clip["path"] for clip in clips if not Path(clip["path"]).is_file()
    ] + [
        item["clip"] for item in lyric_items if not Path(item["clip"]).is_file()
    ]
    if title_item and not Path(title_item["clip"]).is_file():
        missing.append(title_item["clip"])
    if not Path(project.audio).is_file():
        missing.append(str(project.audio))
    if missing:
        raise VideoLyricsError(
            "Prepared media is missing ({} file(s), e.g. {}). "
            "Re-run `video-lyrics run --to bed`.".format(len(missing), missing[0])
        )

    say("importing {} bed clips and {} overlays".format(len(clips), len(lyric_items)))
    _resolve, resolve_project, _timeline = assemble(
        clips=clips,
        lyric_items=lyric_items,
        title_item=title_item,
        audio=project.audio,
        subtitle_file=(
            Path(overlay_data["srt"])
            if settings.get("lyrics_mode") == "subtitle" and overlay_data.get("srt")
            else None
        ),
        project_name=project.title,
        timeline_name="{} - lyrics".format(project.title),
        size=project.size,
        fps=project.fps,
        duration=project.duration,
        replace=replace,
        resolve=resolve,
    )

    width, height = project.size
    say("rendering to {}".format(project.output))
    produced = render(
        resolve_project,
        output=project.output,
        fmt=settings.get("format", "mp4"),
        codec=settings.get("codec", "H264"),
        audio_codec=settings.get("audio_codec", "aac"),
        width=width,
        height=height,
        fps=project.fps,
        progress=say,
    )
    project.data["render"]["last_output"] = str(produced)
    try:
        project.save()
    except Exception as error:  # noqa: BLE001 - the render succeeded; recording it is a bonus
        # Resolve's own Python may lack PyYAML, and a finished render must not be
        # reported as a failure because a bookkeeping write did not land.
        say("rendered, but could not update {}: {}".format(project.path.name, error))
    return str(produced)


def formats() -> dict[str, Any]:
    """Report the format/codec combinations this Resolve install supports."""
    resolve = connect()
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise VideoLyricsError("Open a project in Resolve first.")
    available = project.GetRenderFormats() or {}
    return {name: project.GetRenderCodecs(extension) for name, extension in available.items()}
