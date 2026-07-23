#!/usr/bin/env python3
"""Render the requested looping Slack alert emoji."""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core.easing import interpolate
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


CANVAS = 128
SUPERSAMPLE = 4
FRAME_COUNT = 32
FPS = 12

NAVY = (23, 33, 58)       # #17213a
CORAL = (255, 107, 107)   # #ff6b6b
WHITE = (255, 255, 255)   # #ffffff


def regular_polygon(center: tuple[int, int], radius: float, sides: int = 12):
    cx, cy = center
    return [
        (
            cx + radius * math.cos(-math.pi / 2 + 2 * math.pi * i / sides),
            cy + radius * math.sin(-math.pi / 2 + 2 * math.pi * i / sides),
        )
        for i in range(sides)
    ]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf", size
    )


def pulse_scale(index: int) -> float:
    """One seamless, slow-in/slow-out breath over the complete loop."""
    phase = index / FRAME_COUNT
    if phase < 0.5:
        return interpolate(0.84, 1.0, phase * 2, easing="ease_in_out")
    return interpolate(1.0, 0.84, (phase - 0.5) * 2, easing="ease_in_out")


def render_frame(index: int) -> Image.Image:
    size = CANVAS * SUPERSAMPLE
    center = (size // 2, size // 2)
    scale = pulse_scale(index)

    frame = Image.new("RGB", (size, size), NAVY)
    draw = ImageDraw.Draw(frame)

    # A crisp outer echo makes the breathing motion legible at Slack size.
    outer_radius = (49.5 * scale + 4.0) * SUPERSAMPLE
    outer_width = 2 * SUPERSAMPLE
    draw.line(
        regular_polygon(center, outer_radius),
        fill=CORAL,
        width=outer_width,
        joint="curve",
    )
    points = regular_polygon(center, 49.5 * scale * SUPERSAMPLE)
    draw.polygon(points, fill=CORAL)

    # GO remains fixed in size while the badge breathes, preserving legibility.
    font = load_font(39 * SUPERSAMPLE)
    text = "GO"
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = center[0] - text_width / 2
    y = center[1] - text_height / 2 - bbox[1] - 1 * SUPERSAMPLE

    # A restrained navy keyline keeps the white letterforms distinct.
    draw.text(
        (x, y),
        text,
        font=font,
        fill=WHITE,
        stroke_width=1 * SUPERSAMPLE,
        stroke_fill=NAVY,
    )

    return frame.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)


def preserve_requested_palette(frames: list, output_path: str) -> None:
    """Quantize against a shared palette anchored to the three supplied colors."""
    colors = [NAVY, CORAL, WHITE]
    for start, end in ((NAVY, CORAL), (CORAL, WHITE), (NAVY, WHITE)):
        for step in range(1, 42):
            ratio = step / 42
            colors.append(
                tuple(round(a + (b - a) * ratio) for a, b in zip(start, end))
            )

    palette = Image.new("P", (1, 1))
    padded = colors + [NAVY] * (256 - len(colors))
    palette.putpalette([channel for color in padded for channel in color])

    quantized = []
    for frame in frames:
        rgb = np.asarray(frame)
        mapped = Image.fromarray(rgb).quantize(
            palette=palette, dither=Image.Dither.NONE
        )
        indices = np.asarray(mapped).copy()
        # Pillow's palette lookup may choose a nearby ramp color even for an
        # exact match. Pin flat source regions to the three requested entries.
        for palette_index, color in enumerate((NAVY, CORAL, WHITE)):
            indices[np.all(rgb == color, axis=2)] = palette_index
        corrected = Image.fromarray(indices.astype(np.uint8), mode="P")
        corrected.putpalette(palette.getpalette())
        quantized.append(corrected)
    quantized[0].save(
        output_path,
        save_all=True,
        append_images=quantized[1:],
        duration=80,
        loop=0,
        optimize=False,
        disposal=2,
    )


def main() -> None:
    builder = GIFBuilder(width=CANVAS, height=CANVAS, fps=FPS)
    for frame_index in range(FRAME_COUNT):
        builder.add_frame(render_frame(frame_index))

    info = builder.save(
        "go_alert_pulse.gif",
        num_colors=128,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )
    preserve_requested_palette(builder.frames, "go_alert_pulse.gif")
    passed, validation = validate_gif(
        "go_alert_pulse.gif", is_emoji=True, verbose=True
    )
    if not passed:
        raise RuntimeError(f"Slack validation failed: {validation}")
    if validation["duration_seconds"] > 2.8:
        raise RuntimeError(f"Duration budget exceeded: {validation}")
    print({"builder": info, "validation": validation})


if __name__ == "__main__":
    main()
