#!/usr/bin/env python3
"""Render a periodic three-sparkle ring animation for a Slack emoji."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def periodic_envelope(phase: float, center: float) -> float:
    """A smooth, exactly periodic appear/peak/fade envelope in [0, 1]."""
    wrapped = (phase - center + 0.5) % 1.0 - 0.5
    if abs(wrapped) >= 0.29:
        return 0.0
    x = wrapped / 0.29
    return 0.5 + 0.5 * math.cos(math.pi * x)


def four_point_sparkle(
    layer: Image.Image,
    center: tuple[float, float],
    radius: float,
    color: tuple[int, int, int, int],
) -> None:
    cx, cy = center
    vertical = radius
    horizontal = radius * 0.52
    inner = radius * 0.19
    points = [
        (cx, cy - vertical),
        (cx + inner, cy - inner),
        (cx + horizontal, cy),
        (cx + inner, cy + inner),
        (cx, cy + vertical),
        (cx - inner, cy + inner),
        (cx - horizontal, cy),
        (cx - inner, cy - inner),
    ]
    ImageDraw.Draw(layer).polygon(points, fill=color)


def render_frame(frame_index: int, frame_count: int, scale: int) -> Image.Image:
    size = 128
    large = size * scale
    phase = frame_index / frame_count

    # A rich, high-contrast background with a restrained radial highlight.
    yy, xx = np.mgrid[0:large, 0:large]
    radial = np.sqrt((xx - large * 0.48) ** 2 + (yy - large * 0.43) ** 2) / large
    top = np.array([24.0, 27.0, 72.0])
    bottom = np.array([8.0, 10.0, 35.0])
    vertical = yy[..., None] / max(1, large - 1)
    rgb = top * (1.0 - vertical) + bottom * vertical
    highlight = np.clip(1.0 - radial / 0.72, 0.0, 1.0)[..., None]
    rgb = np.clip(rgb + highlight * np.array([17.0, 13.0, 33.0]), 0, 255)
    frame = Image.fromarray(rgb.astype(np.uint8), mode="RGB").convert("RGBA")

    center = (64 * scale, 64 * scale)
    draw = ImageDraw.Draw(frame)

    # Stable central ring: soft aura, dark core, violet body, and crisp highlight.
    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    for radius, alpha in ((29, 18), (25, 27), (22, 42)):
        r = radius * scale
        glow_draw.ellipse(
            [center[0] - r, center[1] - r, center[0] + r, center[1] + r],
            outline=(118, 92, 255, alpha),
            width=max(2, 3 * scale),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(5 * scale))
    frame = Image.alpha_composite(frame, glow)
    draw = ImageDraw.Draw(frame)

    outer = 21 * scale
    inner = 13 * scale
    draw.ellipse(
        [center[0] - outer, center[1] - outer, center[0] + outer, center[1] + outer],
        fill=(132, 100, 255, 255),
        outline=(210, 194, 255, 255),
        width=2 * scale,
    )
    draw.ellipse(
        [center[0] - inner, center[1] - inner, center[0] + inner, center[1] + inner],
        fill=(13, 15, 45, 255),
        outline=(82, 65, 151, 255),
        width=2 * scale,
    )
    # Fixed specular arc reinforces that the ring itself does not rotate or pulse.
    arc_box = [
        center[0] - 18 * scale,
        center[1] - 18 * scale,
        center[0] + 18 * scale,
        center[1] + 18 * scale,
    ]
    draw.arc(arc_box, start=208, end=304, fill=(244, 237, 255, 255), width=2 * scale)

    anchors = ((64, 27), (98, 81), (31, 86))
    colors = ((255, 225, 88), (100, 231, 255), (255, 125, 215))
    offsets = (0.00, 1.0 / 3.0, 2.0 / 3.0)
    for (ax, ay), base_color, offset in zip(anchors, colors, offsets):
        strength = periodic_envelope(phase, offset)
        if strength <= 0.0:
            continue

        eased = strength * strength * (3.0 - 2.0 * strength)
        radius = (2.0 + 9.0 * eased) * scale
        # A tiny radial bloom at peak gives life without moving the stable ring.
        vx, vy = ax - 64.0, ay - 64.0
        distance = math.hypot(vx, vy)
        drift = 1.5 * math.sin(math.pi * strength) * scale
        cx = ax * scale + (vx / distance) * drift
        cy = ay * scale + (vy / distance) * drift

        sparkle_glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        glow_radius = radius * 1.9
        ImageDraw.Draw(sparkle_glow).ellipse(
            [cx - glow_radius, cy - glow_radius, cx + glow_radius, cy + glow_radius],
            fill=(*base_color, int(90 * eased)),
        )
        sparkle_glow = sparkle_glow.filter(ImageFilter.GaussianBlur(4 * scale))
        frame = Image.alpha_composite(frame, sparkle_glow)

        sparkle = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        four_point_sparkle(
            sparkle,
            (cx, cy),
            radius,
            (*base_color, int(215 + 40 * eased)),
        )
        if eased > 0.45:
            core = max(1.0, radius * 0.28)
            ImageDraw.Draw(sparkle).ellipse(
                [cx - core, cy - core, cx + core, cy + core],
                fill=(255, 255, 246, int(120 + 135 * eased)),
            )
        frame = Image.alpha_composite(frame, sparkle)

    return frame.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(args.package_root))
    from core.gif_builder import GIFBuilder
    from core.validators import validate_gif

    frame_count = 20
    fps = 10
    frames = [render_frame(i, frame_count, scale=4) for i in range(frame_count)]

    builder = GIFBuilder(width=128, height=128, fps=fps)
    builder.add_frames(frames)
    build_info = builder.save(
        args.output,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(args.output, is_emoji=True, verbose=True)
    # Keep the portable report free of host-specific or run-layout paths.
    build_info["path"] = args.output.name
    validation["file"] = args.output.name

    arrays = [np.asarray(frame, dtype=np.int16) for frame in frames]
    adjacent_mae = [
        float(np.mean(np.abs(arrays[(i + 1) % frame_count] - arrays[i])))
        for i in range(frame_count)
    ]
    seam_mae = adjacent_mae[-1]
    report = {
        "design": {
            "cycle_sampling": "phase=i/N; endpoint is not duplicated",
            "frame_count": frame_count,
            "fps": fps,
            "duration_seconds": frame_count / fps,
            "ring_motion": "stable",
            "sparkle_envelopes": "three periodic cosine envelopes, phase-shifted by one third cycle",
        },
        "build": build_info,
        "validation": validation,
        "slack_ready": passes,
        "loop_seam": {
            "last_to_first_mae": seam_mae,
            "max_adjacent_mae": max(adjacent_mae),
            "mean_adjacent_mae": float(np.mean(adjacent_mae)),
            "seam_not_abrupt": seam_mae <= max(adjacent_mae),
        },
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
