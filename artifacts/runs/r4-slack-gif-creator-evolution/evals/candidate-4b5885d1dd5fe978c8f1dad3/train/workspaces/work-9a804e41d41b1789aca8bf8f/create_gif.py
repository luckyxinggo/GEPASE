#!/usr/bin/env python3
"""Create the requested Slack emoji GIF using the supplied skill package."""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageSequence


package_root = os.environ.get("GEPASE_SKILL_PACKAGE_ROOT")
if not package_root:
    raise RuntimeError("GEPASE_SKILL_PACKAGE_ROOT is required")
sys.path.insert(0, package_root)

from core.easing import calculate_arc_motion, interpolate  # noqa: E402
from core.frame_composer import (  # noqa: E402
    create_gradient_background,
    draw_circle,
    draw_star,
)
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


SIZE = 128
FPS = 10
MOVING_FRAMES = 20
HOLD_FRAMES = 4
START = (15.0, 102.0)
DOCK = (105.0, 51.0)
ARC_HEIGHT = 38.0
OUTPUT = Path("satellite_orbit_ease.gif")


STARS = [
    (9, 17, 1),
    (23, 29, 1),
    (43, 13, 1),
    (65, 18, 2),
    (91, 12, 1),
    (116, 23, 1),
    (14, 59, 1),
    (35, 72, 1),
    (117, 78, 1),
    (22, 119, 1),
    (52, 111, 1),
    (100, 111, 1),
]


def draw_background(frame_index: int) -> Image.Image:
    frame = create_gradient_background(SIZE, SIZE, (5, 9, 35), (16, 17, 62))
    draw = ImageDraw.Draw(frame)

    # Calm, sparse star field; its subtle twinkle keeps the docked hold readable.
    for index, (x, y, radius) in enumerate(STARS):
        phase = (frame_index + index * 3) % 10
        brightness = 150 + (phase if phase <= 5 else 10 - phase) * 18
        color = (brightness, brightness, min(255, brightness + 28))
        if radius == 2:
            draw_star(frame, (x, y), 2, color)
        else:
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)

    # Dotted orbital guide is deliberately violet rather than cyan so the moving
    # satellite remains the only cyan subject in every frame.
    for index in range(33):
        path_t = index / 32
        px, py = calculate_arc_motion(START, DOCK, ARC_HEIGHT, path_t)
        if index % 2 == 0:
            draw.ellipse(
                (round(px) - 1, round(py) - 1, round(px) + 1, round(py) + 1),
                fill=(102, 91, 173),
            )

    return frame


def draw_planet(frame: Image.Image) -> None:
    center = (74, 78)

    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((46, 50, 102, 106), fill=(255, 116, 35, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(6))
    frame.paste(glow, (0, 0), glow)

    draw = ImageDraw.Draw(frame)
    draw.ellipse((43, 70, 105, 89), outline=(247, 184, 85), width=4)
    draw.ellipse((48, 54, 100, 102), fill=(118, 44, 34), outline=(255, 190, 71), width=2)
    draw.ellipse((51, 56, 97, 99), fill=(235, 101, 39))
    draw.ellipse((55, 59, 92, 79), fill=(255, 139, 49))
    draw.ellipse((59, 61, 72, 69), fill=(255, 190, 93))
    draw.ellipse((77, 83, 91, 94), fill=(187, 68, 42))
    draw.ellipse((56, 84, 65, 91), fill=(205, 76, 39))
    draw.arc((43, 70, 105, 89), 183, 355, fill=(255, 224, 139), width=2)


def draw_docking_target(frame: Image.Image, frame_index: int) -> None:
    draw = ImageDraw.Draw(frame)
    pulse = 2 if frame_index % 4 < 2 else 3
    x, y = (round(DOCK[0]), round(DOCK[1]))
    draw.ellipse((x - 9 - pulse, y - 9 - pulse, x + 9 + pulse, y + 9 + pulse), outline=(240, 216, 132), width=1)
    draw.arc((x - 9, y - 9, x + 9, y + 9), 205, 335, fill=(255, 241, 181), width=2)
    draw.line((x - 3, y + 9, x + 3, y + 9), fill=(255, 241, 181), width=2)


def draw_satellite(frame: Image.Image, center: tuple[int, int], docked: bool) -> None:
    x, y = center
    draw = ImageDraw.Draw(frame)

    # Soft shadow and symmetric solar panels.
    draw.ellipse((x - 7, y - 5, x + 8, y + 7), fill=(3, 26, 49))
    draw.rounded_rectangle((x - 13, y - 4, x - 6, y + 4), radius=2, fill=(12, 145, 161), outline=(4, 65, 91), width=2)
    draw.rounded_rectangle((x + 6, y - 4, x + 13, y + 4), radius=2, fill=(12, 145, 161), outline=(4, 65, 91), width=2)
    draw.line((x - 10, y - 3, x - 10, y + 3), fill=(67, 221, 220), width=1)
    draw.line((x + 10, y - 3, x + 10, y + 3), fill=(67, 221, 220), width=1)
    draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(24, 225, 232), outline=(4, 68, 91), width=2)
    draw.ellipse((x - 3, y - 3, x + 1, y + 1), fill=(177, 255, 249))
    draw.line((x, y - 6, x + 3, y - 10), fill=(219, 251, 242), width=2)
    draw.ellipse((x + 2, y - 11, x + 5, y - 8), fill=(255, 232, 142))

    if docked:
        draw.arc((x - 16, y - 16, x + 16, y + 16), 205, 335, fill=(255, 241, 181), width=2)


def cyan_centroid(frame: Image.Image) -> tuple[float, float]:
    rgb = np.asarray(frame.convert("RGB"))
    mask = (rgb[:, :, 0] < 100) & (rgb[:, :, 1] > 125) & (rgb[:, :, 2] > 130)
    ys, xs = np.nonzero(mask)
    if len(xs) < 12:
        raise RuntimeError("cyan satellite mask was not found in a rendered frame")
    return (float(xs.mean()), float(ys.mean()))


def main() -> None:
    builder = GIFBuilder(width=SIZE, height=SIZE, fps=FPS)
    intended_centers: list[tuple[int, int]] = []

    for frame_index in range(MOVING_FRAMES + HOLD_FRAMES):
        if frame_index < MOVING_FRAMES:
            t = frame_index / (MOVING_FRAMES - 1)
            eased_progress = interpolate(0.0, 1.0, t, easing="ease_out")
            px, py = calculate_arc_motion(START, DOCK, ARC_HEIGHT, eased_progress)
            center = (round(px), round(py))
        else:
            center = (round(DOCK[0]), round(DOCK[1]))

        frame = draw_background(frame_index)
        draw_docking_target(frame, frame_index)
        draw_planet(frame)
        draw_satellite(frame, center, docked=frame_index >= MOVING_FRAMES - 1)
        builder.add_frame(frame)
        intended_centers.append(center)

    # Validate the exact integerized centers used for rendering. Pixel rounding may
    # introduce one-pixel plateaus, so allow at most one pixel of local tolerance.
    tail_start = MOVING_FRAMES - 7
    integer_tail_steps = [
        math.dist(intended_centers[index - 1], intended_centers[index])
        for index in range(tail_start + 1, len(intended_centers))
    ]
    if any(
        current > previous + 1.0
        for previous, current in zip(integer_tail_steps, integer_tail_steps[1:])
    ):
        raise RuntimeError(f"tail displacement increased after rounding: {integer_tail_steps}")
    if intended_centers[-1] != (round(DOCK[0]), round(DOCK[1])):
        raise RuntimeError("final integerized center missed the docking point")

    build_info = builder.save(
        OUTPUT,
        num_colors=96,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )
    passes, slack_info = validate_gif(OUTPUT, is_emoji=True, verbose=True)
    if not passes:
        raise RuntimeError(f"Slack validation failed: {slack_info}")
    if slack_info["duration_seconds"] > 2.7:
        raise RuntimeError(f"duration exceeded fixture limit: {slack_info}")

    with Image.open(OUTPUT) as gif:
        rendered_frames = [frame.convert("RGB") for frame in ImageSequence.Iterator(gif)]
    rendered_centers = [cyan_centroid(frame) for frame in rendered_frames]
    rendered_tail_steps = [
        math.dist(rendered_centers[index - 1], rendered_centers[index])
        for index in range(tail_start + 1, len(rendered_centers))
    ]
    if any(
        current > previous + 1.25
        for previous, current in zip(rendered_tail_steps, rendered_tail_steps[1:])
    ):
        raise RuntimeError(f"rendered tail displacement increased: {rendered_tail_steps}")
    if math.dist(rendered_centers[-1], DOCK) > 1.5:
        raise RuntimeError(
            f"rendered cyan center missed dock: {rendered_centers[-1]} vs {DOCK}"
        )

    report = {
        "output": OUTPUT.name,
        "build": {**build_info, "path": OUTPUT.name},
        "slack_validation": {**slack_info, "file": OUTPUT.name},
        "motion": {
            "easing": "ease_out",
            "path": "single continuous parabolic arc",
            "start_center": list(intended_centers[0]),
            "dock_center": list(intended_centers[-1]),
            "integer_tail_displacements": [round(value, 3) for value in integer_tail_steps],
            "rendered_tail_displacements": [round(value, 3) for value in rendered_tail_steps],
            "rendered_final_cyan_centroid": [round(value, 3) for value in rendered_centers[-1]],
            "pixel_rounding_tolerance": 1.25,
        },
    }
    Path("validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
