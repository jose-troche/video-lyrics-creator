"""DaVinci Resolve 21 Free menu launcher installed under Workspace > Scripts."""

from pathlib import Path
import sys
import traceback


# Resolve executes menu scripts with ``exec`` and may not define ``__file__``.
# The installer replaces this with the absolute Modules directory.
_INSTALLED_MODULE_ROOT = None


def _resolve_object():
    current = globals().get("resolve")
    if current:
        return current
    current_app = globals().get("app")
    if current_app:
        try:
            current = current_app.GetResolve()
            if current:
                return current
        except Exception:
            pass
    try:
        import __main__

        current = getattr(__main__, "resolve", None)
        if current:
            return current
        current_app = getattr(__main__, "app", None)
        if current_app:
            current = current_app.GetResolve()
            if current:
                return current
    except Exception:
        pass
    try:
        import DaVinciResolveScript as dvr_script

        return dvr_script.scriptapp("Resolve")
    except Exception:
        return None


def _module_root() -> Path:
    source = globals().get("__file__")
    if source:
        return Path(source).resolve().parents[1] / "Modules"
    if _INSTALLED_MODULE_ROOT:
        return Path(_INSTALLED_MODULE_ROOT)
    raise RuntimeError("Resolve did not provide a launcher path or installed module directory")


def _log_path() -> Path:
    folder = "Movies" if sys.platform == "darwin" else "Videos"
    return Path.home() / folder / "Video Lyrics Creator" / "resolve-launcher.log"


def _write_log(message: str) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(message.rstrip() + "\n")
    except Exception:
        # Logging must never mask the original Resolve script error.
        pass


module_root = _module_root()
if str(module_root) not in sys.path:
    sys.path.insert(0, str(module_root))

try:
    from video_lyrics_creator.workspace import run_latest_workspace_job

    resolve_object = _resolve_object()
    if not resolve_object:
        raise RuntimeError("This script must be launched from DaVinci Resolve: Workspace > Scripts")

    _write_log("START Video Lyrics Creator workspace launcher")
    print("Video Lyrics Creator: loading the latest staged job...")
    workspace_result = run_latest_workspace_job(resolve_object)
    message = "Video Lyrics Creator: complete — project {!r}, timeline {!r}, output {}".format(
        workspace_result["project_name"],
        workspace_result["timeline_name"],
        workspace_result.get("output") or "timeline only",
    )
    print(message)
    _write_log(message)
except Exception:
    details = "Video Lyrics Creator launcher failed:\n" + traceback.format_exc()
    print(details)
    _write_log(details)
    raise
