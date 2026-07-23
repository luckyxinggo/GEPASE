#!/usr/bin/env python3
"""Render the requested Slack star-bounce emoji with the candidate package."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from core.easing import interpolate
from core.frame_composer import create_gradient_background, draw_star
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


SIZE = 128
SCALE = 3
FPS = 12


def star_layer(center_y: float, scale_x: float, scale_y: float, angle: float) -> Image.Image:
    """Draw an antialiased outlined star, glow, and highlight on a transparent layer."""
    layer_size = 84 * SCALE
    center = layer_size // 2

    glow = Image.new("RGBA", (layer_size, layer_size), (0, 0, 0, 0))
    draw_star(
        glow,
        (center, center),
        31 * SCALE,
        (255, 218, 65, 95),
        outline_color=None,
    )
    glow = glow.filter(ImageFilter.GaussianBlur(5 * SCALE))

    face = Image.new("RGBA", (layer_size, layer_size), (0, 0, 0, 0))
    draw_star(
        face,
        (center, center),
        28 * SCALE,
        (255, 215, 55, 255),
        outline_color=(55, 30, 70, 255),
        outline_width=5 * SCALE,
    )
    detail = ImageDraw.Draw(face)
    detail.ellipse(
        [center - 10 * SCALE, center - 14 * SCALE,
         center - 4 * SCALE, center - 7 * SCALE],
        fill=(255, 249, 191, 245),
    )
    detail.ellipse(
        [center - 7 * SCALE, center - 7 * SCALE,
         center - 4 * SCALE, center - 4 * SCALE],
        fill=(255, 235, 116, 225),
    )

    composed = Image.alpha_composite(glow, face)
    composed = composed.resize(
        (max(1, round(layer_size * scale_x)), max(1, round(layer_size * scale_y))),
        Image.Resampling.LANCZOS,
    )
    composed = composed.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    return composed


def frame_for(index: int) -> Image.Image:
    bg = create_gradient_background(
        SIZE * SCALE,
        SIZE * SCALE,
        (5, 15, 39),
        (42, 35, 104),
    ).convert("RGBA")
    draw = ImageDraw.Draw(bg, "RGBA")

    # Quiet background glints retain legibility at emoji size.
    for x, y, radius, alpha in ((18, 25, 2, 105), (106, 31, 1, 150), (18, 91, 1, 115), (111, 82, 2, 80)):
        x *= SCALE
        y *= SCALE
        radius *= SCALE
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(143, 164, 255, alpha))

    # One fall, one rebound, then a stable landing.
    if index <= 5:
        y = interpolate(-24, 91, index / 5, easing="ease_in")
    elif index <= 7:
        y = interpolate(91, 75, (index - 5) / 2, easing="ease_out")
    elif index <= 10:
        y = interpolate(75, 91, (index - 7) / 3, easing="ease_in")
    else:
        y = 91

    scale_x = 1.0
    scale_y = 1.0
    if index == 5:
        scale_x, scale_y = 1.12, 0.86
    elif index == 6:
        scale_x, scale_y = 0.94, 1.08
    elif index == 10:
        scale_x, scale_y = 1.06, 0.94

    angle = interpolate(-10, 0, min(index, 5) / 5, easing="ease_out") if index <= 5 else 0

    # A soft contact shadow visually anchors the settled star.
    distance = max(0.0, 91 - y)
    shadow_alpha = max(0, round(105 * (1 - min(distance / 65, 1))))
    shadow_half_width = round((19 - min(distance / 7, 9)) * SCALE)
    shadow_y = 119 * SCALE
    shadow_layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.ellipse(
        [64 * SCALE - shadow_half_width, shadow_y - 3 * SCALE,
         64 * SCALE + shadow_half_width, shadow_y + 3 * SCALE],
        fill=(2, 5, 18, shadow_alpha),
    )
    bg = Image.alpha_composite(bg, shadow_layer)

    sprite = star_layer(y, scale_x, scale_y, angle)
    paste_x = round(64 * SCALE - sprite.width / 2)
    paste_y = round(y * SCALE - sprite.height / 2)
    bg.alpha_composite(sprite, (paste_x, paste_y))

    return bg.convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def main(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    builder = GIFBuilder(width=SIZE, height=SIZE, fps=FPS)
    builder.add_frames([frame_for(i) for i in range(12)])
    info = builder.save(
        output_path,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(output_path, is_emoji=True, verbose=True)
    if not passes:
        raise SystemExit(f"Slack validation failed: {validation}")
    if info["duration_seconds"] > 2.4:
        raise SystemExit("Duration exceeds fixture maximum")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_emoji.py OUTPUT_GIF")
    main(Path(sys.argv[1]))
