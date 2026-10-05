"""Bounded text layout for image exports; never paint outside an OCR box."""
from __future__ import annotations
import re


def wrap_text(text, font, width):
    rows = []
    for paragraph in text.split("\n"):
        current = ""
        for token in re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9]", paragraph):
            if current and font.getlength(current + token) > width:
                rows.append(current.rstrip())
                current = ""
            for character in token:
                if current and font.getlength(current + character) > width:
                    rows.append(current.rstrip())
                    current = ""
                current += character
        rows.append(current.rstrip())
    return "\n".join(rows)


def fitted_text(text, width, height, load_font):
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in range(min(48, max(7, int(height * .68))), 6, -1):
        font = load_font(size)
        wrapped = wrap_text(text, font, max(1, width))
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=1)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            return wrapped, font, box, False
    # Minimum readable size. Ellipsis exposes truncation instead of overwriting
    # neighboring text. Keep prefixes, never invent or summarize a translation.
    rows = wrapped.split("\n")
    while len(rows) > 1 and draw.multiline_textbbox((0, 0), "\n".join(rows), font=font, spacing=1)[3] > height:
        rows.pop()
    last = rows[-1]
    while last and font.getlength(last + "…") > width:
        last = last[:-1]
    rows[-1] = last + "…"
    clipped = "\n".join(rows)
    return clipped, font, draw.multiline_textbbox((0, 0), clipped, font=font, spacing=1), True


def paint_translation(canvas, rect, text, background, foreground, load_font):
    from PIL import Image, ImageDraw
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    tile = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(tile)
    inset = min(3, max(1, height // 8))
    wrapped, font, bounds, truncated = fitted_text(text, width - inset * 2, height - inset * 2, load_font)
    draw.multiline_text((inset - bounds[0], inset - bounds[1]), wrapped, font=font, fill=foreground, spacing=1)
    canvas.paste(tile, (left, top))
    return truncated
