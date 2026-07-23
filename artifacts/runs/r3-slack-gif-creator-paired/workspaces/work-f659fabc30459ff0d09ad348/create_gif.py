from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WORKSPACE = Path(__file__).resolve().parent
REPOSITORY_ROOT = WORKSPACE.parents[4]
PACKAGE = REPOSITORY_ROOT / "benchmarks/canaries/slack-gif-creator/package"
sys.path.insert(0, str(PACKAGE))

from core.easing import interpolate
from core.frame_composer import create_blank_frame, draw_circle
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


WIDTH = HEIGHT = 128
FPS = 12
FRAME_COUNT = 24
NAVY = (23, 33, 58)
CORAL = (255, 107, 107)
WHITE = (255, 255, 255)


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def make_frame(index: int) -> Image.Image:
    # A cosine cycle makes the last-to-first transition as smooth as all other steps.
    phase = 2.0 * math.pi * index / FRAME_COUNT
    raw = (1.0 - math.cos(phase)) / 2.0
    pulse = interpolate(0.0, 1.0, raw, easing="ease_in_out")
    radius = round(42 + 7 * pulse)

    frame = create_blank_frame(WIDTH, HEIGHT, NAVY)

    # The thin halo grows with the badge, adding a clearly visible red pulse accent.
    halo_radius = radius + 6
    draw_circle(frame, (64, 64), halo_radius, None, CORAL, 3)
    draw_circle(frame, (64, 64), radius, CORAL, None, 1)

    draw = ImageDraw.Draw(frame)
    go_font = font(38)
    bbox = draw.textbbox((0, 0), "GO", font=go_font, stroke_width=1)
    x = (WIDTH - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = (HEIGHT - (bbox[3] - bbox[1])) // 2 - bbox[1] - 1
    draw.text((x, y), "GO", font=go_font, fill=WHITE, stroke_width=1, stroke_fill=WHITE)
    return frame


def main() -> None:
    output = WORKSPACE / "go_alert_pulse.gif"
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    builder.add_frames([make_frame(i) for i in range(FRAME_COUNT)])
    info = builder.save(
        output,
        num_colors=48,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(output, is_emoji=True, verbose=True)
    if not passes:
        raise RuntimeError(f"Slack validation failed: {validation}")
    print({"builder": info, "validator": validation})


if __name__ == "__main__":
    main()
