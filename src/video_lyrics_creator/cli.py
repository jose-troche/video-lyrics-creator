from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .alignment import (
    align_lines,
    apply_canonical_lines,
    parse_timing_file,
    read_lyrics,
    transcribe_words,
)
from .errors import VideoLyricsError
from .google_drive import authorize_google_drive
from .handoff import stage_workspace_job
from .images import generate_scene_images, preserve_scene_images_for_replan
from .manifest import load_manifest, new_manifest, save_manifest, validate_manifest
from .media import verify_video
from .overlays import prepare_overlays
from .planning import plan_scenes
from .resolve import timeline_plan
from .workspace_install import install_workspace_script


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-lyrics", description="Automate synchronized lyric videos in DaVinci Resolve"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a project manifest")
    init.add_argument("manifest", nargs="?", default="project.json")
    init.add_argument("--title", required=True)
    init.add_argument("--audio", required=True)
    init.add_argument("--lyrics", required=True)
    init.add_argument("--style", required=True, help="visual style anchor used in every scene prompt")

    prepare = subparsers.add_parser("prepare", help="align lyrics and create a scene plan")
    prepare.add_argument("manifest")
    _alignment_arguments(prepare)

    replan = subparsers.add_parser(
        "replan", help="rebuild lyric-led scenes without retranscribing existing cues"
    )
    replan.add_argument("manifest")
    replan.add_argument("--scene-seconds", type=float, default=7.0)

    images = subparsers.add_parser("images", help="generate all scene images")
    images.add_argument("manifest")
    _image_arguments(images)

    overlays = subparsers.add_parser("overlays", help="render transparent title and lyric overlays")
    overlays.add_argument("manifest")
    overlays.add_argument("--force", action="store_true")

    validate = subparsers.add_parser("validate", help="validate the complete manifest and media")
    validate.add_argument("manifest")

    google_auth = subparsers.add_parser(
        "google-auth", help="authorize private .gdoc lyric access and save the refresh token"
    )
    google_auth.add_argument("--env-file", default=".env")
    google_auth.add_argument("--timeout", type=int, default=300)
    google_auth.add_argument("--no-browser", action="store_true")

    install = subparsers.add_parser(
        "install-resolve", help="install the Resolve 21 Free Workspace Scripts launcher"
    )
    install.add_argument("--target", help="override the user Resolve Fusion/Scripts directory")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument(
        "--skip-python-check",
        action="store_true",
        help="install even if a Resolve-embeddable macOS Python framework is not detected",
    )

    build = subparsers.add_parser(
        "build", help="stage a job for the Resolve 21 Free Workspace Scripts launcher"
    )
    build.add_argument("manifest")
    _resolve_arguments(build)

    verify = subparsers.add_parser("verify", help="probe and validate the rendered video")
    verify.add_argument("manifest")
    verify.add_argument("output", nargs="?")

    run = subparsers.add_parser(
        "run", help="prepare all assets and stage the latest Resolve 21 Free job"
    )
    run.add_argument("manifest")
    _alignment_arguments(run)
    _image_arguments(run)
    _resolve_arguments(run, default_render=True)
    return parser


def _alignment_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timings", help="reviewed .srt or .lrc with one cue per canonical lyric line")
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    parser.add_argument("--scene-seconds", type=float, default=7.0)


def _image_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=("codex", "openai", "command", "placeholder"))
    parser.add_argument(
        "--image-command",
        help="argv template with {prompt_file}, {output}, {width}, and {height} placeholders",
    )
    parser.add_argument("--force-images", action="store_true")


def _resolve_arguments(parser: argparse.ArgumentParser, *, default_render: bool = False) -> None:
    parser.add_argument("--project-name")
    parser.add_argument("--timeline-name")
    parser.add_argument("--replace-timeline", action="store_true")
    parser.add_argument(
        "--handoff-dir", help="sandbox-safe job root; defaults to Movies/Video Lyrics Creator"
    )
    render = parser.add_mutually_exclusive_group()
    render.add_argument("--render", action="store_true", default=default_render)
    render.add_argument("--timeline-only", action="store_false", dest="render")
    parser.add_argument("--dry-run", action="store_true")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _init(args)
        if args.command == "prepare":
            return _prepare(args)
        if args.command == "replan":
            return _replan(args)
        if args.command == "images":
            return _images(args)
        if args.command == "overlays":
            return _overlays(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "google-auth":
            return _google_auth(args)
        if args.command == "install-resolve":
            return _install_resolve(args)
        if args.command == "build":
            return _build(args)
        if args.command == "verify":
            return _verify(args)
        if args.command == "run":
            return _run(args)
    except (VideoLyricsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


def _init(args: argparse.Namespace) -> int:
    path = Path(args.manifest).expanduser().resolve()
    if path.exists():
        raise VideoLyricsError(f"Refusing to overwrite existing manifest: {path}")
    data = new_manifest(
        title=args.title,
        audio=args.audio,
        lyrics_source=args.lyrics,
        visual_style=args.style,
        base=path.parent,
    )
    save_manifest(path, data)
    print(path)
    return 0


def _prepare(args: argparse.Namespace) -> int:
    path, data = load_manifest(args.manifest)
    source_only = {
        **data,
        "lyrics": [],
        "scenes": [],
        "overlays": {"title": "", "lyrics": []},
    }
    duration = validate_manifest(source_only, require_timing=False, require_images=False)
    lines = read_lyrics(data["lyrics_source"], env_dir=path.parent)
    if args.timings:
        cues = apply_canonical_lines(parse_timing_file(args.timings), lines, duration)
    else:
        words = transcribe_words(data["audio"], model=args.whisper_model, device=args.device)
        cues = align_lines(lines, words, duration)
    data["duration"] = round(duration, 6)
    data["lyrics"] = cues
    data["scenes"] = plan_scenes(
        cues,
        duration,
        str(data.get("visual_style", "cinematic realism")),
        args.scene_seconds,
        transition_seconds=float(data["video"].get("transition", 0.75)),
    )
    data["overlays"] = {"title": "", "lyrics": []}
    validate_manifest(data, require_images=False)
    save_manifest(path, data)
    low_confidence = sum(float(cue.get("alignment_confidence", 1)) < 0.6 for cue in cues)
    print(f"Prepared {len(cues)} lyric cues and {len(data['scenes'])} scenes.")
    if not args.timings:
        skipped = len(lines) - len(cues)
        print(
            f"Audio confirmed {len(cues)} of {len(lines)} reference lyric lines; "
            f"skipped {skipped} line(s) with no transcription match."
        )
    if low_confidence:
        print(f"Review recommended: {low_confidence} cue(s) have alignment confidence below 60%.")
    return 0


def _replan(args: argparse.Namespace) -> int:
    path, data = load_manifest(args.manifest)
    data["duration"] = validate_manifest(data, require_images=False)
    previous_scenes = data.get("scenes", [])
    scenes = plan_scenes(
        data["lyrics"],
        float(data["duration"]),
        str(data.get("visual_style", "cinematic realism")),
        args.scene_seconds,
        transition_seconds=float(data["video"].get("transition", 0.75)),
    )
    reused = preserve_scene_images_for_replan(data, previous_scenes, scenes)
    data["scenes"] = scenes
    validate_manifest(data, require_images=False)
    save_manifest(path, data)
    print(
        f"Replanned {len(scenes)} lyric-led scenes; preserved {reused} existing image(s). "
        "Run `video-lyrics images` to generate only the new scenes."
    )
    return 0


def _images(args: argparse.Namespace) -> int:
    path, data = load_manifest(args.manifest)
    data["duration"] = validate_manifest(data, require_images=False)
    count = generate_scene_images(
        data,
        provider=args.provider,
        command_template=args.image_command,
        force=args.force_images,
    )
    save_manifest(path, data)
    print(f"Generated {count} image(s); {len(data['scenes']) - count} reused.")
    return 0


def _overlays(args: argparse.Namespace) -> int:
    path, data = load_manifest(args.manifest)
    data["duration"] = validate_manifest(data, require_images=False)
    count = prepare_overlays(data, force=args.force)
    save_manifest(path, data)
    print(f"Prepared {count} overlay image(s).")
    return 0


def _validate(args: argparse.Namespace) -> int:
    _, data = load_manifest(args.manifest)
    duration = validate_manifest(data)
    print(f"Manifest is valid. Audio duration: {duration:.3f}s")
    return 0


def _google_auth(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise VideoLyricsError("--timeout must be positive")
    result = authorize_google_drive(
        args.env_file,
        timeout=args.timeout,
        open_browser=not args.no_browser,
    )
    print(f"Google Drive authorization complete. Refresh token saved to {result['env_file']}.")
    print(f"Authorized scope: {result['scope']}")
    return 0


def _install_resolve(args: argparse.Namespace) -> int:
    result = install_workspace_script(
        args.target,
        dry_run=args.dry_run,
        check_python=not args.skip_python_check,
    )
    action = "Would install" if args.dry_run else "Installed"
    print(f"{action} Resolve workspace launcher: {result['launcher']}")
    print(f"{action} {result['module_files']} Python module(s): {result['module_dir']}")
    if result["python_runtimes"]:
        print(f"Resolve host Python: {result['python_runtimes'][-1]}")
    elif sys.platform == "darwin":
        print(
            "Resolve host Python: NOT DETECTED under "
            "/Library/Frameworks/Python.framework/Versions"
        )
        if args.skip_python_check:
            print("Warning: the Python launcher may remain hidden until Resolve can load Py3.")
    if not args.dry_run:
        print(
            "Restart Resolve, then use Workspace > Scripts > Video Lyrics Creator "
            "(possibly under Utility)."
        )
    return 0


def _build(args: argparse.Namespace) -> int:
    path, data = load_manifest(args.manifest)
    data["duration"] = validate_manifest(data)
    if args.dry_run:
        print(json.dumps(timeline_plan(data), indent=2, ensure_ascii=False))
        return 0
    project_name = args.project_name or f"{data['title']} - Lyric Video"
    timeline_name = args.timeline_name or data["title"]
    job_path, job = stage_workspace_job(
        path,
        data,
        project_name=project_name,
        timeline_name=timeline_name,
        replace_timeline=args.replace_timeline,
        render=args.render,
        handoff_root=args.handoff_dir,
    )
    data["render"]["output"] = job["manifest"]["render"]["output"]
    save_manifest(path, data)
    print(f"Staged Resolve job: {job_path}")
    print(f"Output: {data['render']['output'] if args.render else 'timeline only'}")
    print(
        "Next: open Resolve and choose Workspace > Scripts > Video Lyrics Creator "
        "(possibly under Utility)."
    )
    if args.render:
        print(f"After Resolve finishes, verify with: video-lyrics verify {path}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    _, data = load_manifest(args.manifest)
    duration = validate_manifest(data)
    output = args.output or data["render"]["output"]
    result = verify_video(output, duration)
    codecs = [
        f"{stream.get('codec_type')}:{stream.get('codec_name')}" for stream in result.get("streams", [])
    ]
    print(f"Verified {Path(output).resolve()} ({result['duration']:.3f}s, {', '.join(codecs)})")
    return 0


def _run(args: argparse.Namespace) -> int:
    path, data = load_manifest(args.manifest)
    if not data.get("lyrics") or not data.get("scenes"):
        result = _prepare(args)
        if result:
            return result
        path, data = load_manifest(path)
    data["duration"] = validate_manifest(data, require_images=False)
    image_count = generate_scene_images(
        data,
        provider=args.provider,
        command_template=args.image_command,
        force=args.force_images,
    )
    overlay_count = prepare_overlays(data, force=args.force_images)
    save_manifest(path, data)
    print(f"Assets ready: {image_count} generated scene image(s), {overlay_count} overlay(s).")
    return _build(args)


if __name__ == "__main__":
    raise SystemExit(main())
