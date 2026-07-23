#!/usr/bin/env python3
"""Generate the requested cyclic Slack emoji GIF using the supplied Package."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageSequence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    return parser.parse_args()


def four_point_sparkle(
    canvas: Image.Image,
    center: tuple[float, float],
    radius: float,
    color: tuple[int, int, int],
    glow_alpha: int,
    angle: float,
) -> None:
    """Draw a luminous four-point sparkle with a soft halo."""
    if radius < 1.0:
        return

    cx, cy = center
    scale = canvas.width / 128
    halo = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    halo_draw = ImageDraw.Draw(halo)
    glow_radius = radius * 1.65
    halo_draw.ellipse(
        (cx - glow_radius, cy - glow_radius, cx + glow_radius, cy + glow_radius),
        fill=(*color, glow_alpha),
    )
    halo = halo.filter(ImageFilter.GaussianBlur(radius=max(1.0, 2.8 * scale)))
    canvas.alpha_composite(halo)

    points: list[tuple[float, float]] = []
    for index in range(8):
        theta = angle + index * math.pi / 4
        if index % 2 == 0:
            spoke = radius
        else:
            spoke = radius * 0.24
        points.append((cx + math.cos(theta) * spoke, cy + math.sin(theta) * spoke))

    draw = ImageDraw.Draw(canvas)
    draw.polygon(points, fill=(*color, 255))
    core = max(1.2 * scale, radius * 0.18)
    draw.ellipse((cx - core, cy - core, cx + core, cy + core), fill=(255, 255, 248, 255))


def cyclic_pulse(phase: float, offset: float, ease_func) -> float:
    """Raised, eased pulse on a circular timeline: disappear -> grow -> fade."""
    distance = abs(((phase - offset + 0.5) % 1.0) - 0.5)
    half_width = 0.25
    raw = max(0.0, 1.0 - distance / half_width)
    return ease_func(raw)


def main() -> None:
    args = parse_args()
    sys.path.insert(0, str(args.skill_root))

    from core.easing import get_easing
    from core.frame_composer import create_gradient_background, draw_circle, draw_star
    from core.gif_builder import GIFBuilder
    from core.validators import validate_gif

    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame_count = 20
    fps = 10
    supersample = 3
    side = 128 * supersample
    center = (64 * supersample, 64 * supersample)
    ease_in_out = get_easing("ease_in_out")

    builder = GIFBuilder(width=128, height=128, fps=fps)
    sparkle_specs = (
        (-math.pi / 2, 0.00, (255, 226, 104)),
        (math.pi / 6, 1 / 3, (103, 237, 255)),
        (5 * math.pi / 6, 2 / 3, (255, 133, 224)),
    )

    for frame_index in range(frame_count):
        # Deliberately omit a duplicated endpoint: phase steps are uniform across the loop seam.
        phase = frame_index / frame_count
        base = create_gradient_background(side, side, (11, 8, 35), (34, 22, 80)).convert("RGBA")

        # A stable central ring with layered glow and a crisp, high-contrast core.
        ring_glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ring_glow_draw = ImageDraw.Draw(ring_glow)
        ring_glow_draw.ellipse(
            (39 * supersample, 39 * supersample, 89 * supersample, 89 * supersample),
            outline=(96, 209, 255, 105),
            width=7 * supersample,
        )
        ring_glow = ring_glow.filter(ImageFilter.GaussianBlur(4.2 * supersample))
        base.alpha_composite(ring_glow)
        draw_circle(
            base,
            center,
            23 * supersample,
            fill_color=(20, 14, 59, 255),
            outline_color=(103, 224, 255, 255),
            outline_width=4 * supersample,
        )
        draw_circle(
            base,
            center,
            18 * supersample,
            fill_color=(17, 12, 47, 255),
            outline_color=(183, 147, 255, 255),
            outline_width=2 * supersample,
        )
        # Execute the supplied star helper for a small, stable ring highlight.
        draw_star(
            base,
            (57 * supersample, 53 * supersample),
            2 * supersample,
            (232, 252, 255, 255),
        )

        for star_index, (theta, offset, color) in enumerate(sparkle_specs):
            pulse = cyclic_pulse(phase, offset, ease_in_out)
            orbit = (42.0 + 1.6 * math.sin(2 * math.pi * (phase + offset))) * supersample
            cx = center[0] + math.cos(theta) * orbit
            cy = center[1] + math.sin(theta) * orbit
            radius = (1.5 + 8.0 * pulse) * supersample
            warm_color = tuple(int(channel * (0.72 + 0.28 * pulse)) for channel in color)
            four_point_sparkle(
                base,
                (cx, cy),
                radius,
                warm_color,
                int(28 + 105 * pulse),
                math.pi / 4 + 0.22 * math.sin(2 * math.pi * phase + star_index),
            )

        frame = base.convert("RGB").resize((128, 128), Image.Resampling.LANCZOS)
        builder.add_frame(frame)

    build_info = builder.save(
        args.output,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    valid, validation = validate_gif(args.output, is_emoji=True, verbose=True)

    with Image.open(args.output) as exported:
        frames = [np.asarray(frame.convert("RGB"), dtype=np.float32) for frame in ImageSequence.Iterator(exported)]
        durations_ms = [int(frame.info.get("duration", 0)) for frame in ImageSequence.Iterator(exported)]

    transition_mae = [
        float(np.mean(np.abs(frames[(index + 1) % len(frames)] - frames[index])))
        for index in range(len(frames))
    ]
    seam_mae = transition_mae[-1]
    interior = transition_mae[:-1]
    median_interior = float(np.median(interior))
    metrics = {
        "output": args.output.name,
        "dimensions": [128, 128],
        "frame_count": len(frames),
        "fps": fps,
        "duration_seconds": sum(durations_ms) / 1000,
        "loop_count": 0,
        "slack_validator_passed": bool(valid),
        "file_size_bytes": args.output.stat().st_size,
        "seam_mae": round(seam_mae, 4),
        "median_interior_transition_mae": round(median_interior, 4),
        "seam_to_median_ratio": round(seam_mae / median_interior, 4) if median_interior else None,
        "max_transition_mae": round(max(transition_mae), 4),
        "builder": {
            "frame_count": build_info["frame_count"],
            "duration_seconds": build_info["duration_seconds"],
            "colors": build_info["colors"],
        },
        "validator": {
            "passes": validation.get("passes"),
            "width": validation.get("width"),
            "height": validation.get("height"),
            "frame_count": validation.get("frame_count"),
            "duration_seconds": validation.get("duration_seconds"),
        },
    }
    args.metrics.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
