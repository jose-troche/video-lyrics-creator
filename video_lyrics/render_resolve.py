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


def scripting_hint() -> str:
    mode = scripting_preference()
    if mode == 0:
        return (
            "DaVinci Resolve has external scripting switched OFF "
            "(System.Scripting.Mode = 0). In Resolve open Preferences > System > "
            "General, set 'External scripting using' to Local, save, and restart "
            "Resolve. Until then use --engine ffmpeg."
        )
    if mode is None:
        return (
            "Start Resolve, open any project, and make sure Preferences > System > "
            "General > 'External scripting using' is set to Local."
        )
    return (
        f"External scripting is set to {SCRIPTING_MODES.get(mode, mode)!r}; make sure "
        "Resolve has finished loading and a project (or the Project Manager) is open."
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


def connect():
    """Return the Resolve application object."""
    dvr = _load_module()
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise VideoLyricsError(f"Could not reach DaVinci Resolve. {scripting_hint()}")
    return resolve


def check() -> str:
    """Report whether Resolve is reachable, for `video-lyrics resolve-check`."""
    lines = [
        f"Resolve running : {'yes' if is_running() else 'no'}",
        f"scripting pref  : {SCRIPTING_MODES.get(scripting_preference(), scripting_preference())}",
    ]
    try:
        resolve = connect()
        project = resolve.GetProjectManager().GetCurrentProject()
        lines += [
            f"scripting API   : {resolve.GetProductName()} {resolve.GetVersionString()}",
            f"current project : {project.GetName() if project else '(none open)'}",
            "ready to render : yes",
        ]
    except VideoLyricsError as error:
        lines += ["ready to render : no", f"reason          : {error}"]
    return "\n".join(lines)


# ------------------------------------------------------------------ helpers


def _open_project(resolve, name: str):
    """Reuse the Resolve project of that name, or make one. Never deletes a project."""
    manager = resolve.GetProjectManager()
    manager.GotoRootFolder()
    current = manager.GetCurrentProject()
    if current is not None and current.GetName() == name:
        return manager, current
    project = manager.LoadProject(name) or manager.CreateProject(name)
    if project is None:
        raise VideoLyricsError(f"Could not create or open Resolve project {name!r}.")
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
        if project.GetTimelineByName(candidate) is None:
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
) -> tuple[Any, Any, Any]:
    """Build the timeline. Returns (resolve, project, timeline)."""
    resolve = connect()
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

    existing = project.GetTimelineByName(timeline_name)
    if existing is not None:
        if replace:
            media_pool.DeleteTimelines([existing])
        else:
            # Keep the old edit around and build alongside it.
            timeline_name = _unused_timeline_name(project, timeline_name)
    timeline = media_pool.CreateEmptyTimeline(timeline_name)
    if timeline is None:
        raise VideoLyricsError(f"Could not create timeline {timeline_name!r}.")
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
) -> Path:
    """Queue a single render job for the current timeline and wait for it."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not project.SetCurrentRenderFormatAndCodec(fmt, codec):
        raise VideoLyricsError(
            f"Resolve rejected format/codec {fmt}/{codec}. "
            "Run `video-lyrics resolve-formats` to see what this install offers."
        )
    project.SetCurrentRenderMode(1)  # single clip
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

    job_id = project.AddRenderJob()
    if not job_id:
        raise VideoLyricsError("Resolve refused to queue the render job.")
    log.info("Rendering in DaVinci Resolve -> %s", output)
    if not project.StartRendering([job_id], isInteractiveMode=False):
        raise VideoLyricsError("Resolve could not start rendering.")

    last_progress = -1
    while project.IsRenderingInProgress():
        status = project.GetRenderJobStatus(job_id) or {}
        progress = int(status.get("CompletionPercentage", 0) or 0)
        if progress != last_progress:
            log.info("  render %d%%", progress)
            last_progress = progress
        time.sleep(poll)

    status = project.GetRenderJobStatus(job_id) or {}
    if status.get("JobStatus") not in (None, "Complete"):
        raise VideoLyricsError(f"Render finished with status {status}")

    produced = _find_output(output)
    log.info("Resolve wrote %s", produced)
    return produced


def _find_output(output: Path) -> Path:
    if output.is_file():
        return output
    matches = sorted(output.parent.glob(f"{output.stem}*"))
    if matches:
        return matches[0]
    raise VideoLyricsError(
        f"Render reported success but no file appeared at {output}. "
        "Check the Deliver page for the job's status."
    )


def formats() -> dict[str, Any]:
    """Report the format/codec combinations this Resolve install supports."""
    resolve = connect()
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise VideoLyricsError("Open a project in Resolve first.")
    available = project.GetRenderFormats() or {}
    return {name: project.GetRenderCodecs(extension) for name, extension in available.items()}
