#!/usr/bin/env python3
"""Create a warm, readable Slack meeting-reminder GIF."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.easing import interpolate
from core.frame_composer import create_blank_frame
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


WIDTH = HEIGHT = 480
FPS = 10
FRAME_COUNT = 20
SCALE = 2

CREAM = (247, 241, 232)  # #f7f1e8
DARK = (37, 34, 31)      # #25221f
BRICK = (181, 76, 47)    # #b54c2f


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Load Pillow's bundled scalable fallback without external font paths."""
    del name
    return ImageFont.load_default(size=size * SCALE)


TITLE_FONT = font("DejaVuSans-Bold.ttf", 76)
TIME_FONT = font("DejaVuSans-Bold.ttf", 82)
LABEL_FONT = font("DejaVuSans-Bold.ttf", 18)
FOOTER_FONT = font("DejaVuSans.ttf", 16)


def centered_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
                  text_font: ImageFont.FreeTypeFont, fill: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """Draw centered text and return its final high-resolution bounding box."""
    x, y = (xy[0] * SCALE, xy[1] * SCALE)
    box = draw.textbbox((0, 0), text, font=text_font)
    tw, th = box[2] - box[0], box[3] - box[1]
    origin = (round(x - tw / 2 - box[0]), round(y - th / 2 - box[1]))
    draw.text(origin, text, font=text_font, fill=fill)
    return draw.textbbox(origin, text, font=text_font)


def draw_sparkle(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: float,
                 color: tuple[int, int, int]) -> None:
    """Draw a four-point friendly sparkle."""
    cx, cy = center[0] * SCALE, center[1] * SCALE
    r = radius * SCALE
    short = max(2.0 * SCALE, r * 0.24)
    points = [
        (cx, cy - r), (cx + short, cy - short),
        (cx + r, cy), (cx + short, cy + short),
        (cx, cy + r), (cx - short, cy + short),
        (cx - r, cy), (cx - short, cy - short),
    ]
    draw.polygon(points, fill=color)


def render_frame(index: int) -> Image.Image:
    """Render one frame at 2x and downsample for crisp type and curves."""
    base = create_blank_frame(WIDTH * SCALE, HEIGHT * SCALE, CREAM)
    draw = ImageDraw.Draw(base)
    phase = 2 * math.pi * index / FRAME_COUNT

    # A high-contrast framed card keeps every text element inside a 40px safe area.
    draw.rounded_rectangle((38 * SCALE, 34 * SCALE, 442 * SCALE, 446 * SCALE),
                           radius=38 * SCALE, fill=DARK)
    draw.rounded_rectangle((46 * SCALE, 42 * SCALE, 434 * SCALE, 438 * SCALE),
                           radius=32 * SCALE, fill=CREAM)

    # Fixed header and calm calendar detail.
    draw.rounded_rectangle((74 * SCALE, 66 * SCALE, 259 * SCALE, 101 * SCALE),
                           radius=17 * SCALE, fill=BRICK)
    centered_text(draw, (166, 84), "MEETING REMINDER", LABEL_FONT, CREAM)
    draw.ellipse((368 * SCALE, 67 * SCALE, 402 * SCALE, 101 * SCALE),
                 outline=DARK, width=3 * SCALE)
    draw.line((385 * SCALE, 84 * SCALE, 385 * SCALE, 75 * SCALE),
              fill=DARK, width=3 * SCALE)
    hand_x = round((385 + 10 * math.cos(phase - math.pi / 2)) * SCALE)
    hand_y = round((84 + 10 * math.sin(phase - math.pi / 2)) * SCALE)
    draw.line((385 * SCALE, 84 * SCALE, hand_x, hand_y),
              fill=BRICK, width=3 * SCALE)

    # Both required strings remain still and fully visible for the full two seconds.
    title_box = centered_text(draw, (240, 177), "SYNC", TITLE_FONT, DARK)

    wave = (math.sin(phase - math.pi / 2) + 1.0) / 2.0
    eased = interpolate(0.0, 1.0, wave, easing="ease_in_out")
    halo = round(interpolate(2.0, 7.0, eased, easing="ease_in_out"))
    halo_width = round(interpolate(2.0, 4.0, eased, easing="ease_out"))

    time_panel = (82, 257, 398, 359)
    draw.rounded_rectangle(tuple((v + (-halo if n < 2 else halo)) * SCALE
                                 for n, v in enumerate(time_panel)),
                           radius=(30 + halo) * SCALE,
                           outline=BRICK, width=halo_width * SCALE)
    draw.rounded_rectangle(tuple(v * SCALE for v in time_panel),
                           radius=28 * SCALE, fill=BRICK)
    time_box = centered_text(draw, (240, 307), "10:30", TIME_FONT, CREAM)

    # Small cyclical accents provide motion without reducing reading time.
    sparkle_a = interpolate(7.0, 13.0, eased, easing="ease_in_out")
    sparkle_b = interpolate(11.0, 6.0, eased, easing="ease_in_out")
    draw_sparkle(draw, (84, 222), sparkle_a, BRICK)
    draw_sparkle(draw, (395, 221), sparkle_b, DARK)

    footer = "TODAY  |  SEE YOU THERE"
    centered_text(draw, (240, 400), footer, FOOTER_FONT, DARK)
    draw.line((132 * SCALE, 374 * SCALE, 348 * SCALE, 374 * SCALE),
              fill=BRICK, width=3 * SCALE)

    # Guard the task's safe-zone and readability requirements before export.
    for box in (title_box, time_box):
        assert box[0] >= 48 * SCALE and box[2] <= 432 * SCALE
        assert box[1] >= 48 * SCALE and box[3] <= 432 * SCALE

    return base.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def mean_pixel_delta(a: Image.Image, b: Image.Image) -> float:
    """Return a compact seam/adjacent-frame visual-difference measure."""
    pa = list(a.convert("RGB").getdata())
    pb = list(b.convert("RGB").getdata())
    return sum(abs(x - y) for ca, cb in zip(pa, pb) for x, y in zip(ca, cb)) / (len(pa) * 3)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: create_meeting_gif.py OUTPUT.gif")

    output = Path(sys.argv[1])
    frames = [render_frame(i) for i in range(FRAME_COUNT)]

    # The loop boundary must be no more abrupt than the normal pulse steps.
    adjacent = [mean_pixel_delta(frames[i], frames[(i + 1) % FRAME_COUNT])
                for i in range(FRAME_COUNT)]
    seam_delta = adjacent[-1]
    non_seam_max = max(adjacent[:-1])
    assert seam_delta <= non_seam_max * 1.15 + 0.01

    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    builder.add_frames(frames)
    info = builder.save(output, num_colors=48, optimize_for_emoji=False,
                        remove_duplicates=False)

    passes, validation = validate_gif(output, is_emoji=False, verbose=True)
    assert passes
    assert validation["width"] == WIDTH and validation["height"] == HEIGHT
    assert validation["frame_count"] == FRAME_COUNT
    assert abs(validation["duration_seconds"] - 2.0) <= 0.1
    print(f"Content checks: required text stable; safe margin >= 48px; "
          f"loop seam delta {seam_delta:.4f} <= adjacent max {non_seam_max:.4f}")
    print(f"Export summary: {info}")


if __name__ == "__main__":
    main()
