#!/usr/bin/env python3
"""Render and validate a seamless, rhythmic sparkle-ring Slack emoji GIF."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageSequence

from core.frame_composer import create_gradient_background, draw_circle, draw_star
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


SIZE = 128
SCALE = 3
FRAME_COUNT = 18
FPS = 10


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def _rhythmic_envelope(progress: float, phase: float) -> float:
    """A smooth periodic rise-and-fall envelope with no duplicated endpoint."""
    wave = 0.5 - 0.5 * math.cos(2.0 * math.pi * (progress - phase))
    return wave**1.65


def _draw_sparkle(
    frame: Image.Image,
    center: tuple[int, int],
    color: tuple[int, int, int],
    strength: float,
) -> None:
    if strength < 0.015:
        return

    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_radius = round((5.5 + 6.5 * strength) * SCALE)
    cx, cy = center
    glow_draw.ellipse(
        (cx - glow_radius, cy - glow_radius, cx + glow_radius, cy + glow_radius),
        fill=(*color, round(120 * strength)),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=(3.0 + 2.2 * strength) * SCALE))
    frame.paste(glow, (0, 0), glow)

    symbol = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    size = max(2 * SCALE, round((2.1 + 5.2 * strength) * SCALE))
    alpha = round(58 + 197 * strength)
    bright = _mix(color, (255, 255, 255), 0.38 + 0.42 * strength)

    # Use the Package star helper, then add long four-way rays for a crisp sparkle silhouette.
    draw_star(
        symbol,
        center,
        size,
        fill_color=(*bright, alpha),
        outline_color=(*color, alpha),
        outline_width=max(2, SCALE),
    )
    symbol_draw = ImageDraw.Draw(symbol)
    long_r = round((3.0 + 8.0 * strength) * SCALE)
    short_r = max(2, round((1.2 + 2.5 * strength) * SCALE))
    diamond = [
        (cx, cy - long_r),
        (cx + short_r, cy - short_r),
        (cx + long_r, cy),
        (cx + short_r, cy + short_r),
        (cx, cy + long_r),
        (cx - short_r, cy + short_r),
        (cx - long_r, cy),
        (cx - short_r, cy - short_r),
    ]
    symbol_draw.polygon(diamond, fill=(*bright, alpha))
    core_r = max(2 * SCALE, round((1.4 + 1.4 * strength) * SCALE))
    symbol_draw.ellipse(
        (cx - core_r, cy - core_r, cx + core_r, cy + core_r),
        fill=(255, 255, 248, alpha),
    )
    frame.paste(symbol, (0, 0), symbol)


def _render_frame(frame_index: int) -> Image.Image:
    extent = SIZE * SCALE
    progress = frame_index / FRAME_COUNT
    frame = create_gradient_background(extent, extent, (19, 19, 51), (39, 24, 71))

    # Add a fixed vignette so the bright, stable ring stays readable at emoji scale.
    pixels = np.asarray(frame, dtype=np.float32)
    yy, xx = np.mgrid[0:extent, 0:extent]
    distance = np.sqrt((xx - extent / 2) ** 2 + (yy - extent / 2) ** 2)
    vignette = np.clip(1.04 - 0.18 * (distance / (extent * 0.72)) ** 1.7, 0.82, 1.04)
    pixels = np.clip(pixels * vignette[..., None], 0, 255).astype(np.uint8)
    frame = Image.fromarray(pixels, "RGB")

    center = (64 * SCALE, 64 * SCALE)
    # Stable layered ring: shadow, saturated rim, bright inner edge, and two fixed highlights.
    draw_circle(frame, center, 24 * SCALE, fill_color=(24, 18, 53), outline_color=(8, 8, 28), outline_width=7 * SCALE)
    draw_circle(frame, center, 23 * SCALE, outline_color=(97, 68, 210), outline_width=5 * SCALE)
    draw_circle(frame, center, 19 * SCALE, outline_color=(201, 183, 255), outline_width=2 * SCALE)
    ring_draw = ImageDraw.Draw(frame)
    ring_box = (41 * SCALE, 41 * SCALE, 87 * SCALE, 87 * SCALE)
    ring_draw.arc(ring_box, 205, 292, fill=(117, 227, 255), width=3 * SCALE)
    ring_draw.arc(ring_box, 26, 72, fill=(255, 217, 118), width=2 * SCALE)

    sparkle_specs = (
        (-90.0, (255, 199, 78), 0.00),
        (30.0, (85, 221, 255), 1.0 / 3.0),
        (150.0, (255, 104, 190), 2.0 / 3.0),
    )
    orbit_radius = 40 * SCALE
    for angle_degrees, color, phase in sparkle_specs:
        angle = math.radians(angle_degrees)
        sparkle_center = (
            round(center[0] + orbit_radius * math.cos(angle)),
            round(center[1] + orbit_radius * math.sin(angle)),
        )
        _draw_sparkle(frame, sparkle_center, color, _rhythmic_envelope(progress, phase))

    return frame.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def _export_contact_sheet(frames: list[Image.Image], output_path: Path) -> None:
    selected = [frames[i] for i in (0, 3, 6, 9, 12, 15)]
    sheet = Image.new("RGB", (SIZE * 3, SIZE * 2), (12, 12, 28))
    for index, frame in enumerate(selected):
        sheet.paste(frame, ((index % 3) * SIZE, (index // 3) * SIZE))
    sheet.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    frames = [_render_frame(i) for i in range(FRAME_COUNT)]
    builder = GIFBuilder(width=SIZE, height=SIZE, fps=FPS)
    builder.add_frames(frames)
    export_info = builder.save(
        args.output,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )

    passes, validation = validate_gif(args.output, is_emoji=True, verbose=True)
    with Image.open(args.output) as gif:
        exported_frames = [np.asarray(frame.convert("RGB"), dtype=np.float32) for frame in ImageSequence.Iterator(gif)]
        frame_durations_ms = [frame.info.get("duration", gif.info.get("duration", 100)) for frame in ImageSequence.Iterator(gif)]

    adjacent_differences = [
        float(np.mean(np.abs(exported_frames[i + 1] - exported_frames[i])))
        for i in range(len(exported_frames) - 1)
    ]
    seam_difference = float(np.mean(np.abs(exported_frames[0] - exported_frames[-1])))
    median_adjacent = float(np.median(adjacent_differences))
    seam_ratio = seam_difference / median_adjacent if median_adjacent else 0.0
    duration_seconds = sum(frame_durations_ms) / 1000.0
    seam_pass = seam_ratio <= 1.25

    diagnostics = {
        "output": args.output.name,
        "dimensions": [SIZE, SIZE],
        "frame_count": len(exported_frames),
        "fps_requested": FPS,
        "duration_seconds": duration_seconds,
        "duration_limit_seconds": 2.2,
        "looping": "infinite",
        "motion_design": "18 exclusive periodic samples; three phase-offset smooth envelopes",
        "seam_mean_abs_difference": round(seam_difference, 4),
        "median_adjacent_mean_abs_difference": round(median_adjacent, 4),
        "seam_to_median_adjacent_ratio": round(seam_ratio, 4),
        "seam_pass": seam_pass,
        "slack_validation_pass": passes,
        "validator": validation,
        "builder": {key: value for key, value in export_info.items() if key != "path"},
    }
    args.diagnostics.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.preview:
        _export_contact_sheet(frames, args.preview)

    if not passes or duration_seconds > 2.2 or not seam_pass:
        raise SystemExit("export validation failed")


if __name__ == "__main__":
    main()
