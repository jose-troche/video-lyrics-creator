"""Hand a finished job over to DaVinci Resolve's own script menu.

The free edition of Resolve has no "External scripting using" preference, so
nothing outside Resolve can drive it.  What does work in every edition is a script
placed in Resolve's Fusion/Scripts folder and started from **Workspace > Scripts**:
inside that process the `resolve` object is handed to the script directly.

So the CLI prepares everything (images, overlays, motion, cross dissolves), writes a
pointer to the job, and installs the launcher.  One menu click in Resolve then
builds the timeline and renders.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .util import VideoLyricsError, ensure_dir, log

LAUNCHER_NAME = "Video Lyrics Creator.py"
PACKAGE_ROOT_MARKER = "@PACKAGE_ROOT@"
TEMPLATE = Path(__file__).parent / "data" / "resolve_launcher.py"


JOB_ENV = "VIDEO_LYRICS_JOB"


def job_path() -> Path:
    """Where the staged job lives; $VIDEO_LYRICS_JOB overrides it."""
    override = os.environ.get(JOB_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".video-lyrics" / "staged-job.json"


def scripts_dir() -> Path:
    """Resolve's per-user Fusion scripts folder."""
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve"
    elif sys.platform.startswith("win"):
        base = Path.home() / "AppData/Roaming/Blackmagic Design/DaVinci Resolve/Support"
    else:
        base = Path.home() / ".local/share/DaVinciResolve"
    return base / "Fusion" / "Scripts" / "Utility"


def package_root() -> Path:
    """The directory that has to be on sys.path for `import video_lyrics` to work."""
    return Path(__file__).resolve().parent.parent


def stage(project) -> Path:
    """Record what the Resolve launcher should build next.

    The job carries a full copy of the project data, not just its path: inside
    Resolve the script runs under Resolve's own Python, which has no PyYAML, and
    this job file is plain JSON.  It is a snapshot - edit the project afterwards
    and you need to re-run `video-lyrics render` to re-stage it.
    """
    path = job_path()
    ensure_dir(path.parent)
    payload = {
        "project": str(Path(project.path).resolve()),
        "package_root": str(package_root()),
        "title": project.title,
        "output": str(project.output),
        "staged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": project.data,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.debug("Staged Resolve job at %s", path)
    return path


def load() -> dict:
    path = job_path()
    if not path.is_file():
        raise VideoLyricsError(
            f"No staged job at {path}. Run `video-lyrics render` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def install(target_dir: Path | None = None) -> Path:
    """Copy the launcher into Resolve's script menu folder."""
    if not TEMPLATE.is_file():
        raise VideoLyricsError(f"Launcher template missing: {TEMPLATE}")
    target_dir = Path(target_dir) if target_dir else scripts_dir()
    ensure_dir(target_dir)
    source = TEMPLATE.read_text(encoding="utf-8")
    source = source.replace(PACKAGE_ROOT_MARKER, str(package_root()))
    target = target_dir / LAUNCHER_NAME

    if target.is_file() and target.read_text(encoding="utf-8", errors="replace") != source:
        backup = target.with_suffix(".py.previous")
        if not backup.exists():
            shutil.copy2(target, backup)
            log.info("Kept the launcher that was already there as %s", backup.name)

    target.write_text(source, encoding="utf-8")
    log.info("Installed Resolve launcher: %s", target)
    return target


def is_installed(target_dir: Path | None = None) -> bool:
    target_dir = Path(target_dir) if target_dir else scripts_dir()
    return (target_dir / LAUNCHER_NAME).is_file()


def uninstall(target_dir: Path | None = None) -> bool:
    target_dir = Path(target_dir) if target_dir else scripts_dir()
    target = target_dir / LAUNCHER_NAME
    if target.is_file():
        target.unlink()
        return True
    return False


def instructions(project) -> str:
    return "\n".join(
        [
            "",
            "  Everything is prepared. Finish in DaVinci Resolve:",
            "",
            "    1. Open DaVinci Resolve (any project, or the Project Manager).",
            "    2. Workspace > Scripts > Video Lyrics Creator",
            "",
            f"  It will build the timeline for {project.title!r} and render to:",
            f"    {project.output}",
            "",
            f"  Progress is logged to {project.work_dir / 'resolve-launcher.log'}",
            "",
        ]
    )


def copy_template_for_test(destination: Path) -> Path:
    """Used by the tests to compile the launcher exactly as it is installed."""
    shutil.copy(TEMPLATE, destination)
    return destination
