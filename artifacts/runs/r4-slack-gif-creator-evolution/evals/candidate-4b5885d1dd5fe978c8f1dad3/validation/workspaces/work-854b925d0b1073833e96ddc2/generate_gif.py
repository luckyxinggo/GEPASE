#!/usr/bin/env python3
"""Generate the requested three-color pulsing Slack alert emoji."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PACKAGE_ROOT = Path(
    "artifacts/runs/r4-slack-gif-creator-evolution/"
    "candidate-workspaces/applications/application-da89180cab97e9b294bda455"
)
sys.path.insert(0, str(PACKAGE_ROOT))

from core.easing import interpolate  # noqa: E402
from core.frame_composer import create_blank_frame, draw_circle  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


CANVAS = 128
FPS = 12
FRAME_COUNT = 12
NAVY = (23, 33, 58)
CORAL = (255, 107, 107)
WHITE = (255, 255, 255)
PALETTE = (NAVY, CORAL, WHITE)
OUTPUT = Path(__file__).with_name("go_alert_pulse.gif")


def nearest_palette_color(pixel: tuple[int, int, int]) -> tuple[int, int, int]:
    return min(
        PALETTE,
        key=lambda color: sum((pixel[channel] - color[channel]) ** 2 for channel in range(3)),
    )


def force_three_color_palette(frame: Image.Image) -> Image.Image:
    pixels = [nearest_palette_color(pixel) for pixel in frame.getdata()]
    limited = Image.new("RGB", frame.size)
    limited.putdata(pixels)
    return limited


def load_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_frame(index: int) -> Image.Image:
    # Explicit samples avoid consecutive integer-rounded duplicates in GIF encoders.
    breath_samples = (0.0, 0.125, 0.375, 0.625, 0.875, 1.0,
                      0.875, 0.625, 0.375, 0.25, 0.125, 0.0)
    radius = int(round(interpolate(39, 47, breath_samples[index], easing="linear")))

    frame = create_blank_frame(CANVAS, CANVAS, NAVY)
    # A separated outer ring gives the pulse a clear silhouette on Slack's dark UI.
    draw_circle(frame, (64, 64), radius + 5, CORAL)
    draw_circle(frame, (64, 64), radius + 2, NAVY)
    draw_circle(frame, (64, 64), radius, CORAL)

    draw = ImageDraw.Draw(frame)
    font = load_bold_font(43)
    bbox = draw.textbbox((0, 0), "GO", font=font, stroke_width=0)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (CANVAS - text_w) // 2 - bbox[0]
    y = (CANVAS - text_h) // 2 - bbox[1] - 1
    draw.text((x, y), "GO", font=font, fill=WHITE)
    return force_three_color_palette(frame)


def main() -> None:
    builder = GIFBuilder(width=CANVAS, height=CANVAS, fps=FPS)
    builder.add_frames([make_frame(index) for index in range(FRAME_COUNT)])
    build_info = builder.save(
        OUTPUT,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    passes, validation_info = validate_gif(OUTPUT, is_emoji=True, verbose=True)
    if not passes:
        raise SystemExit("Slack validation failed")

    with Image.open(OUTPUT) as image:
        used_colors: set[tuple[int, int, int]] = set()
        durations: list[int] = []
        for frame_index in range(image.n_frames):
            image.seek(frame_index)
            used_colors.update(image.convert("RGB").getdata())
            durations.append(int(image.info.get("duration", 0)))

    if image.size != (CANVAS, CANVAS):
        raise SystemExit(f"Unexpected dimensions: {image.size}")
    if image.n_frames != FRAME_COUNT:
        raise SystemExit(f"Unexpected frame count: {image.n_frames}")
    if sum(durations) > 2800:
        raise SystemExit(f"Duration budget exceeded: {sum(durations)} ms")
    if not used_colors.issubset(set(PALETTE)):
        raise SystemExit(f"Unexpected colors: {sorted(used_colors - set(PALETTE))}")

    print(
        {
            "build": build_info,
            "validation": validation_info,
            "duration_ms": sum(durations),
            "used_colors": ["#%02x%02x%02x" % color for color in sorted(used_colors)],
            "loop": image.info.get("loop"),
        }
    )


if __name__ == "__main__":
    main()
