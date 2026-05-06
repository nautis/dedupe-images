#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["Pillow>=10.0"]
# ///
"""Generate macapp/icon.png (1024x1024) for the DedupeImages.app bundle.

Concept: a stack of three offset photo-tiles on a deep blue gradient
background, with a single tile pulled out to the right - "many duplicates,
one keeper."
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).parent / "icon.png"
SIZE = 1024


def rounded_rect(draw: ImageDraw.ImageDraw, box, radius, **kw):
    draw.rounded_rectangle(box, radius=radius, **kw)


def make() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # Background: rounded square with deep blue → teal gradient.
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    grad = Image.new("RGB", (SIZE, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        r = int(20 + t * 30)
        g = int(50 + t * 70)
        b = int(120 + t * 60)
        for x in range(SIZE):
            grad.putpixel((x, y), (r, g, b))
    # Apply rounded mask
    mask = Image.new("L", (SIZE, SIZE), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, SIZE, SIZE), radius=int(SIZE * 0.22), fill=255)
    bg.paste(grad, (0, 0), mask=mask)
    img.paste(bg, (0, 0))

    d = ImageDraw.Draw(img)

    # Three stacked photo tiles, each rotated/offset slightly.
    tile_w, tile_h = 380, 380
    tile_radius = 32

    def draw_tile(x: int, y: int, fill, mountain_color):
        # Outer card
        rounded_rect(d, (x, y, x + tile_w, y + tile_h),
                     radius=tile_radius, fill=fill,
                     outline=(255, 255, 255, 220), width=6)
        # "Photo" content: mountain triangles + a sun circle
        inner_pad = 36
        ix, iy = x + inner_pad, y + inner_pad
        iw, ih = tile_w - 2 * inner_pad, tile_h - 2 * inner_pad
        # Sky inside (slightly different fill)
        rounded_rect(d, (ix, iy, ix + iw, iy + ih), radius=18,
                     fill=mountain_color[:3] + (60,))
        # Sun
        sun_r = 36
        d.ellipse((ix + iw - 130, iy + 40, ix + iw - 130 + 2 * sun_r,
                   iy + 40 + 2 * sun_r), fill=(255, 220, 120, 255))
        # Mountains
        d.polygon([
            (ix + 30, iy + ih - 30),
            (ix + iw // 3, iy + ih // 2),
            (ix + 2 * iw // 3, iy + ih - 30),
        ], fill=mountain_color)
        d.polygon([
            (ix + iw // 3, iy + ih - 30),
            (ix + iw // 2, iy + ih // 3),
            (ix + iw - 30, iy + ih - 30),
        ], fill=tuple(max(0, c - 35) for c in mountain_color[:3]) + (255,))

    # Stack: left side, three tiles offset
    base_x, base_y = 90, 200
    draw_tile(base_x + 60, base_y + 60,
              (220, 230, 245, 255), (90, 110, 140, 255))
    draw_tile(base_x + 30, base_y + 30,
              (230, 240, 250, 255), (90, 130, 160, 255))
    draw_tile(base_x, base_y,
              (245, 250, 255, 255), (60, 120, 170, 255))

    # Arrow → single keeper tile on the right
    arrow_color = (255, 255, 255, 255)
    arrow_y = SIZE // 2
    d.line([(560, arrow_y), (700, arrow_y)],
           fill=arrow_color, width=18)
    d.polygon([(685, arrow_y - 26), (740, arrow_y),
               (685, arrow_y + 26)], fill=arrow_color)

    # Single "kept" tile, smaller, with green check badge
    kt_x, kt_y = 740, 320
    kt_w, kt_h = 240, 360
    rounded_rect(d, (kt_x, kt_y, kt_x + kt_w, kt_y + kt_h),
                 radius=24, fill=(255, 255, 255, 255),
                 outline=(40, 80, 130, 255), width=4)
    # Inner "photo"
    pad = 22
    d.rounded_rectangle((kt_x + pad, kt_y + pad,
                         kt_x + kt_w - pad, kt_y + kt_h - pad),
                        radius=14, fill=(180, 210, 240, 255))
    # Check badge: green circle with checkmark
    cx, cy, cr = kt_x + kt_w - 30, kt_y + kt_h - 30, 60
    d.ellipse((cx - cr, cy - cr, cx + cr, cy + cr),
              fill=(46, 172, 96, 255), outline=(255, 255, 255, 255), width=8)
    # Checkmark stroke
    d.line([(cx - 26, cy + 4), (cx - 6, cy + 24), (cx + 28, cy - 18)],
           fill=(255, 255, 255, 255), width=14, joint="curve")

    # Subtle drop-shadow on whole composite
    return img


if __name__ == "__main__":
    icon = make()
    icon.save(OUT, "PNG")
    print(f"Wrote {OUT}")
