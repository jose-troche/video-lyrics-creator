"""Command line entry point: `video-lyrics`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pipeline
from .config import DEFAULT_AUTHOR, DEFAULT_VISUAL_STYLE, Project, find_project
from .util import VideoLyricsError, load_dotenv, log, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-lyrics",
        description="Create a lyric video from a song and its lyrics.",
    )
    parser.add_argument(
        "-p", "--project",
        help="project file; defaults to ./project.yaml (a project.json is also picked up)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--env", default=".env", help="env file with Google credentials")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a project file")
    init.add_argument("--audio", required=True, help="song audio (wav/mp3/m4a/...)")
    init.add_argument("--lyrics", required=True, help="lyrics .txt or Google Drive .gdoc")
    init.add_argument("--title", help="song title (defaults to the audio filename)")
    init.add_argument("--author", default=DEFAULT_AUTHOR)
    init.add_argument("--style", default=DEFAULT_VISUAL_STYLE, help="visual style for the images")
    init.add_argument("--work-dir", help="where intermediates live (default ./work)")
    init.add_argument("--output", help="final video path")
    init.add_argument("--width", type=int)
    init.add_argument("--height", type=int)
    init.add_argument("--fps", type=float)
    init.add_argument("--font")
    init.add_argument("--font-size", type=int)
    init.add_argument("--whisper-model", help="faster-whisper model, e.g. small.en, medium.en")
    init.add_argument("--images-dir", help="use images from this folder instead of generating")
    init.add_argument("--engine", choices=("resolve", "ffmpeg"))
    init.add_argument(
        "--format", choices=("yaml", "json"), default="yaml",
        help="project file format when --project is not given (default yaml)",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing project file")

    for name, help_text in (
        ("lyrics", "load the reference lyrics and measure the audio"),
        ("transcribe", "transcribe the audio (faster-whisper)"),
        ("align", "confirm lyric lines against the transcript and time them"),
        ("plan", "group cues into image scenes"),
        ("overlays", "draw the title card, lyric overlays and SRT"),
        ("bed", "bake Ken Burns motion and cross dissolves"),
    ):
        stage = subparsers.add_parser(name, help=help_text)
        stage.add_argument("--force", action="store_true", help="redo work even if cached")

    images = subparsers.add_parser("images", help="generate the scene images with codex")
    images.add_argument("--force", action="store_true")
    images.add_argument("--jobs", type=int, default=1, help="parallel codex runs")
    images.add_argument("--images-dir", help="adopt images from this folder instead")

    render = subparsers.add_parser("render", help="assemble and export the video")
    render.add_argument("--engine", choices=("resolve", "ffmpeg"))
    render.add_argument("--launch", action="store_true", help="start DaVinci Resolve if needed")
    render.add_argument(
        "--handoff",
        action="store_true",
        help="always finish from Resolve's Workspace > Scripts menu",
    )
    render.add_argument("--force", action="store_true")

    run = subparsers.add_parser("run", help="run the whole pipeline")
    run.add_argument("--from", dest="first", default="lyrics", choices=pipeline.STAGES)
    run.add_argument("--to", dest="last", default="render", choices=pipeline.STAGES)
    run.add_argument("--engine", choices=("resolve", "ffmpeg"))
    run.add_argument("--launch", action="store_true")
    run.add_argument("--handoff", action="store_true")
    run.add_argument("--jobs", type=int, default=1)
    run.add_argument("--images-dir")
    run.add_argument("--force", action="store_true")

    subparsers.add_parser("status", help="show what is done so far")

    show = subparsers.add_parser("cues", help="print the timed lyric cues")
    show.add_argument("--json", action="store_true")

    subparsers.add_parser(
        "tune", help="hear the song and adjust where each lyric line starts and ends"
    )

    auth = subparsers.add_parser("google-auth", help="one-time Google Drive login")
    auth.add_argument("--no-browser", action="store_true")

    subparsers.add_parser("resolve-formats", help="list DaVinci Resolve render formats/codecs")
    subparsers.add_parser("resolve-check", help="report how this machine can drive Resolve")
    subparsers.add_parser(
        "resolve-install", help="install the Workspace > Scripts launcher into Resolve"
    )
    subparsers.add_parser("resolve-uninstall", help="remove the Resolve launcher")

    setter = subparsers.add_parser("set", help="change a project setting, e.g. set video.zoom 1.12")
    setter.add_argument("key")
    setter.add_argument("value")

    convert = subparsers.add_parser("convert", help="rewrite the project as YAML or JSON")
    convert.add_argument("--to", choices=("yaml", "json"), default="yaml")
    convert.add_argument("--output", help="where to write it (default: same name, new suffix)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv(args.env)

    if args.command == "init":
        project_path = Path(args.project) if args.project else Path(f"project.{args.format}")
    else:
        project_path = find_project(args.project)

    try:
        return dispatch(args, project_path)
    except VideoLyricsError as error:
        log.error("%s", error)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130


def dispatch(args: argparse.Namespace, project_path: Path) -> int:
    command = args.command

    if command == "init":
        return command_init(args, project_path)

    if command == "google-auth":
        from . import google_drive

        token = google_drive.interactive_login(open_browser=not args.no_browser)
        google_drive.store_refresh_token(Path(args.env), token)
        print(f"Saved GOOGLE_DRIVE_REFRESH_TOKEN to {args.env}")
        return 0

    if command == "resolve-formats":
        from . import render_resolve

        print(json.dumps(render_resolve.formats(), indent=2))
        return 0

    if command == "resolve-check":
        from . import render_resolve

        print(render_resolve.check())
        return 0

    if command == "resolve-install":
        from . import handoff

        target = handoff.install()
        print(f"Installed {target}")
        print("Restart DaVinci Resolve, then look under Workspace > Scripts.")
        return 0

    if command == "resolve-uninstall":
        from . import handoff

        print("Removed the launcher." if handoff.uninstall() else "Nothing to remove.")
        return 0

    project = Project.load(project_path)

    if command == "status":
        print(pipeline.status(project))
        return 0

    if command == "cues":
        return command_cues(project, as_json=args.json)

    if command == "tune":
        return command_tune(project)

    if command == "set":
        return command_set(project, args.key, args.value)

    if command == "convert":
        return command_convert(project, target=args.to, output=args.output)

    options = {
        "force": getattr(args, "force", False),
        "jobs": getattr(args, "jobs", 1),
        "images_dir": getattr(args, "images_dir", None),
        "engine": getattr(args, "engine", None),
        "launch": getattr(args, "launch", False),
        "handoff_only": getattr(args, "handoff", False),
    }

    if command == "run":
        pipeline.run(project, first=args.first, last=args.last, **options)
        return 0

    handler = pipeline.HANDLERS.get(command)
    if handler is None:
        raise VideoLyricsError(f"Unknown command {command!r}")
    handler(project, **options)
    return 0


def command_init(args: argparse.Namespace, project_path: Path) -> int:
    if project_path.exists() and not args.force:
        raise VideoLyricsError(f"{project_path} already exists (use --force to replace it).")
    project = Project.create(
        project_path,
        audio=args.audio,
        lyrics_source=args.lyrics,
        title=args.title,
        author=args.author,
        visual_style=args.style,
        work_dir=args.work_dir,
        output=args.output,
    )
    video = project.video
    for key, value in (
        ("width", args.width), ("height", args.height), ("fps", args.fps),
        ("font", args.font), ("font_size", args.font_size),
    ):
        if value is not None:
            video[key] = value
    if args.whisper_model:
        project.alignment["model"] = args.whisper_model
    if args.images_dir:
        project.image_generation["provider"] = "supplied"
        project.image_generation["source_dir"] = str(Path(args.images_dir).expanduser())
    if args.engine:
        project.render_settings["engine"] = args.engine
    project.save()
    print(f"Wrote {project_path}\n")
    print(project.describe())
    return 0


def command_convert(project: Project, *, target: str, output: str | None) -> int:
    destination = Path(output) if output else project.path.with_suffix(f".{target}")
    if destination.resolve() == project.path.resolve():
        print(f"{project.path} is already {target}.")
        return 0
    project.save(destination)
    print(f"Wrote {destination}")
    print(f"{project.path} was left in place; delete it once you are happy.")
    return 0


def command_cues(project: Project, *, as_json: bool) -> int:
    cues = project.cues
    if as_json:
        print(json.dumps(cues, indent=2, ensure_ascii=False))
        return 0
    from .util import human_time

    for index, cue in enumerate(cues, start=1):
        print(
            f"{index:3d} {'*' if cue.get('tuned') else ' '} "
            f"{human_time(cue['start'])} → {human_time(cue['end'])}  "
            f"[{cue.get('alignment_confidence', 1):.2f}]  {cue['text']}"
        )
    tuned = sum(1 for cue in cues if cue.get("tuned"))
    print(f"\n{len(cues)} cues confirmed by the audio.")
    if tuned:
        print(f"{tuned} marked * were adjusted by hand in `video-lyrics tune`.")
    return 0


def command_tune(project: Project) -> int:
    from . import tune as tune_mod

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise VideoLyricsError("`video-lyrics tune` needs a terminal to draw in.")
    saved = tune_mod.tune(project)
    if not saved:
        print("Nothing was saved; the timing is as it was.")
        return 0
    print(f"Saved: {saved}.")
    print("Rebuild the video with the new timing:\n  video-lyrics run --from plan")
    return 0


def command_set(project: Project, key: str, value: str) -> int:
    node = project.data
    parts = key.split(".")
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise VideoLyricsError(f"No such setting group: {part}")
        node = node[part]
    leaf = parts[-1]
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    node[leaf] = parsed
    project.save()
    print(f"{key} = {json.dumps(parsed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
