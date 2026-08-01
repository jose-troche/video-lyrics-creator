from __future__ import annotations


def plan_scenes(
    lyrics: list[dict],
    duration: float,
    visual_style: str,
    target_seconds: float = 7.0,
    max_seconds: float = 10.0,
    transition_seconds: float = 0.75,
) -> list[dict]:
    if not lyrics:
        return [
            {
                "start": 0.0,
                "end": round(duration, 3),
                "image": "",
                "motion": "zoom_in",
                "prompt": _prompt("instrumental opening", visual_style),
            }
        ]

    minimum_scene_seconds = max(0.05, max(0.0, transition_seconds) * 2 + 0.05)
    boundaries: list[tuple[float, float, list[str]]] = []
    scene_start = 0.0
    texts: list[str] = []
    for index, cue in enumerate(lyrics):
        texts.append(str(cue["text"]))
        next_start = float(lyrics[index + 1]["start"]) if index + 1 < len(lyrics) else duration
        elapsed = next_start - scene_start
        following_start = (
            float(lyrics[index + 2]["start"]) if index + 2 < len(lyrics) else duration
        )
        next_group_too_long = following_start - scene_start > max_seconds and elapsed >= 4.0
        is_last = index + 1 == len(lyrics)
        # A new visual normally begins with every lyric line. Very short cues are grouped only
        # when a standalone scene could not safely contain the configured cross-dissolve.
        should_close = is_last or (
            elapsed >= minimum_scene_seconds
            and (
                len(texts) >= 1
                or elapsed >= target_seconds
                or elapsed >= max_seconds
                or next_group_too_long
            )
        )
        if should_close:
            boundaries.append((scene_start, next_start, list(texts)))
            scene_start = next_start
            texts.clear()

    if boundaries[-1][1] < duration:
        boundaries[-1] = (boundaries[-1][0], duration, boundaries[-1][2])
    if len(boundaries) > 1 and boundaries[-1][1] - boundaries[-1][0] < minimum_scene_seconds:
        previous = boundaries[-2]
        final = boundaries[-1]
        boundaries[-2:] = [(previous[0], final[1], previous[2] + final[2])]

    scenes = []
    for index, (start, end, scene_lines) in enumerate(boundaries):
        scenes.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "image": "",
                "motion": "zoom_in" if index % 2 == 0 else "zoom_out",
                "prompt": _prompt(" / ".join(scene_lines), visual_style),
            }
        )
    return scenes


def _prompt(lyric_text: str, visual_style: str) -> str:
    return (
        f"{visual_style}. Create a cinematic lyric-video scene inspired by this passage: "
        f"{lyric_text!r}. Express its meaning and emotion through a coherent visual metaphor; "
        "intentional composition, strong subject separation, atmospheric lighting, consistent "
        "palette and era, landscape 16:9 framing with safe space near the lower third. "
        "No words, letters, captions, logos, watermarks, borders, or typography."
    )
