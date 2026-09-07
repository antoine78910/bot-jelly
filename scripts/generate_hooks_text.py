"""Generate text-hook PNG overlays (one file per hook line)."""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent / "assets" / "clips" / "hooks_text" / "lines"

HOOK_LINES = [
    "the art of jelly",
    "This tool just replaced your ENTIRE marketing team",
    "I could literally KISS the business owner that showed me this",
    "This method is replacing marketing agencies",
    "AI just killed marketing agencies",
    "Save this before they take it down",
    "This feels illegal to know",
    "My secret AI stack revealed",
    "AI on steroids",
    "Feels like bitcoin in 2009",
    "How this is legal ??",
    "Got fired by my agency so i'm leaking their ENTIRE process",
    "How is this website even legal",
    "I'm so bad at marketing .. do this instead",
    "How on earth is this legal ?!",
]

WIDTH = 1080
MAX_HEIGHT = 620
H_PADDING = 40
V_PADDING = 44
MAX_FONT_SIZE = 80
MIN_FONT_SIZE = 44
LINE_GAP = 10
STROKE_WIDTH = 4
LINE_PAD = STROKE_WIDTH + 4
STROKE_OFFSETS = (
    (-4, 0),
    (4, 0),
    (0, -4),
    (0, 4),
    (-3, -3),
    (3, -3),
    (-3, 3),
    (3, 3),
    (-2, -2),
    (2, -2),
    (-2, 2),
    (2, 2),
)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:72] or "hook"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Prefer the OS system UI bold font (Segoe UI Bold on Windows)."""
    candidates = [
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)

    for name in ("segoeuib.ttf", "Segoe UI Bold.ttf", "segoeui.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_lines(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> list[str]:
    words = text.split()
    if not words:
        return [text]

    for width in (22, 18, 14, 12, 10):
        wrapped = textwrap.wrap(text, width=width)
        if len(wrapped) <= 3:
            return wrapped

    return textwrap.wrap(text, width=10)


def _line_metrics(
    line: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[tuple[int, int, int, int], int, int]:
    probe = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), line, font=font, stroke_width=0)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    canvas_w = text_w + LINE_PAD * 2
    canvas_h = text_h + LINE_PAD * 2
    return bbox, canvas_w, canvas_h


def _measure_block(
    lines: list[str],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[int, int]:
    max_w = 0
    total_h = 0
    for index, line in enumerate(lines):
        _, canvas_w, canvas_h = _line_metrics(line, font)
        max_w = max(max_w, canvas_w)
        total_h += canvas_h
        if index < len(lines) - 1:
            total_h += LINE_GAP
    return max_w, total_h


def _layout(text: str) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
    for size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -2):
        font = _font(size)
        lines = _wrap_lines(text, font)
        block_w, block_h = _measure_block(lines, font)
        if block_w <= WIDTH - H_PADDING * 2 and block_h <= MAX_HEIGHT - V_PADDING * 2:
            return font, lines

    font = _font(MIN_FONT_SIZE)
    return font, _wrap_lines(text, font)


def _render_line(line: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> Image.Image:
    bbox, canvas_w, canvas_h = _line_metrics(line, font)
    layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x = LINE_PAD - bbox[0]
    y = LINE_PAD - bbox[1]

    for dx, dy in STROKE_OFFSETS:
        draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 255))
    draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
    return layer


def _draw_label(text: str, dest: Path) -> None:
    font, lines = _layout(text)
    line_images = [_render_line(line, font) for line in lines]
    content_h = sum(image.height for image in line_images) + LINE_GAP * max(0, len(line_images) - 1)

    img = Image.new("RGBA", (WIDTH, content_h + V_PADDING * 2), (0, 0, 0, 0))
    y = V_PADDING

    for line_image in line_images:
        x = (WIDTH - line_image.width) // 2
        img.alpha_composite(line_image, (x, y))
        y += line_image.height + LINE_GAP

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")


def main() -> None:
    for line in HOOK_LINES:
        dest = ROOT / f"{_slug(line)}.png"
        _draw_label(line, dest)
        print(f"Wrote {dest}")


if __name__ == "__main__":
    main()
