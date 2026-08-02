"""Plan the image scenes: which picture is on screen, when, and with what motion."""

from __future__ import annotations

from typing import Any

MOTION_CYCLE = ("zoom_in", "zoom_out", "pan_right", "zoom_in", "pan_left", "zoom_out")

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
) -> list[dict[str, Any]]:
    """Bundle 1-2 consecutive lyric lines per image, breaking on musical gaps."""
    groups: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

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
            if len(current) >= max(1, lines_per_image) or gap > scene_gap:
                flush()
        current.append(cue)
    flush()
    return groups


def plan(
    cues: list[dict[str, Any]],
    *,
    duration: float,
    title: str,
    visual_style: str,
    lines_per_image: int = 2,
    scene_gap: float = 2.5,
    interlude: float = 12.0,
) -> list[dict[str, Any]]:
    """Return scenes covering [0, duration] with no gaps and no overlaps."""
    groups = group_cues(cues, lines_per_image=lines_per_image, scene_gap=scene_gap)

    scenes: list[dict[str, Any]] = []
    if not groups:
        scenes.append(
            {
                "start": 0.0,
                "end": duration,
                "lines": [],
                "prompt": INSTRUMENTAL_TEMPLATE.format(
                    style=visual_style, title=title, context=""
                ),
            }
        )
    else:
        # A long intro before the first sung line gets its own image (the title card
        # sits on top of it).
        if groups[0]["start"] > interlude:
            scenes.append(
                {
                    "start": 0.0,
                    "end": groups[0]["start"],
                    "lines": [],
                    "prompt": INSTRUMENTAL_TEMPLATE.format(
                        style=visual_style,
                        title=title,
                        context=" — the opening, before the first line \""
                        + groups[0]["lines"][0]
                        + "\"",
                    ),
                }
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
            if following - group["end"] > interlude:
                context = " — an instrumental break after \"" + group["lines"][-1] + "\""
                scenes.append(
                    {
                        "start": group["end"],
                        "end": following,
                        "lines": [],
                        "prompt": INSTRUMENTAL_TEMPLATE.format(
                            style=visual_style, title=title, context=context
                        ),
                    }
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
