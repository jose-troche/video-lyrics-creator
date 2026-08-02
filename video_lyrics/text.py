"""Font lookup and text-image drawing (PIL)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .util import log

FONT_DIRS = (
    Path.home() / "Library" / "Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)
FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")
FALLBACKS = ("Helvetica.ttc", "HelveticaNeue.ttc", "Arial.ttf", "DejaVuSans-Bold.ttf")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@lru_cache(maxsize=32)
def _locate(name: str) -> tuple[str, int] | None:
    """Find (file, face index) for a font described by family (+ style) name."""
    candidate = Path(name).expanduser()
    if candidate.is_file():
        return str(candidate), 0

    wanted = _key(name)
    fallback: tuple[str, int] | None = None
    for directory in FONT_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in FONT_SUFFIXES:
                continue
            if _key(path.stem) == wanted:
                return str(path), 0
            if not wanted.startswith(_key(path.stem)):
                continue
            # Collection or family file: scan its faces for "<family><style>".
            for index in range(0, 24):
                try:
                    font = ImageFont.truetype(str(path), 12, index=index)
                except Exception:  # noqa: BLE001 - past the last face
                    break
                family, style = font.getname()
                if _key(f"{family}{style}") == wanted:
                    return str(path), index
                if fallback is None and _key(family) == wanted:
                    fallback = (str(path), index)
    return fallback


@lru_cache(maxsize=64)
def load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    located = _locate(name)
    if located:
        path, index = located
        return ImageFont.truetype(path, size, index=index)
    for fallback in FALLBACKS:
        try:
            return ImageFont.truetype(fallback, size)
        except OSError:
            continue
    log.warning("Font %r not found; using PIL's default bitmap font.", name)
    return ImageFont.load_default(size)


def measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word wrap against pixel width."""
    scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if measure(scratch, trial, font)[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def draw_block(
    canvas: Image.Image,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    *,
    center_y: int,
    fill: tuple[int, int, int, int] = (255, 255, 255, 255),
    line_spacing: float = 1.25,
    shadow_blur: int = 10,
    shadow_offset: tuple[int, int] = (0, 4),
    stroke_width: int = 2,
) -> None:
    """Draw centred lines with a soft drop shadow, vertically centred on center_y."""
    draw = ImageDraw.Draw(canvas)
    ascent, descent = font.getmetrics()
    line_height = int(round((ascent + descent) * line_spacing))
    total = line_height * len(lines)
    top = center_y - total // 2

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    for index, line in enumerate(lines):
        width = measure(draw, line, font)[0]
        x = (canvas.width - width) // 2
        y = top + index * line_height
        shadow_draw.text(
            (x + shadow_offset[0], y + shadow_offset[1]),
            line,
            font=font,
            fill=(0, 0, 0, 190),
            stroke_width=stroke_width + 2,
            stroke_fill=(0, 0, 0, 190),
        )
    if shadow_blur:
        shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
    canvas.alpha_composite(shadow)

    for index, line in enumerate(lines):
        width = measure(draw, line, font)[0]
        x = (canvas.width - width) // 2
        y = top + index * line_height
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=(0, 0, 0, 150),
        )
