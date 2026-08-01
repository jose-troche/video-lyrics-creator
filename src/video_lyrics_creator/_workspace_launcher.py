"""DaVinci Resolve 21 Free menu launcher installed under Workspace > Scripts."""

from pathlib import Path
import sys


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
    return Path(__file__).resolve().parents[1] / "Modules"


module_root = _module_root()
if str(module_root) not in sys.path:
    sys.path.insert(0, str(module_root))

from video_lyrics_creator.workspace import run_latest_workspace_job

resolve_object = _resolve_object()
if not resolve_object:
    raise RuntimeError("This script must be launched from DaVinci Resolve: Workspace > Scripts")

print("Video Lyrics Creator: loading the latest staged job...")
workspace_result = run_latest_workspace_job(resolve_object)
print(
    "Video Lyrics Creator: complete — project {!r}, timeline {!r}, output {}".format(
        workspace_result["project_name"],
        workspace_result["timeline_name"],
        workspace_result.get("output") or "timeline only",
    )
)
