from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .errors import VideoLyricsError


MACOS_PYTHON_FRAMEWORK_ROOT = Path("/Library/Frameworks/Python.framework/Versions")


def macos_resolve_python_runtimes(
    framework_root: str | Path = MACOS_PYTHON_FRAMEWORK_ROOT,
) -> list[Path]:
    """Return Python.org-style runtimes that Resolve can embed on macOS."""
    root = Path(framework_root)
    if not root.is_dir():
        return []
    return sorted(
        (version / "Python" for version in root.iterdir() if (version / "Python").is_file()),
        key=str,
    )


def require_resolve_python_runtime(
    framework_root: str | Path = MACOS_PYTHON_FRAMEWORK_ROOT,
) -> list[Path]:
    if sys.platform != "darwin":
        return []
    runtimes = macos_resolve_python_runtimes(framework_root)
    if runtimes:
        return runtimes
    raise VideoLyricsError(
        "DaVinci Resolve cannot discover a host Python runtime. The macOS /usr/bin/python3 "
        "command and this project's virtual environment are not embeddable Resolve runtimes. "
        "Install a universal macOS Python from https://www.python.org/downloads/macos/ so that "
        "/Library/Frameworks/Python.framework/Versions/<version>/Python exists, fully quit "
        "Resolve, rerun `video-lyrics install-resolve`, and restart Resolve. Pass "
        "--skip-python-check only if Python scripts already work in Resolve's Py3 Console."
    )


def default_scripts_root() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts"
        )
    if sys.platform.startswith("win"):
        app_data = os.environ.get("APPDATA")
        if not app_data:
            raise VideoLyricsError("APPDATA is not set; pass --target with the Resolve Scripts path")
        return Path(app_data) / "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts"
    return Path.home() / ".local/share/DaVinciResolve/Fusion/Scripts"


def install_workspace_script(
    target: str | Path | None = None,
    *,
    dry_run: bool = False,
    check_python: bool = True,
) -> dict:
    python_runtimes = macos_resolve_python_runtimes() if sys.platform == "darwin" else []
    if check_python and not dry_run and sys.platform == "darwin" and not python_runtimes:
        require_resolve_python_runtime()
    scripts_root = Path(target).expanduser().resolve() if target else default_scripts_root()
    utility_dir = scripts_root / "Utility"
    module_dir = scripts_root / "Modules" / "video_lyrics_creator"
    package_dir = Path(__file__).resolve().parent
    launcher_source = package_dir / "_workspace_launcher.py"
    launcher_target = utility_dir / "Video Lyrics Creator.py"
    files = [path for path in package_dir.glob("*.py") if path.name != "_workspace_launcher.py"]
    if not launcher_source.is_file():
        raise VideoLyricsError(f"Workspace launcher template is missing: {launcher_source}")

    if not dry_run:
        utility_dir.mkdir(parents=True, exist_ok=True)
        module_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(launcher_source, launcher_target)
        for source in files:
            shutil.copy2(source, module_dir / source.name)
    return {
        "scripts_root": str(scripts_root),
        "launcher": str(launcher_target),
        "module_dir": str(module_dir),
        "module_files": len(files),
        "python_runtimes": [str(path) for path in python_runtimes],
        "python_check_skipped": not check_python,
        "dry_run": dry_run,
    }
