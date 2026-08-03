"""Plan the image scenes: which picture is on screen, when, and with what motion."""

from __future__ import annotations

import math
from typing import Any

MOTION_CYCLE = ("zoom_in", "zoom_out", "pan_right", "zoom_in", "pan_left", "zoom_out")

MIN_SCENE_DURATION = 4.0    # no image is ever shown for less than this
MAX_SCENE_DURATION = 15.0   # ... or for longer than this

PROMPT_TEMPLATE = (
    "{style}. Create a cinematic lyric-video scene inspired by this passage: "
    "'{passage}'. Express its meaning and emotion through a coherent visual metaphor; "
    "intentional composition, strong subject separation, atmospheric lighting, "
    "consistent palette and era, landscape 16:9 framing with safe space near the lower "
    "third. No words, letters, captions, logos, watermarks, borders, or typography."
)

INSTRUMENTAL_TEMPLATE = (
    "{style}. Create a cinematic lyric-video scene for an instrumental passage of the "
    "song \"{title}\"{context}. Wide, atmospheric, no people speaking; "
    "intentional composition, atmospheric lighting, consistent palette and era, "
    "landscape 16:9 framing with safe space near the lower third. "
    "No words, letters, captions, logos, watermarks, borders, or typography."
)


def group_cues(
    cues: list[dict[str, Any]],
    *,
    lines_per_image: int = 2,
    scene_gap: float = 2.5,
    min_scene: float = MIN_SCENE_DURATION,
    max_scene: float = MAX_SCENE_DURATION,
) -> list[dict[str, Any]]:
    """Bundle 1-2 consecutive lyric lines per image.

    How many lines share an image is decided by how long they run, not a fixed
    count: a line short enough on its own to fall under `min_scene` picks up its
    neighbour, and a pair that would together run past `max_scene` splits back
    into two - unless the first line is still short of `min_scene` even alone,
    in which case an overlong image beats one that barely shows at all. A gap
    wider than `scene_gap` always starts a new image regardless; it belongs to
    the next phrase, not this one.
    """
    groups: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def own_span() -> float:
        return current[-1]["end"] - current[0]["start"]

    def flush() -> None:
        if current:
            groups.append(
                {
                    "start": current[0]["start"],
                    "end": current[-1]["end"],
                    "lines": [cue["text"] for cue in current],
                }
            )
            current.clear()

    for cue in cues:
        if current:
            gap = cue["start"] - current[-1]["end"]
            candidate_span = cue["end"] - current[0]["start"]
            if (
                len(current) >= max(1, lines_per_image)
                or gap > scene_gap
                or (candidate_span > max_scene and own_span() >= min_scene)
            ):
                flush()
        current.append(cue)
    flush()
    return groups


def _needs_own_scene(gap: float, natural_duration: float, *, max_scene: float) -> bool:
    """Would silently absorbing this whole gap into the neighbouring image push it
    past `max_scene`? If so, the gap is long enough to deserve its own image
    instead of just holding the neighbouring one on screen."""
    return gap > max(0.0, max_scene - natural_duration)


def _split_evenly(
    start: float,
    end: float,
    *,
    max_scene: float = MAX_SCENE_DURATION,
    min_scene: float = MIN_SCENE_DURATION,
) -> list[tuple[float, float]]:
    """Divide a stretch of instrumental time into a few evenly-sized images.

    One piece if it already fits inside `max_scene`; otherwise as few equal
    pieces as it takes to bring each one back under it, without leaving any
    piece shorter than `min_scene` if that can be avoided.
    """
    length = end - start
    if length <= max_scene:
        return [(start, end)]
    count = math.ceil(length / max_scene)
    while count > 1 and length / count < min_scene:
        count -= 1
    piece = length / count
    return [(start + i * piece, start + (i + 1) * piece) for i in range(count)]


def _instrumental_scenes(
    start: float,
    end: float,
    *,
    context: str,
    title: str,
    visual_style: str,
    max_scene: float,
    min_scene: float,
) -> list[dict[str, Any]]:
    pieces = _split_evenly(start, end, max_scene=max_scene, min_scene=min_scene)
    scenes = []
    for index, (piece_start, piece_end) in enumerate(pieces):
        suffix = f" (part {index + 1} of {len(pieces)})" if len(pieces) > 1 else ""
        scenes.append(
            {
                "start": piece_start,
                "end": piece_end,
                "lines": [],
                "prompt": INSTRUMENTAL_TEMPLATE.format(
                    style=visual_style, title=title, context=context + suffix
                ),
            }
        )
    return scenes


def plan(
    cues: list[dict[str, Any]],
    *,
    duration: float,
    title: str,
    visual_style: str,
    lines_per_image: int = 2,
    scene_gap: float = 2.5,
    min_scene: float = MIN_SCENE_DURATION,
    max_scene: float = MAX_SCENE_DURATION,
) -> list[dict[str, Any]]:
    """Return scenes covering [0, duration] with no gaps and no overlaps."""
    groups = group_cues(
        cues, lines_per_image=lines_per_image, scene_gap=scene_gap,
        min_scene=min_scene, max_scene=max_scene,
    )

    scenes: list[dict[str, Any]] = []
    if not groups:
        scenes.extend(
            _instrumental_scenes(
                0.0, duration, context="", title=title, visual_style=visual_style,
                max_scene=max_scene, min_scene=min_scene,
            )
        )
    else:
        # A lead-in too long to just hold the first image back over gets its own
        # image(s) instead (the title card sits on top of it).
        lead = groups[0]["start"]
        first_natural = groups[0]["end"] - groups[0]["start"]
        if lead > 0 and _needs_own_scene(lead, first_natural, max_scene=max_scene):
            context = (
                " — the opening, before the first line \"" + groups[0]["lines"][0] + "\""
            )
            scenes.extend(
                _instrumental_scenes(
                    0.0, lead, context=context, title=title, visual_style=visual_style,
                    max_scene=max_scene, min_scene=min_scene,
                )
            )

        for index, group in enumerate(groups):
            scenes.append(
                {
                    "start": group["start"],
                    "end": group["end"],
                    "lines": list(group["lines"]),
                    "prompt": PROMPT_TEMPLATE.format(
                        style=visual_style, passage=" / ".join(group["lines"])
                    ),
                }
            )
            following = groups[index + 1]["start"] if index + 1 < len(groups) else duration
            gap = following - group["end"]
            natural = group["end"] - group["start"]
            if gap > 0 and _needs_own_scene(gap, natural, max_scene=max_scene):
                # Give this image just enough of the gap's front to reach
                # `min_scene` itself, then treat what's left as dedicated
                # instrumental time - so a short line right before a long
                # instrumental break still gets a decent look.
                donation = max(0.0, min(gap, min_scene - natural))
                block_start = group["end"] + donation
                if block_start < following - 0.05:
                    context = (
                        " — an instrumental break after \"" + group["lines"][-1] + "\""
                    )
                    scenes.extend(
                        _instrumental_scenes(
                            block_start, following, context=context, title=title,
                            visual_style=visual_style, max_scene=max_scene,
                            min_scene=min_scene,
                        )
                    )

    # Stretch every scene so the image bed covers the whole song with no gaps.
    scenes[0]["start"] = 0.0
    for index, scene in enumerate(scenes):
        scene["end"] = scenes[index + 1]["start"] if index + 1 < len(scenes) else duration
    scenes = [scene for scene in scenes if scene["end"] - scene["start"] > 0.05]
    if not scenes:  # a song shorter than a single scene
        scenes = [{"start": 0.0, "end": duration, "lines": [], "prompt": PROMPT_TEMPLATE.format(
            style=visual_style, passage=cues[0]["text"] if cues else title)}]
    scenes[-1]["end"] = duration

    for index, scene in enumerate(scenes):
        scene["index"] = index + 1
        scene["motion"] = MOTION_CYCLE[index % len(MOTION_CYCLE)]
        scene["start"] = round(scene["start"], 3)
        scene["end"] = round(scene["end"], 3)
    return scenes


def merge_existing_images(
    new_scenes: list[dict[str, Any]], old_scenes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Carry image files over to a re-plan when the prompt is unchanged."""
    by_prompt = {
        scene.get("prompt"): scene.get("image")
        for scene in old_scenes
        if scene.get("image")
    }
    for scene in new_scenes:
        image = by_prompt.get(scene.get("prompt"))
        if image:
            scene["image"] = image
    return new_scenes
