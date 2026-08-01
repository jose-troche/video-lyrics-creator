from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .errors import VideoLyricsError

FONT_CANDIDATES = {
    "Avenir Next Demi Bold": [
        "/System/Library/Fonts/Avenir Next.ttc",
        "/Library/Fonts/Avenir Next.ttc",
    ],
    "Arial": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ],
}


def prepare_overlays(manifest: dict, *, force: bool = False) -> int:
    video = manifest["video"]
    width, height = int(video["width"]), int(video["height"])
    work_dir = Path(manifest["work_dir"]) / "overlays"
    work_dir.mkdir(parents=True, exist_ok=True)
    font_name = str(video.get("font", "Avenir Next Demi Bold"))
    title_font = _load_font(font_name, max(44, round(height * 0.068)))
    author_font = _load_font(font_name, max(28, round(height * 0.035)))
    lyric_font = _load_font(font_name, int(video.get("font_size", 58)))
    created = 0

    title_path = work_dir / "title.png"
    if force or not title_path.is_file():
        _draw_title(
            title_path,
            width,
            height,
            str(manifest["title"]),
            str(manifest.get("author", "José Troche")),
            title_font,
            author_font,
        )
        created += 1

    lyric_assets = []
    for index, cue in enumerate(manifest["lyrics"], 1):
        path = work_dir / f"lyric-{index:03d}.png"
        if force or not path.is_file():
            _draw_lyric(
                path,
                width,
                height,
                str(cue["text"]),
                lyric_font,
                int(video.get("margin_v", 72)),
            )
            created += 1
        lyric_assets.append(
            {
                "start": float(cue["start"]),
                "end": float(cue["end"]),
                "image": str(path.resolve()),
            }
        )
    manifest["overlays"] = {"title": str(title_path.resolve()), "lyrics": lyric_assets}
    return created


def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = []
    supplied = Path(name).expanduser()
    if supplied.is_file():
        candidates.append(str(supplied))
    candidates.extend(FONT_CANDIDATES.get(name, []))
    candidates.extend(FONT_CANDIDATES["Arial"])
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    raise VideoLyricsError(
        f"Could not load font {name!r}. Set video.font to an installed .ttf/.otf/.ttc path."
    )


def _fit_lines(text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int = 2) -> list[str]:
    words = text.replace("\\N", " ").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if font.getlength(candidate) <= max_width or len(lines) + 1 >= max_lines:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1 :])]
    return lines


def _fit_font_and_lines(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    max_lines: int = 2,
    minimum_size: int = 28,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    candidate = font
    while True:
        lines = _fit_lines(text, candidate, max_width, max_lines)
        if all(candidate.getlength(line) <= max_width for line in lines):
            return candidate, lines
        next_size = candidate.size - 2
        if next_size < minimum_size:
            return candidate, lines
        candidate = candidate.font_variant(size=next_size)


def _draw_title(
    path: Path,
    width: int,
    height: int,
    title: str,
    author: str,
    title_font: ImageFont.FreeTypeFont,
    author_font: ImageFont.FreeTypeFont,
) -> None:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    title_font, lines = _fit_font_and_lines(
        title, title_font, round(width * 0.78), max_lines=2, minimum_size=32
    )
    title_height = sum(draw.textbbox((0, 0), line, font=title_font, stroke_width=2)[3] for line in lines)
    spacing = round(height * 0.018)
    author_height = draw.textbbox((0, 0), author, font=author_font, stroke_width=1)[3]
    total = title_height + spacing * max(1, len(lines)) + author_height
    y = (height - total) / 2
    for line in lines:
        box = draw.textbbox((0, 0), line, font=title_font, stroke_width=2)
        x = (width - (box[2] - box[0])) / 2
        _shadowed_text(draw, (x, y), line, title_font, stroke=2)
        y += box[3] - box[1] + spacing
    box = draw.textbbox((0, 0), author, font=author_font, stroke_width=1)
    x = (width - (box[2] - box[0])) / 2
    _shadowed_text(draw, (x, y), author, author_font, stroke=1)
    image.save(path)


def _draw_lyric(
    path: Path,
    width: int,
    height: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    margin_v: int,
) -> None:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font, lines = _fit_font_and_lines(
        text, font, round(width * 0.82), max_lines=2, minimum_size=28
    )
    spacing = max(8, round(font.size * 0.2))
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
    total = sum(box[3] - box[1] for box in boxes) + spacing * (len(lines) - 1)
    y = height - margin_v - total
    for line, box in zip(lines, boxes):
        x = (width - (box[2] - box[0])) / 2
        _shadowed_text(draw, (x, y), line, font, stroke=2)
        y += box[3] - box[1] + spacing
    image.save(path)


def _shadowed_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    stroke: int,
) -> None:
    x, y = position
    draw.text(
        (x + 4, y + 5),
        text,
        font=font,
        fill=(0, 0, 0, 190),
        stroke_width=stroke + 2,
        stroke_fill=(0, 0, 0, 170),
    )
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 230),
    )
