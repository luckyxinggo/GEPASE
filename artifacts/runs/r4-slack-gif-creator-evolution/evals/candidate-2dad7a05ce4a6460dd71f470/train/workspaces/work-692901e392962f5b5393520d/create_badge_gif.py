#!/usr/bin/env python3
"""Create the requested Slack badge lift animation from the supplied PPM asset."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


PACKAGE_ROOT = Path(
    "artifacts/runs/r4-slack-gif-creator-evolution/candidate-workspaces/"
    "applications/application-20ada438d49e648f1bb86749"
)
INPUT_PATH = Path("benchmarks/canaries/slack-gif-creator/fixtures/input-badge.ppm")
OUTPUT_PATH = Path(
    "artifacts/runs/r4-slack-gif-creator-evolution/evals/"
    "candidate-2dad7a05ce4a6460dd71f470/train/workspaces/"
    "work-692901e392962f5b5393520d/uploaded_badge_lift.gif"
)

sys.path.insert(0, str(PACKAGE_ROOT))

from core.easing import interpolate  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


CANVAS = 128
FRAME_COUNT = 18
FPS = 10
BADGE_SIZE = 64


def gradient_background(frame_index: int) -> Image.Image:
    """Build a deep navy-to-teal backdrop with a restrained animated glow."""
    image = Image.new("RGB", (CANVAS, CANVAS))
    pixels = image.load()
    pulse = 0.5 + 0.5 * math.sin(frame_index * math.pi / 9)
    for y in range(CANVAS):
        amount = y / (CANVAS - 1)
        for x in range(CANVAS):
            radial = max(0.0, 1.0 - math.hypot(x - 64, y - 54) / 92)
            pixels[x, y] = (
                int(7 + 5 * amount + 2 * radial),
                int(20 + 22 * amount + (5 + 3 * pulse) * radial),
                int(34 + 25 * amount + 11 * radial),
            )
    return image


def load_uploaded_badge() -> Image.Image:
    """Load the PPM directly and turn only its corner-colored backdrop transparent."""
    source = Image.open(INPUT_PATH).convert("RGB")
    backdrop = source.getpixel((0, 0))
    alpha = Image.new("L", source.size, 0)
    alpha.putdata([0 if pixel == backdrop else 255 for pixel in source.getdata()])
    rgba = source.convert("RGBA")
    rgba.putalpha(alpha)
    return rgba.resize((BADGE_SIZE, BADGE_SIZE), Image.Resampling.NEAREST)


def add_glow(frame: Image.Image, center: tuple[int, int], strength: float) -> None:
    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    cx, cy = center
    radius = int(34 + 3 * strength)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(42, 203, 190, int(40 + 18 * strength)),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(13))
    frame.paste(glow, (0, 0), glow)


def add_background_effects(frame: Image.Image, frame_index: int, badge_top: int) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")

    # A soft landing shadow makes the upward travel readable at emoji scale.
    lift = max(0.0, min(1.0, (60 - badge_top) / 25))
    shadow_half_width = int(28 - 10 * lift)
    shadow_alpha = int(80 - 40 * lift)
    draw.ellipse(
        (64 - shadow_half_width, 106, 64 + shadow_half_width, 114),
        fill=(0, 7, 14, shadow_alpha),
    )

    # Updraft strokes stay below and beside the uploaded subject.
    for index, x in enumerate((18, 25, 103, 110)):
        phase = (frame_index * 5 + index * 13) % 34
        y_bottom = 119 - phase
        draw.line(
            ((x, y_bottom), (x, y_bottom - 10)),
            fill=(42, 203, 190, 28 + index * 5),
            width=2,
        )

    # Two tiny sparkles add finish without crossing the badge silhouette.
    shimmer = int(120 + 90 * (0.5 + 0.5 * math.sin(frame_index * 0.9)))
    for x, y in ((20, 39), (108, 50)):
        draw.line(((x - 3, y), (x + 3, y)), fill=(255, 187, 128, shimmer), width=1)
        draw.line(((x, y - 3), (x, y + 3)), fill=(255, 187, 128, shimmer), width=1)


def render() -> None:
    badge = load_uploaded_badge()
    builder = GIFBuilder(width=CANVAS, height=CANVAS, fps=FPS)

    for frame_index in range(FRAME_COUNT):
        # Motion finishes by frame 14; the final frames visibly hold the settled height.
        progress = min(frame_index, 14) / 14
        badge_top = round(interpolate(60, 35, progress, easing="back_out"))

        frame = gradient_background(frame_index)
        add_glow(frame, (64, badge_top + BADGE_SIZE // 2), progress)
        add_background_effects(frame, frame_index, badge_top)

        # The foreground pixels come directly from the supplied PPM, scaled with nearest-neighbor.
        frame.paste(badge, (32, badge_top), badge)
        builder.add_frame(frame)

    info = builder.save(
        OUTPUT_PATH,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(OUTPUT_PATH, is_emoji=True, verbose=True)
    if not passes:
        raise RuntimeError(f"Slack validation failed: {validation}")
    if info["duration_seconds"] > 2.5:
        raise RuntimeError(f"Duration exceeds fixture maximum: {info['duration_seconds']}")


if __name__ == "__main__":
    render()
