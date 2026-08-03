"""The stages, in order, and the glue that runs them.

    lyrics -> transcribe -> align -> tune -> plan -> images -> overlays -> bed -> render

Every stage reads and writes the project file, so any stage can be re-run on its own
and the ones after it pick up the change.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from . import align as align_mod
from . import audio as audio_mod
from . import images as images_mod
from . import lyrics as lyrics_mod
from . import motion as motion_mod
from . import overlays as overlays_mod
from . import render_ffmpeg, scenes as scenes_mod, transcribe as transcribe_mod
from .config import Project
from .util import VideoLyricsError, log

STAGES = ("lyrics", "transcribe", "align", "tune", "plan", "images", "overlays", "bed", "render")


# ------------------------------------------------------------------- stages


def stage_lyrics(project: Project, *, force: bool = False, **_: Any) -> None:
    """Read the reference lyrics and measure the song."""
    lines, section_starts = lyrics_mod.load_lines_with_sections(project.lyrics_source)
    if not lines:
        raise VideoLyricsError(f"No lyric lines found in {project.lyrics_source}")
    project.data["lyric_lines"] = lines
    project.data["lyric_section_starts"] = sorted(section_starts)
    project.lyrics_text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    duration = audio_mod.duration(project.audio)
    project.data["duration"] = round(duration, 4)
    log.info("Lyrics: %d lines. Audio: %.2fs.", len(lines), duration)
    project.save()


def stage_transcribe(project: Project, *, force: bool = False, **_: Any) -> None:
    """Transcribe the song; the transcript decides the timing of everything."""
    settings = project.alignment
    # Feeding the lyrics in as a prompt makes Whisper recite them over the intro, so
    # it is off unless asked for.
    hint = None
    if settings.get("prompt_hint"):
        hint = " ".join(project.data.get("lyric_lines", [])[:8]) or None
    transcript = transcribe_mod.load_or_create(
        project.audio,
        project.transcript_path,
        model=settings["model"],
        language=settings.get("language"),
        initial_prompt=hint,
        vad=bool(settings.get("vad", False)),
        force=force,
    )
    project.data["transcript"] = {
        "path": str(project.transcript_path),
        "model": transcript.get("model"),
        "words": len(transcript.get("words", [])),
    }
    project.save()


def stage_align(project: Project, *, force: bool = False, **_: Any) -> None:
    """Confirm reference lines against the transcript and time them."""
    import json

    tuned = sum(1 for cue in project.cues if cue.get("tuned"))
    if tuned and not force:
        # Re-aligning would throw the hand-tuned timings away, and this stage runs on
        # the way through every `video-lyrics run`. Keeping them is the safe default.
        log.warning(
            "Keeping the existing cues: %d were adjusted by hand in `video-lyrics tune`. "
            "Use --force to align from the transcript again.", tuned,
        )
        return

    if not project.transcript_path.is_file():
        raise VideoLyricsError("No transcript yet. Run `video-lyrics transcribe` first.")
    transcript = json.loads(project.transcript_path.read_text(encoding="utf-8"))
    lines = project.data.get("lyric_lines") or []
    if not lines:
        raise VideoLyricsError("No lyric lines loaded. Run `video-lyrics lyrics` first.")

    settings = project.alignment
    cues = align_mod.align(
        lines,
        transcript.get("words", []),
        duration=project.duration,
        min_confidence=float(settings["min_confidence"]),
        min_matched_words=int(settings.get("min_matched_words", 2)),
        min_duration=float(settings["min_duration"]),
        max_gap_fill=float(settings["max_gap_fill"]),
    )
    project.data["lyrics"] = cues
    log.info("%s", align_mod.report(lines, cues))
    project.save()


def stage_tune(project: Project, *, skip_tune: bool = False, **_: Any) -> None:
    """Offer to open `video-lyrics tune` before the cues are locked into scenes."""
    if skip_tune:
        log.info("Skipping the fine-tuning prompt (--skip-tune).")
        return
    if not project.cues:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        log.debug("Not running interactively - skipping the fine-tuning prompt.")
        return

    answer = input("Fine-tune lyric line timing by ear now? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        return

    from . import tune as tune_mod

    saved = tune_mod.tune(project)
    if saved:
        log.info("Tuning saved: %s.", saved)
    else:
        log.info("No timing changes saved.")


def stage_plan(project: Project, *, force: bool = False, **_: Any) -> None:
    """Decide which image is on screen when."""
    if not project.cues:
        raise VideoLyricsError("No lyric cues. Run `video-lyrics align` first.")
    settings = project.alignment
    planned = scenes_mod.plan(
        project.cues,
        duration=project.duration,
        title=project.title,
        visual_style=project.data["visual_style"],
        lines_per_image=int(project.image_generation.get("lines_per_image", 2)),
        scene_gap=float(settings["scene_gap"]),
        min_scene=float(settings["min_scene_duration"]),
        max_scene=float(settings["max_scene_duration"]),
        section_starts=set(project.data.get("lyric_section_starts", [])),
    )
    if not force:
        planned = scenes_mod.merge_existing_images(planned, project.scenes)
    project.data["scenes"] = planned
    log.info(
        "Planned %d scenes (%d already have images).",
        len(planned),
        sum(1 for scene in planned if scene.get("image")),
    )
    project.save()


def stage_images(
    project: Project, *, force: bool = False, jobs: int = 1, images_dir: str | None = None, **_: Any
) -> None:
    """Generate (or adopt) one still per scene."""
    settings = project.image_generation
    source = images_dir or settings.get("source_dir")
    images_mod.generate(
        project.scenes,
        images_dir=project.images_dir,
        provider="supplied" if source else settings.get("provider", "codex"),
        model=settings.get("model", "gpt-image-2"),
        quality=settings.get("quality", "medium"),
        source_dir=source,
        size=project.size,
        force=force,
        jobs=jobs,
    )
    project.save()


def stage_overlays(project: Project, *, force: bool = False, **_: Any) -> None:
    """Draw the title card, the lyric lines, and write the SRT."""
    video = project.video
    lyric_items = overlays_mod.render_lyrics(
        project.cues,
        directory=project.overlays_dir,
        size=project.size,
        font_name=video["font"],
        font_size=int(video["font_size"]),
        margin_v=int(video["margin_v"]),
        lead=float(video["lyric_lead"]),
        force=force,
    )
    start, end = overlays_mod.title_window(
        project.cues,
        duration=project.duration,
        requested=float(video["title_duration"]),
        fade=float(video["title_fade"]),
        lead=float(video["lyric_lead"]),
    )
    title: dict[str, Any] | None = None
    if end > start:
        title_png = overlays_mod.render_title(
            title=project.title,
            author=project.author,
            directory=project.overlays_dir,
            size=project.size,
            font_name=video["font"],
            font_size=int(video["font_size"]),
            force=force,
        )
        title = {
            "image": str(title_png),
            "start": start,
            "end": end,
            "fade": float(video["title_fade"]),
        }
        log.info("Title card: %.2fs - %.2fs", start, end)
    else:
        log.warning("The first lyric arrives too early for a title card; skipping it.")

    # Bake the fades into alpha movie clips here, while the drawing tools are already
    # in hand: both render engines - and the script that runs inside Resolve - then
    # only ever deal with finished media.
    overlay_clips = project.work_dir / "overlay-clips"
    overlays_mod.bake_items(
        lyric_items,
        directory=overlay_clips,
        fps=project.fps,
        fade=float(video["lyric_fade"]),
        force=force,
    )
    if title:
        overlays_mod.bake_items(
            [title], directory=overlay_clips, fps=project.fps,
            fade=float(video["title_fade"]), force=force,
        )

    overlays_mod.write_srt(project.cues, project.srt_path, lead=float(video["lyric_lead"]))
    project.data["overlays"] = {
        "title": title,
        "lyrics": lyric_items,
        "srt": str(project.srt_path),
    }
    project.save()


def stage_bed(project: Project, *, force: bool = False, **_: Any) -> None:
    """Bake the Ken Burns motion and the cross dissolves."""
    video = project.video
    clips = motion_mod.render_bed(
        project.scenes,
        directory=project.clips_dir,
        size=project.size,
        fps=project.fps,
        duration=project.duration,
        transition=float(video["transition"]),
        zoom=float(video["zoom"]),
        codec=project.render_settings.get("intermediate", "h264"),
        force=force,
    )
    project.data["bed"] = clips
    project.save()


def _resolve_reachable() -> tuple[bool, str]:
    """Can this process drive Resolve directly?"""
    from . import render_resolve

    if not render_resolve.is_running():
        return False, "DaVinci Resolve is not running"
    try:
        render_resolve.connect()
    except VideoLyricsError as error:
        return False, str(error)
    return True, "reachable"


def stage_render(
    project: Project,
    *,
    force: bool = False,
    engine: str | None = None,
    launch: bool = False,
    handoff_only: bool = False,
    **_: Any,
) -> Path | None:
    """Assemble and export the finished video."""
    settings = project.render_settings
    engine = engine or settings.get("engine", "ffmpeg")
    clips = project.data.get("bed")
    if not clips:
        raise VideoLyricsError("No image bed. Run `video-lyrics bed` first.")
    overlay_data = project.data.get("overlays") or {}
    lyric_items = list(overlay_data.get("lyrics", []))
    title_item = overlay_data.get("title")
    output = project.output
    if output.exists() and not settings.get("replace_existing", True):
        raise VideoLyricsError(f"{output} already exists and replace_existing is false.")

    audio = audio_mod.bake_fades(
        project.audio,
        project.faded_audio_path,
        duration=project.duration,
        fade=float(project.video.get("audio_fade", 1.0)),
        force=force,
    )

    if engine == "ffmpeg":
        items = ([title_item] if title_item else []) + lyric_items
        result = render_ffmpeg.render(
            clips=clips,
            overlay_items=items,
            audio=audio,
            output=output,
            work_dir=project.work_dir,
            size=project.size,
            fps=project.fps,
            duration=project.duration,
            fade=float(project.video["lyric_fade"]),
            force=force,
        )
    elif engine == "resolve":
        from . import handoff, render_resolve

        if launch and not render_resolve.is_running():
            try:
                render_resolve.launch_and_wait()
            except VideoLyricsError as error:
                log.debug("Could not wait for Resolve: %s", error)

        if handoff_only:
            reachable, reason = False, "--handoff was requested"
        else:
            reachable, reason = _resolve_reachable()

        if not reachable:
            # The free edition has no external-scripting switch, so the CLI cannot
            # drive Resolve. Everything is prepared; Resolve runs the last step
            # itself from Workspace > Scripts.
            log.info("Driving Resolve from here is not possible (%s).", reason)
            handoff.stage(project)
            handoff.install()
            project.save()
            print(handoff.instructions(project))
            return None

        result = Path(render_resolve.build_and_render(project))
    else:
        raise VideoLyricsError(f"Unknown render engine {engine!r} (use resolve or ffmpeg).")

    project.data["render"]["last_output"] = str(result)
    project.save()
    return result


HANDLERS: dict[str, Callable[..., Any]] = {
    "lyrics": stage_lyrics,
    "transcribe": stage_transcribe,
    "align": stage_align,
    "tune": stage_tune,
    "plan": stage_plan,
    "images": stage_images,
    "overlays": stage_overlays,
    "bed": stage_bed,
    "render": stage_render,
}


def run(
    project: Project,
    *,
    first: str = "lyrics",
    last: str = "render",
    force: bool = False,
    **options: Any,
) -> None:
    """Run a contiguous range of stages."""
    if first not in STAGES or last not in STAGES:
        raise VideoLyricsError(f"Stages must be one of: {', '.join(STAGES)}")
    begin, end = STAGES.index(first), STAGES.index(last)
    if begin > end:
        raise VideoLyricsError(f"--from {first} comes after --to {last}")
    for name in STAGES[begin : end + 1]:
        log.info("── %s ──", name)
        HANDLERS[name](project, force=force, **options)


def status(project: Project) -> str:
    """A short report of what is done and what is not."""
    data = project.data
    overlays = data.get("overlays") or {}
    checks = [
        ("lyrics", bool(data.get("lyric_lines")), f"{len(data.get('lyric_lines', []))} lines"),
        ("transcribe", project.transcript_path.is_file(),
         f"{(data.get('transcript') or {}).get('words', 0)} words"),
        ("align", bool(data.get("lyrics")), f"{len(data.get('lyrics', []))} cues"),
        ("plan", bool(data.get("scenes")), f"{len(data.get('scenes', []))} scenes"),
        ("images", all(scene.get("image") for scene in data.get("scenes", [])) and bool(data.get("scenes")),
         f"{sum(1 for scene in data.get('scenes', []) if scene.get('image'))} images"),
        ("overlays", bool(overlays.get("lyrics")), f"{len(overlays.get('lyrics', []))} overlays"),
        ("bed", bool(data.get("bed")), f"{len(data.get('bed', []))} clips"),
        ("render", bool(data.get("render", {}).get("last_output")),
         str(data.get("render", {}).get("last_output", ""))),
    ]
    lines = [project.describe(), "", "stages:"]
    for name, done, detail in checks:
        lines.append(f"  [{'x' if done else ' '}] {name:<10} {detail}")
    return "\n".join(lines)
