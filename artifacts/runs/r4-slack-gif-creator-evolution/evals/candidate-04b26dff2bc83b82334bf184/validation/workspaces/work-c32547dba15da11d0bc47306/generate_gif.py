#!/usr/bin/env python3
"""Generate the requested compact Slack celebration GIF."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw

from core.easing import interpolate
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


WIDTH = 480
HEIGHT = 480
FPS = 12
FRAME_COUNT = 24
CENTER = (240, 240)
OUTPUT = Path("compact_check_burst.gif")


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def segment_progress(points: list[tuple[float, float]], progress: float):
    lengths = [
        math.dist(points[index], points[index + 1])
        for index in range(len(points) - 1)
    ]
    target = sum(lengths) * clamp(progress)
    rendered = [points[0]]
    for index, length in enumerate(lengths):
        if target >= length:
            rendered.append(points[index + 1])
            target -= length
        else:
            x0, y0 = points[index]
            x1, y1 = points[index + 1]
            ratio = target / length if length else 0
            rendered.append((x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio))
            break
    return rendered


def rgba_layer() -> Image.Image:
    return Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))


def compose_frame(index: int, particles: list[dict]) -> Image.Image:
    t = index / (FRAME_COUNT - 1)
    background = Image.new("RGBA", (WIDTH, HEIGHT), (9, 15, 31, 255))

    # Quiet, low-color radial depth keeps the center readable without inflating size.
    ambience = rgba_layer()
    ambient_draw = ImageDraw.Draw(ambience)
    for radius, color in (
        (200, (18, 42, 66, 30)),
        (160, (15, 63, 74, 34)),
        (122, (16, 102, 93, 30)),
    ):
        ambient_draw.ellipse(
            (240 - radius, 240 - radius, 240 + radius, 240 + radius), fill=color
        )
    background = Image.alpha_composite(background, ambience)

    # A brief expanding halo emphasizes the outward burst.
    burst_t = clamp((t - 0.06) / 0.72)
    burst_eased = interpolate(0.0, 1.0, burst_t, "ease_out")
    halo = rgba_layer()
    halo_draw = ImageDraw.Draw(halo)
    halo_alpha = int(120 * (1.0 - burst_t) ** 1.6)
    if halo_alpha > 2:
        radius = int(78 + 145 * burst_eased)
        halo_draw.ellipse(
            (240 - radius, 240 - radius, 240 + radius, 240 + radius),
            outline=(48, 231, 183, halo_alpha),
            width=5,
        )
    background = Image.alpha_composite(background, halo)

    # Rays and particles share one eased radial progress, preserving smooth motion.
    particle_layer = rgba_layer()
    particle_draw = ImageDraw.Draw(particle_layer)
    ray_palette = [(48, 231, 183), (77, 208, 225), (255, 207, 84)]
    for ray_index in range(12):
        angle = (2 * math.pi * ray_index / 12) + 0.07
        local = clamp((t - 0.08 - (ray_index % 3) * 0.012) / 0.54)
        eased = interpolate(0.0, 1.0, local, "ease_out")
        alpha = int(150 * (1.0 - local) ** 1.5)
        if alpha <= 3:
            continue
        inner = 104 + 31 * eased
        outer = inner + 20 + 38 * (1.0 - local)
        p0 = (240 + math.cos(angle) * inner, 240 + math.sin(angle) * inner)
        p1 = (240 + math.cos(angle) * outer, 240 + math.sin(angle) * outer)
        particle_draw.line([p0, p1], fill=ray_palette[ray_index % 3] + (alpha,), width=5)

    for particle in particles:
        local = clamp((t - particle["delay"]) / (1.0 - particle["delay"]))
        if local <= 0:
            continue
        eased = interpolate(0.0, 1.0, local, "ease_out")
        distance = 62 + particle["distance"] * eased
        angle = particle["angle"] + particle["curve"] * math.sin(local * math.pi)
        x = 240 + math.cos(angle) * distance
        y = 240 + math.sin(angle) * distance + particle["gravity"] * local * local
        # Retain low-alpha motion through the penultimate frame so the encoder
        # preserves the requested cadence instead of coalescing a static tail.
        alpha = int(255 * clamp((1.0 - local) ** 0.72))
        size = max(1.5, particle["size"] * (1.0 - 0.42 * local))
        color = particle["color"] + (alpha,)
        if particle["shape"] == "diamond":
            points = [(x, y - size), (x + size, y), (x, y + size), (x - size, y)]
            particle_draw.polygon(points, fill=color)
        else:
            particle_draw.ellipse((x - size, y - size, x + size, y + size), fill=color)
    background = Image.alpha_composite(background, particle_layer)

    # The central badge arrives quickly, then settles as particles fade around it.
    badge_t = clamp((t - 0.01) / 0.33)
    badge_scale = interpolate(0.12, 1.0, badge_t, "back_out")
    badge_scale = max(0.08, badge_scale)
    badge = rgba_layer()
    badge_draw = ImageDraw.Draw(badge)
    radius = int(90 * badge_scale)
    glow_radius = radius + 20
    badge_draw.ellipse(
        (240 - glow_radius, 240 - glow_radius, 240 + glow_radius, 240 + glow_radius),
        fill=(26, 221, 165, 32),
    )
    badge_draw.ellipse(
        (240 - radius, 240 - radius, 240 + radius, 240 + radius),
        fill=(22, 184, 134, 255),
        outline=(81, 241, 194, 255),
        width=max(3, int(6 * badge_scale)),
    )
    inner_radius = max(1, radius - 12)
    badge_draw.ellipse(
        (
            240 - inner_radius,
            240 - inner_radius,
            240 + inner_radius,
            240 + inner_radius,
        ),
        outline=(10, 123, 105, 150),
        width=max(2, int(4 * badge_scale)),
    )
    background = Image.alpha_composite(background, badge)

    # Draw a hand-rendered, round-ended check for a bold readable silhouette.
    check_t = clamp((t - 0.11) / 0.38)
    check_t = interpolate(0.0, 1.0, check_t, "ease_out")
    if check_t > 0:
        check_layer = rgba_layer()
        check_draw = ImageDraw.Draw(check_layer)
        points = segment_progress([(183, 241), (224, 281), (305, 193)], check_t)
        outline_width = 39
        stroke_width = 25
        check_draw.line(points, fill=(5, 91, 80, 210), width=outline_width, joint="curve")
        check_draw.line(points, fill=(247, 255, 252, 255), width=stroke_width, joint="curve")
        for point in (points[0], points[-1]):
            x, y = point
            check_draw.ellipse(
                (x - outline_width / 2, y - outline_width / 2, x + outline_width / 2, y + outline_width / 2),
                fill=(5, 91, 80, 210),
            )
            check_draw.ellipse(
                (x - stroke_width / 2, y - stroke_width / 2, x + stroke_width / 2, y + stroke_width / 2),
                fill=(247, 255, 252, 255),
            )
        background = Image.alpha_composite(background, check_layer)

    return background.convert("RGB")


def main() -> None:
    random.seed(20260722)
    palette = [
        (48, 231, 183),
        (77, 208, 225),
        (255, 207, 84),
        (255, 116, 139),
        (170, 130, 255),
    ]
    particles = []
    for index in range(34):
        particles.append(
            {
                "angle": (2 * math.pi * index / 34) + random.uniform(-0.12, 0.12),
                "distance": random.uniform(105, 176),
                "size": random.uniform(4.0, 8.5),
                "delay": random.uniform(0.055, 0.16),
                "curve": random.uniform(-0.13, 0.13),
                "gravity": random.uniform(5.0, 24.0),
                "color": palette[index % len(palette)],
                "shape": "diamond" if index % 3 == 0 else "circle",
            }
        )

    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    builder.add_frames([compose_frame(index, particles) for index in range(FRAME_COUNT)])
    build_info = builder.save(
        OUTPUT, num_colors=48, optimize_for_emoji=False, remove_duplicates=False
    )
    passes, validation = validate_gif(OUTPUT, is_emoji=False, verbose=True)

    with Image.open(OUTPUT) as gif:
        durations = []
        for frame_index in range(gif.n_frames):
            gif.seek(frame_index)
            durations.append(int(gif.info.get("duration", 0)))
    total_ms = sum(durations)
    measured_fps = len(durations) / (total_ms / 1000) if total_ms else 0
    metadata = {
        "build": build_info,
        "validation": validation,
        "dimension_pass": passes,
        "file_size_budget_kb": 900,
        "file_size_budget_pass": OUTPUT.stat().st_size <= 900 * 1024,
        "fps_budget": [10, 16],
        "measured_fps": measured_fps,
        "fps_budget_pass": 10 <= measured_fps <= 16,
        "frame_durations_ms": sorted(set(durations)),
    }
    Path("artifact-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if not passes:
        raise SystemExit("Slack message dimensions failed validation")
    if not metadata["file_size_budget_pass"]:
        raise SystemExit("GIF exceeds 900KB budget")
    if not metadata["fps_budget_pass"]:
        raise SystemExit("GIF falls outside the 10-16 FPS budget")


if __name__ == "__main__":
    main()
