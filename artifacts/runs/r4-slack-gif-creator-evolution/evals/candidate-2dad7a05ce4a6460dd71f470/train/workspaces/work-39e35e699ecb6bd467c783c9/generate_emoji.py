#!/usr/bin/env python3
"""Generate the requested Slack-ready bouncing-star emoji GIF."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


REPO = Path.cwd()
PACKAGE = REPO / (
    "artifacts/runs/r4-slack-gif-creator-evolution/candidate-workspaces/"
    "applications/application-20ada438d49e648f1bb86749"
)
WORKSPACE = REPO / (
    "artifacts/runs/r4-slack-gif-creator-evolution/evals/"
    "candidate-2dad7a05ce4a6460dd71f470/train/workspaces/"
    "work-39e35e699ecb6bd467c783c9"
)
OUTPUT = WORKSPACE / "emoji_star_bounce.gif"

sys.path.insert(0, str(PACKAGE))

from core.easing import apply_squash_stretch, calculate_arc_motion, interpolate
from core.frame_composer import create_gradient_background, draw_star
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


SCALE = 4
CANVAS = 128
FRAMES = 18
FPS = 12


def scaled(value: float) -> int:
    return round(value * SCALE)


def star_position(frame_index: int) -> tuple[float, float]:
    """One accelerated fall, one rebound arc, then a stable landing."""
    if frame_index <= 7:
        t = frame_index / 7
        return (64.0, interpolate(-27.0, 89.0, t, easing="ease_in"))
    if frame_index <= 13:
        t = (frame_index - 7) / 6
        return calculate_arc_motion((64.0, 89.0), (64.0, 89.0), 19.0, t)
    return (64.0, 89.0)


def star_transform(frame_index: int) -> tuple[float, float, float]:
    if frame_index == 7:
        sx, sy = apply_squash_stretch((1.0, 1.0), 0.40, "vertical")
    elif frame_index == 8:
        sx, sy = (0.91, 1.12)
    elif frame_index == 13:
        sx, sy = apply_squash_stretch((1.0, 1.0), 0.22, "vertical")
    elif frame_index == 14:
        sx, sy = (0.96, 1.04)
    else:
        sx, sy = (1.0, 1.0)

    if frame_index <= 7:
        angle = interpolate(-11.0, 4.0, frame_index / 7, easing="ease_out")
    elif frame_index <= 13:
        angle = interpolate(4.0, 0.0, (frame_index - 7) / 6, easing="ease_in_out")
    else:
        angle = 0.0
    return sx, sy, angle


def draw_star_sprite(frame_index: int, sx: float, sy: float, angle: float) -> Image.Image:
    sprite_size = scaled(76)
    center = sprite_size // 2

    glow = Image.new("RGBA", (sprite_size, sprite_size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_radius = scaled(30)
    glow_draw.ellipse(
        [
            center - glow_radius,
            center - glow_radius,
            center + glow_radius,
            center + glow_radius,
        ],
        fill=(255, 211, 64, 72),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=scaled(6)))

    sprite = Image.new("RGBA", (sprite_size, sprite_size), (0, 0, 0, 0))
    draw_star(
        sprite,
        (center, center),
        scaled(29),
        fill_color=(255, 209, 45, 255),
        outline_color=(35, 20, 56, 255),
        outline_width=scaled(4),
    )

    # Warm inner face gives the star depth while preserving the bold silhouette.
    draw_star(
        sprite,
        (center, center + scaled(1)),
        scaled(22),
        fill_color=(255, 222, 72, 255),
        outline_color=None,
        outline_width=0,
    )

    detail = ImageDraw.Draw(sprite)
    detail.ellipse(
        [
            center - scaled(10),
            center - scaled(14),
            center - scaled(5),
            center - scaled(7),
        ],
        fill=(255, 255, 235, 235),
    )
    detail.ellipse(
        [
            center - scaled(5),
            center - scaled(10),
            center - scaled(3),
            center - scaled(7),
        ],
        fill=(255, 255, 255, 170),
    )

    transformed_size = (
        max(1, round(sprite_size * sx)),
        max(1, round(sprite_size * sy)),
    )
    sprite = sprite.resize(transformed_size, Image.Resampling.LANCZOS)
    glow = glow.resize(transformed_size, Image.Resampling.LANCZOS)

    if angle:
        sprite = sprite.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        glow = glow.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)

    combined = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
    if glow.size != combined.size:
        glow_layer = Image.new("RGBA", combined.size, (0, 0, 0, 0))
        glow_layer.alpha_composite(
            glow,
            ((combined.width - glow.width) // 2, (combined.height - glow.height) // 2),
        )
        glow = glow_layer
    combined.alpha_composite(glow)
    combined.alpha_composite(sprite)
    return combined


def compose_frame(frame_index: int) -> Image.Image:
    frame = create_gradient_background(
        scaled(CANVAS),
        scaled(CANVAS),
        top_color=(5, 10, 35),
        bottom_color=(46, 25, 112),
    ).convert("RGBA")
    draw = ImageDraw.Draw(frame, "RGBA")

    # Quiet, fixed specks make the tiny canvas feel atmospheric without noise.
    for x, y, radius, alpha in (
        (17, 26, 1.1, 95),
        (108, 18, 0.8, 80),
        (116, 54, 1.0, 75),
        (21, 78, 0.7, 70),
        (99, 94, 0.7, 55),
    ):
        r = scaled(radius)
        draw.ellipse(
            [scaled(x) - r, scaled(y) - r, scaled(x) + r, scaled(y) + r],
            fill=(173, 182, 255, alpha),
        )

    x, y = star_position(frame_index)
    height = max(0.0, 89.0 - y)
    proximity = max(0.0, 1.0 - height / 78.0)
    shadow_w = scaled(10 + 20 * proximity)
    shadow_h = scaled(2.2 + 2.3 * proximity)
    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow, "RGBA")
    shadow_draw.ellipse(
        [
            scaled(64) - shadow_w,
            scaled(118) - shadow_h,
            scaled(64) + shadow_w,
            scaled(118) + shadow_h,
        ],
        fill=(2, 4, 20, round(35 + 110 * proximity)),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=scaled(2.2)))
    frame.alpha_composite(shadow)

    sx, sy, angle = star_transform(frame_index)
    sprite = draw_star_sprite(frame_index, sx, sy, angle)
    frame.alpha_composite(
        sprite,
        (round(scaled(x) - sprite.width / 2), round(scaled(y) - sprite.height / 2)),
    )

    # A restrained landing glint changes while the star itself remains still.
    if frame_index >= 14:
        glint_alpha = [120, 200, 120, 45][frame_index - 14]
        glint = ImageDraw.Draw(frame, "RGBA")
        gx, gy = scaled(95), scaled(67)
        arm = scaled(2.7)
        glint.line([(gx - arm, gy), (gx + arm, gy)], fill=(255, 241, 170, glint_alpha), width=scaled(1))
        glint.line([(gx, gy - arm), (gx, gy + arm)], fill=(255, 241, 170, glint_alpha), width=scaled(1))

    return frame.convert("RGB")


def main() -> None:
    builder = GIFBuilder(width=CANVAS, height=CANVAS, fps=FPS)
    for index in range(FRAMES):
        builder.add_frame(compose_frame(index))

    info = builder.save(
        OUTPUT,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(OUTPUT, is_emoji=True, verbose=True)
    if not passes:
        raise SystemExit("GIF did not pass Slack emoji validation")

    print(
        "motion=single_bounce; "
        f"frames={validation['frame_count']}; "
        f"duration={validation['duration_seconds']:.3f}s; "
        f"size_kb={validation['size_kb']:.1f}; "
        f"builder_frames={info['frame_count']}"
    )


if __name__ == "__main__":
    main()
