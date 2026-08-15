"""The stages, in order, and the glue that runs them.

    lyrics -> transcribe -> align -> tune -> plan -> images -> overlays -> bed -> render

Every stage reads and writes the project file, so any stage can be re-run on its own
and the ones after it pick up the change.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import align as align_mod
from . import audio as audio_mod
from . import images as images_mod
from . import lyrics as lyrics_mod
from . import motion as motion_mod
from . import overlays as overlays_mod
from . import render_ffmpeg, scenes as scenes_mod, transcribe as transcribe_mod
from .config import Project
from .util import VideoLyricsError, log, short_hash

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


def _listening_to(project: Project) -> Path:
    """The audio the timing is read from: the isolated vocal, or the song as it is.

    Only the listening stages ever ask; the render always uses the real mix.  The
    stem is separated once and then kept - `--force` on a stage means redo that
    stage's own work, and separating a file again only ever produces the same file.
    Delete it to have it made afresh.
    """
    if not project.alignment.get("vocals"):
        return project.audio
    from . import vocals as vocals_mod

    if not vocals_mod.available() and not project.vocals_path.is_file():
        log.warning(
            "Isolating the vocal needs demucs (pip install -e '.[vocals]'); "
            "listening to the whole mix instead."
        )
        return project.audio
    return vocals_mod.isolate(project.audio, project.vocals_path)


def _engine(project: Project) -> str:
    """Which engine will actually run: what the song asks for, or what is installed.

    Both optional extras are opt-in, and a new song asks for forced alignment out of
    the box, so this is the difference between a clear line in the log and a stack
    trace on a fresh clone.  The fallback is always the transcript, which needs
    nothing beyond the base install and holds up on a full mix - which forced
    alignment, measurably, does not: without the isolated voice it does worse than
    the transcript it would be replacing, so a missing demucs falls all the way back
    rather than settling for half the feature.
    """
    settings = project.alignment
    engine = settings.get("engine", "whisper")
    if engine not in ("whisper", "forced"):
        raise VideoLyricsError(
            f"Unknown alignment.engine {engine!r}; it is either 'whisper' or 'forced'."
        )
    if engine == "whisper":
        return engine

    from . import forced as forced_mod
    from . import vocals as vocals_mod

    if not forced_mod.available():
        log.warning(
            "Forced alignment needs torch and transformers (pip install -e '.[align]'); "
            "timing this song from a transcript instead."
        )
        return "whisper"
    if settings.get("vocals") and not vocals_mod.available() and not project.vocals_path.is_file():
        log.warning(
            "Forced alignment wants the isolated voice and demucs is not installed "
            "(pip install -e '.[vocals]'). Over a full mix it reads worse than a "
            "transcript does, so this song is timed from a transcript instead."
        )
        return "whisper"
    return engine


def stage_transcribe(project: Project, *, force: bool = False, **_: Any) -> None:
    """Work out when each word is sung; everything downstream is timed from this."""
    settings = project.alignment
    engine = _engine(project)
    listening = _listening_to(project)

    if engine == "forced":
        transcript = _forced_transcript(project, listening, force=force)
    else:
        # Feeding the lyrics in as a prompt makes Whisper recite them over the intro, so
        # it is off unless asked for.
        hint = None
        if settings.get("prompt_hint"):
            hint = " ".join(project.data.get("lyric_lines", [])[:8]) or None
        transcript = transcribe_mod.load_or_create(
            listening,
            project.transcript_path,
            signature={
                "engine": "whisper",
                "model": settings["model"],
                # What it actually heard, which is not always what was asked for.
                "vocals": listening != project.audio,
            },
            model=settings["model"],
            language=settings.get("language"),
            initial_prompt=hint,
            vad=bool(settings.get("vad", False)),
            force=force,
        )

    project.data["transcript"] = {
        "path": str(project.transcript_path),
        "engine": transcript.get("engine", "whisper"),
        "model": transcript.get("model"),
        "words": len(transcript.get("words", [])),
    }
    project.save()


def _forced_transcript(project: Project, listening: Path, *, force: bool = False) -> dict[str, Any]:
    """Time the reference lyrics directly against the audio (see `forced`)."""
    from . import forced as forced_mod

    settings = project.alignment
    lines = project.data.get("lyric_lines") or []
    if not lines:
        raise VideoLyricsError(
            "Forced alignment needs the lyrics it is aligning. Run `video-lyrics lyrics` first."
        )

    if listening == project.audio:
        # Turned off deliberately - `_engine` has already sent the case where demucs is
        # simply missing back to the transcript. Measured on a full mix: half the lines
        # come back unusable and some land in the wrong verse, because the model is
        # listening for consonants and a band plays straight over them.
        log.warning(
            "Forced alignment is reading the whole mix. It is markedly better on the "
            "voice alone: video-lyrics set alignment.vocals true"
        )

    model = str(settings.get("forced_model", forced_mod.DEFAULT_MODEL))
    # The lyrics are half of the input here, so a changed lyric sheet has to invalidate
    # the cache as surely as a changed model would.
    signature = {
        "engine": "forced",
        "model": model,
        "vocals": listening != project.audio,
        "lyrics_fingerprint": short_hash(*lines),
    }
    cached = transcribe_mod.load(project.transcript_path, signature=signature, force=force)
    if cached is not None:
        return cached

    payload = forced_mod.align_words(
        listening,
        lines,
        model=model,
        min_score=float(settings.get("forced_min_score", 0.05)),
    )
    payload.update(signature)
    return transcribe_mod.store(project.transcript_path, payload)


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
    tail_extend = float(settings.get("tail_extend", 0.0))
    cues = align_mod.align(
        lines,
        transcript.get("words", []),
        duration=project.duration,
        min_confidence=float(settings["min_confidence"]),
        min_matched_words=int(settings.get("min_matched_words", 2)),
        min_duration=float(settings["min_duration"]),
        max_gap_fill=float(settings["max_gap_fill"]),
        energy=audio_mod.envelope(_listening_to(project)) if tail_extend > 0 else None,
        tail_extend=tail_extend,
        tail_level=float(settings.get("tail_level", 0.45)),
        rescue=transcript.get("engine", "whisper") != "forced",
    )
    project.data["lyrics"] = cues
    log.info("%s", align_mod.report(lines, cues))
    project.save()


def stage_tune(project: Project, *, tune: bool = False, **_: Any) -> None:
    """Offer to open `video-lyrics tune` before the cues are locked into scenes.

    Only when asked for: a `run` walks straight past this unless `--tune` says
    otherwise, so an unattended run never stops on a question. Tuning by ear is
    still there any time you want it, as its own `video-lyrics tune` command.
    """
    if not tune:
        log.info("Skipping the fine-tuning prompt (pass --tune to be asked).")
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


def stage_plan(
    project: Project, *, force: bool = False, lines_per_image: int | None = None, **_: Any
) -> None:
    """Decide which image is on screen when."""
    if not project.cues:
        raise VideoLyricsError("No lyric cues. Run `video-lyrics align` first.")
    settings = project.alignment
    if lines_per_image is not None:
        # Kept, not just used for this run: the scenes about to be written were
        # grouped this way, and a project file that still said otherwise would be
        # describing a plan it did not produce.
        project.image_generation["lines_per_image"] = int(lines_per_image)
    planned = scenes_mod.plan(
        project.cues,
        duration=project.duration,
        title=project.title,
        visual_style=project.data["visual_style"],
        context=str(project.data.get("context") or ""),
        lines_per_image=int(project.image_generation.get("lines_per_image", 1)),
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


# The Codex CLI's image_gen tool used to be the default provider; chatgpt.com
# (same account, one fewer thing to install) took its place. Old project files
# still say `codex`, and their images are already on disk, so this quietly reads
# it as the provider that replaced it rather than refusing to run.
RETIRED_PROVIDERS = {"codex": "chatgpt"}


def _image_provider(settings: dict[str, Any]) -> str:
    provider = settings.get("provider", "chatgpt")
    replacement = RETIRED_PROVIDERS.get(provider)
    if replacement:
        log.warning(
            "image_generation.provider %r is gone; using %r instead. Scenes that "
            "already have an image keep it. Make it permanent with "
            "`video-lyrics set image_generation.provider %s`.",
            provider, replacement, replacement,
        )
        return replacement
    return provider


def _browser_options(settings: dict[str, Any], provider: str) -> dict[str, Any]:
    """The active provider's own settings, with its prefix stripped off.

    `meta_min_delay` becomes `min_delay` when meta is the provider, and is simply
    not looked at when it is not.
    """
    prefix = f"{provider}_"
    return {
        key[len(prefix):]: value
        for key, value in settings.items()
        if key.startswith(prefix)
    }


def _notice_old_raw_dir(work_dir: Path) -> None:
    """Say something about a leftover `images.src/` from before the two merged.

    It used to hold every download in the format the site served, and `images/` a
    converted, upscaled copy of each. `images/` no longer upscales, so it now holds
    what `images.src/` used to and the split has nothing left to do. The old folder
    is not deleted for you: for songs generated before this, its files are the
    smaller originals and only their owner can say whether they are worth keeping.
    """
    stale = work_dir / "images.src"
    if not stale.is_dir():
        return
    size = sum(path.stat().st_size for path in stale.rglob("*") if path.is_file())
    log.info(
        "%s is no longer used - images/ now keeps the downloads themselves "
        "(%.0f MB to reclaim: rm -rf %s).", stale, size / 1e6, stale,
    )


def stage_images(
    project: Project, *, force: bool = False, images_dir: str | None = None,
    limit: int | None = None, scene: Sequence[int] | None = None, **_: Any
) -> None:
    """Generate (or adopt) one still per scene."""
    settings = project.image_generation
    source = images_dir or settings.get("source_dir")
    provider = "supplied" if source else _image_provider(settings)
    if scene:
        known = {int(item["index"]) for item in project.scenes}
        unknown = sorted(set(scene) - known)
        if unknown:
            raise VideoLyricsError(
                f"No scene {', '.join(str(index) for index in unknown)} in this song "
                f"(it has {len(known)}). `video-lyrics cues` lists them."
            )
    _notice_old_raw_dir(project.work_dir)
    images_mod.generate(
        project.scenes,
        images_dir=project.images_dir,
        provider=provider,
        source_dir=source,
        size=project.size,
        force=force,
        limit=limit,
        redraw=scene,
        browser=_browser_options(settings, provider),
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
    transcript = data.get("transcript") or {}
    checks = [
        ("lyrics", bool(data.get("lyric_lines")), f"{len(data.get('lyric_lines', []))} lines"),
        ("transcribe", project.transcript_path.is_file(),
         f"{transcript.get('words', 0)} words ({transcript.get('engine', 'whisper')})"),
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
