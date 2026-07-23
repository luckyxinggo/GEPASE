#!/usr/bin/env python3
"""Render the requested compact Slack message GIF with the candidate package."""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw


PACKAGE_ROOT = Path(
    "artifacts/runs/r4-slack-gif-creator-evolution/"
    "candidate-workspaces/applications/application-da89180cab97e9b294bda455"
)
sys.path.insert(0, str(PACKAGE_ROOT))

from core.easing import interpolate  # noqa: E402
from core.frame_composer import create_blank_frame, draw_circle, draw_star  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


WIDTH = 480
HEIGHT = 480
FPS = 12
FRAME_COUNT = 24
SCALE = 2
OUTPUT = Path(
    "artifacts/runs/r4-slack-gif-creator-evolution/evals/"
    "candidate-4b5885d1dd5fe978c8f1dad3/validation/workspaces/"
    "work-96e4bf561b46c20bb344924d/compact_check_burst.gif"
)


def scaled_frame() -> Image.Image:
    """Create a restrained backdrop that compresses cleanly."""
    frame = create_blank_frame(WIDTH * SCALE, HEIGHT * SCALE, (9, 20, 39))
    draw = ImageDraw.Draw(frame)
    # Broad nested panels give depth without a costly full-frame gradient.
    draw.ellipse((74, 74, 886, 886), fill=(11, 28, 50))
    draw.ellipse((152, 152, 808, 808), fill=(13, 35, 57))
    return frame


def make_particles() -> list[dict[str, float | tuple[int, int, int] | str]]:
    random.seed(707)
    palette = [(82, 255, 187), (255, 211, 92), (113, 198, 255), (255, 113, 176)]
    particles: list[dict[str, float | tuple[int, int, int] | str]] = []
    for index in range(24):
        angle = (2 * math.pi * index / 24) + random.uniform(-0.075, 0.075)
        particles.append(
            {
                "angle": angle,
                "distance": random.uniform(132, 208),
                "delay": random.uniform(0.02, 0.15),
                "size": random.uniform(4.5, 9.5),
                "color": palette[index % len(palette)],
                "shape": "star" if index % 6 == 0 else "dot",
            }
        )
    return particles


def draw_check(draw: ImageDraw.ImageDraw, progress: float, pulse: float) -> None:
    """Draw a high-contrast central badge and a progressively revealed check."""
    cx = cy = 240 * SCALE
    radius = int(92 * SCALE * pulse)
    draw.ellipse(
        (cx - radius - 7, cy - radius - 7, cx + radius + 7, cy + radius + 7),
        fill=(6, 22, 35),
        outline=(53, 229, 166),
        width=7 * SCALE,
    )
    draw.ellipse(
        (cx - radius + 8, cy - radius + 8, cx + radius - 8, cy + radius - 8),
        fill=(15, 66, 72),
        outline=(95, 255, 195),
        width=3 * SCALE,
    )

    points = [(183 * SCALE, 243 * SCALE), (225 * SCALE, 284 * SCALE), (304 * SCALE, 194 * SCALE)]
    first_len = math.dist(points[0], points[1])
    second_len = math.dist(points[1], points[2])
    total = first_len + second_len
    reveal = max(0.0, min(1.0, progress)) * total
    visible = [points[0]]
    if reveal <= first_len:
        ratio = reveal / first_len if first_len else 0
        visible.append(
            (
                points[0][0] + (points[1][0] - points[0][0]) * ratio,
                points[0][1] + (points[1][1] - points[0][1]) * ratio,
            )
        )
    else:
        visible.append(points[1])
        ratio = min(1.0, (reveal - first_len) / second_len)
        visible.append(
            (
                points[1][0] + (points[2][0] - points[1][0]) * ratio,
                points[1][1] + (points[2][1] - points[1][1]) * ratio,
            )
        )

    if len(visible) >= 2:
        draw.line(visible, fill=(1, 24, 29), width=38 * SCALE, joint="curve")
        draw.line(visible, fill=(239, 255, 248), width=24 * SCALE, joint="curve")
        for point in (visible[0], visible[-1]):
            r = 12 * SCALE
            draw.ellipse((point[0] - r, point[1] - r, point[0] + r, point[1] + r), fill=(239, 255, 248))


def render() -> dict:
    particles = make_particles()
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)

    for frame_index in range(FRAME_COUNT):
        t = frame_index / (FRAME_COUNT - 1)
        frame = scaled_frame()

        # Two expanding rings support the burst while leaving the check silhouette clear.
        ring_draw = ImageDraw.Draw(frame)
        ring_t = min(1.0, max(0.0, (t - 0.08) / 0.62))
        ring_radius = int(interpolate(94, 184, ring_t, easing="ease_out") * SCALE)
        ring_strength = max(0.0, 1.0 - ring_t)
        ring_color = (
            int(13 + 50 * ring_strength),
            int(35 + 130 * ring_strength),
            int(57 + 90 * ring_strength),
        )
        ring_draw.ellipse(
            (
                240 * SCALE - ring_radius,
                240 * SCALE - ring_radius,
                240 * SCALE + ring_radius,
                240 * SCALE + ring_radius,
            ),
            outline=ring_color,
            width=4 * SCALE,
        )

        # Particles travel on fixed rays with eased outward motion and a clean fade.
        for particle in particles:
            local_t = max(0.0, min(1.0, (t - float(particle["delay"])) / 0.78))
            distance = interpolate(74, float(particle["distance"]), local_t, easing="ease_out")
            fade = max(0.0, 1.0 - local_t**1.7)
            size = max(1, int(float(particle["size"]) * (0.6 + 0.65 * fade) * SCALE))
            angle = float(particle["angle"])
            x = int((240 + math.cos(angle) * distance) * SCALE)
            y = int((240 + math.sin(angle) * distance) * SCALE)
            base = particle["color"]
            assert isinstance(base, tuple)
            bg = (13, 35, 57)
            color = tuple(int(bg[i] + (base[i] - bg[i]) * fade) for i in range(3))
            if particle["shape"] == "star" and fade > 0.12:
                draw_star(frame, (x, y), size, color)
            else:
                draw_circle(frame, (x, y), size, fill_color=color)

        check_progress = interpolate(0.0, 1.0, min(1.0, t / 0.43), easing="ease_out")
        pulse = 1.0 + (0.035 * math.sin((t - 0.35) * math.pi * 3) if t > 0.35 else 0.0)
        draw_check(ImageDraw.Draw(frame), check_progress, pulse)

        frame = frame.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        builder.add_frame(frame)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    info = builder.save(OUTPUT, num_colors=40, remove_duplicates=False)
    passes, validation = validate_gif(OUTPUT, is_emoji=False, verbose=True)
    if not passes:
        raise RuntimeError(f"Slack message GIF validation failed: {validation}")
    if OUTPUT.stat().st_size > 900 * 1024:
        raise RuntimeError(f"GIF exceeds 900KB: {OUTPUT.stat().st_size} bytes")
    if not (10 <= info["fps"] <= 16):
        raise RuntimeError(f"GIF FPS is outside the required budget: {info['fps']}")
    return info


if __name__ == "__main__":
    render()
