"""Video Lyrics Creator - run from DaVinci Resolve: Workspace > Scripts.

Installed by `video-lyrics resolve-install`.  Resolve executes this file inside its
own process, where the `resolve` object is handed to the script directly, so this
works on the free edition, which has no external-scripting preference.

It reads the job staged by the CLI (~/.video-lyrics/staged-job.json), builds the
timeline from the prepared media, and renders.
"""

import json
import os
import sys
import traceback
from pathlib import Path

# Replaced with the checkout path when the launcher is installed.
PACKAGE_ROOT = "@PACKAGE_ROOT@"
JOB_FILE = Path(
    os.environ.get("VIDEO_LYRICS_JOB") or Path.home() / ".video-lyrics" / "staged-job.json"
).expanduser()


def find_resolve():
    """Resolve hands the app object to menu scripts; fall back to the module API."""
    candidate = globals().get("resolve")
    if candidate:
        return candidate
    try:
        import __main__

        candidate = getattr(__main__, "resolve", None)
        if candidate:
            return candidate
        app = getattr(__main__, "app", None) or globals().get("app")
        if app is not None:
            candidate = app.GetResolve()
            if candidate:
                return candidate
    except Exception:
        pass
    try:
        import DaVinciResolveScript

        return DaVinciResolveScript.scriptapp("Resolve")
    except Exception:
        return None


def load_job():
    if not JOB_FILE.is_file():
        raise RuntimeError(
            "No staged job found at {}.\n"
            "Run `video-lyrics render` in the terminal first - it prepares the media "
            "and stages the job for Resolve.".format(JOB_FILE)
        )
    return json.loads(JOB_FILE.read_text(encoding="utf-8"))


def add_package_to_path(job):
    for root in (job.get("package_root"), PACKAGE_ROOT):
        if root and root != "@PACKAGE" + "_ROOT@" and Path(root).is_dir():
            if root not in sys.path:
                sys.path.insert(0, root)
            return root
    raise RuntimeError(
        "Cannot find the video_lyrics checkout. Re-run `video-lyrics resolve-install`."
    )


def fresh_import():
    """Resolve keeps one interpreter alive, so drop any previously loaded copy."""
    for name in [n for n in sys.modules if n == "video_lyrics" or n.startswith("video_lyrics.")]:
        del sys.modules[name]
    from video_lyrics import render_resolve
    from video_lyrics.config import Project

    return Project, render_resolve


class Reporter:
    """Prints to the Console, appends to a log file, and updates a small window."""

    def __init__(self, log_path=None):
        self.log_path = Path(log_path) if log_path else None
        self.window = None
        self.dispatcher = None
        self.lines = []

    def attach_log(self, log_path):
        self.log_path = Path(log_path)

    def open_window(self, resolve_object):
        try:
            fusion = globals().get("fusion") or resolve_object.Fusion()
            bmd = globals().get("bmd")
            if bmd is None:
                import fusionscript as bmd
            ui = fusion.UIManager
            self.dispatcher = bmd.UIDispatcher(ui)
            self.window = self.dispatcher.AddWindow(
                {
                    "ID": "VideoLyricsCreator",
                    "WindowTitle": "Video Lyrics Creator",
                    "Geometry": [200, 200, 680, 320],
                },
                ui.VGroup(
                    {"Spacing": 8, "Margin": 12},
                    [
                        ui.Label({"ID": "Status", "Text": "Starting...", "WordWrap": True}),
                        ui.TextEdit({"ID": "Detail", "ReadOnly": True, "Text": ""}),
                        ui.HGroup(
                            {"Weight": 0},
                            [ui.HGap(0, 1), ui.Button({"ID": "Close", "Text": "Close"})],
                        ),
                    ],
                ),
            )

            def close(_event):
                self.dispatcher.ExitLoop()

            self.window.On.VideoLyricsCreator.Close = close
            self.window.On.Close.Clicked = close
            self.window.Show()
        except Exception:
            self.window = None
            self.dispatcher = None

    def __call__(self, message):
        text = "Video Lyrics Creator: {}".format(message)
        print(text)
        self.lines.append(message)
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(text + "\n")
            except Exception:
                pass
        if self.window is not None:
            try:
                self.window.Find("Status").Text = message
                self.window.Find("Detail").PlainText = "\n".join(self.lines[-14:])
            except Exception:
                pass

    def finish(self, message):
        self(message)
        if self.dispatcher is not None:
            try:
                self.dispatcher.RunLoop()
            except Exception:
                pass


def main():
    reporter = Reporter()
    try:
        resolve_object = find_resolve()
        if resolve_object is None:
            raise RuntimeError(
                "This script has to be started from inside DaVinci Resolve: "
                "Workspace > Scripts > Video Lyrics Creator"
            )
        reporter.open_window(resolve_object)

        job = load_job()
        add_package_to_path(job)
        Project, render_resolve = fresh_import()

        # The job carries the project data inline, so nothing here has to parse
        # YAML with whatever Python Resolve happens to be running.
        if job.get("data"):
            project = Project(Path(job["project"]), job["data"])
        else:
            project = Project.load(job["project"])
        reporter.attach_log(project.work_dir / "resolve-launcher.log")
        reporter("building {!r}".format(project.title))

        result = render_resolve.build_and_render(
            project, resolve=resolve_object, progress=reporter
        )
        reporter.finish("done - rendered {}".format(result))
    except Exception:
        detail = traceback.format_exc()
        print(detail)
        reporter.finish("failed:\n{}".format(detail))
        raise


main()
