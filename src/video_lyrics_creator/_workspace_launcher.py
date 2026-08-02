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


def _status_window(resolve_object):
    """Create a small Resolve-native status window when UIManager is available."""
    try:
        fusion_object = globals().get("fusion") or resolve_object.Fusion()
        bmd_object = globals().get("bmd")
        if not bmd_object:
            import fusionscript as bmd_object

        ui = fusion_object.UIManager
        dispatcher = bmd_object.UIDispatcher(ui)
        window = dispatcher.AddWindow(
            {
                "ID": "VideoLyricsCreatorStatus",
                "Geometry": [240, 180, 620, 260],
                "WindowTitle": "Video Lyrics Creator",
            },
            ui.VGroup(
                {"Spacing": 10, "Margin": 14},
                [
                    ui.Label(
                        {
                            "ID": "StatusText",
                            "Text": "Loading the latest staged job...",
                            "WordWrap": True,
                        }
                    ),
                    ui.TextEdit(
                        {
                            "ID": "DetailsText",
                            "ReadOnly": True,
                            "Text": "Progress is also recorded in resolve-launcher.log.",
                        }
                    ),
                    ui.HGroup(
                        {"Weight": 0},
                        [
                            ui.HGap(0, 1),
                            ui.Button(
                                {
                                    "ID": "CloseButton",
                                    "Text": "Close",
                                    "Enabled": False,
                                }
                            ),
                        ],
                    ),
                ],
            ),
        )

        def close(_event):
            dispatcher.ExitLoop()

        window.On.VideoLyricsCreatorStatus.Close = close
        window.On.CloseButton.Clicked = close
        window.Show()
        window.Raise()
        return dispatcher, window
    except Exception:
        _write_log("Video Lyrics Creator: Resolve UIManager status window unavailable")
        return None


def _update_status(status_ui, message, *, details=None, done=False):
    if not status_ui:
        return
    try:
        _dispatcher, window = status_ui
        window.Find("StatusText").Text = message
        if details is not None:
            window.Find("DetailsText").PlainText = details
        if done:
            window.Find("CloseButton").Enabled = True
        window.Raise()
    except Exception:
        pass


def _wait_for_close(status_ui) -> None:
    if not status_ui:
        return
    try:
        dispatcher, _window = status_ui
        dispatcher.RunLoop()
    except Exception:
        pass


module_root = _module_root()
if str(module_root) not in sys.path:
    sys.path.insert(0, str(module_root))

# Workspace menu scripts can run repeatedly in one Resolve process. Always load the freshly
# installed support modules instead of retaining a failed run's cached package.
for module_name in list(sys.modules):
    if module_name == "video_lyrics_creator" or module_name.startswith("video_lyrics_creator."):
        del sys.modules[module_name]

status_ui = None
try:
    from video_lyrics_creator.workspace import run_latest_workspace_job

    resolve_object = _resolve_object()
    if not resolve_object:
        raise RuntimeError("This script must be launched from DaVinci Resolve: Workspace > Scripts")

    status_ui = _status_window(resolve_object)

    def report_progress(progress_message: str) -> None:
        message = "Video Lyrics Creator: " + progress_message
        print(message)
        _write_log(message)
        _update_status(status_ui, progress_message)

    _write_log("START Video Lyrics Creator workspace launcher")
    print("Video Lyrics Creator: loading the latest staged job...")
    workspace_result = run_latest_workspace_job(resolve_object, progress=report_progress)
    message = "Video Lyrics Creator: complete — project {!r}, timeline {!r}, output {}".format(
        workspace_result["project_name"],
        workspace_result["timeline_name"],
        workspace_result.get("output") or "timeline only",
    )
    print(message)
    _write_log(message)
    _update_status(
        status_ui,
        "Complete",
        details="Project: {}\nTimeline: {}\nOutput: {}".format(
            workspace_result["project_name"],
            workspace_result["timeline_name"],
            workspace_result.get("output") or "timeline only",
        ),
        done=True,
    )
    _wait_for_close(status_ui)
except Exception:
    details = "Video Lyrics Creator launcher failed:\n" + traceback.format_exc()
    print(details)
    _write_log(details)
    _update_status(
        status_ui,
        "Failed — see the details below",
        details=details,
        done=True,
    )
    _wait_for_close(status_ui)
    raise
