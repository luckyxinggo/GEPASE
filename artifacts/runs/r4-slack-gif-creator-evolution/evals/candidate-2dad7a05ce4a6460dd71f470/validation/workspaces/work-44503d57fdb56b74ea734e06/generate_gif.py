#!/usr/bin/env python3
"""Generate the requested compact Slack celebration GIF."""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from core.easing import interpolate
from core.frame_composer import create_gradient_background, draw_circle, draw_star
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


WIDTH = 480
HEIGHT = 480
SCALE = 2
FPS = 14
FRAME_COUNT = 28
COLORS = 64
CENTER = (WIDTH // 2, HEIGHT // 2)


def sc(value: float) -> int:
    return int(round(value * SCALE))


def particle_specs() -> list[dict[str, object]]:
    rng = random.Random(7319)
    palette = [
        (119, 242, 169),
        (251, 211, 91),
        (94, 214, 244),
        (255, 135, 147),
        (232, 247, 239),
    ]
    specs: list[dict[str, object]] = []
    for index in range(34):
        angle = (2 * math.pi * index / 34) + rng.uniform(-0.095, 0.095)
        specs.append(
            {
                "angle": angle,
                "distance": rng.uniform(145, 208),
                "size": rng.uniform(3.8, 8.5),
                "delay": rng.uniform(0.08, 0.25),
                "curve": rng.uniform(-18, 18),
                "color": palette[index % len(palette)],
                "kind": index % 6,
            }
        )
    return specs


def draw_background() -> Image.Image:
    background = create_gradient_background(
        WIDTH * SCALE,
        HEIGHT * SCALE,
        (15, 25, 47),
        (8, 16, 34),
    ).convert("RGBA")
    layer = Image.new("RGBA", background.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    center = (sc(CENTER[0]), sc(CENTER[1]))
    for radius, alpha in [(225, 10), (178, 9), (132, 8)]:
        r = sc(radius)
        draw.ellipse(
            [center[0] - r, center[1] - r, center[0] + r, center[1] + r],
            outline=(94, 214, 244, alpha),
            width=sc(1.5),
        )
    return Image.alpha_composite(background, layer)


def draw_particles(frame: Image.Image, specs: list[dict[str, object]], t: float) -> None:
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = sc(CENTER[0]), sc(CENTER[1])

    for spec in specs:
        delay = float(spec["delay"])
        progress = max(0.0, min(1.0, (t - delay) / (0.84 - delay)))
        if progress <= 0.0 or progress >= 1.0:
            continue

        eased = interpolate(0.0, 1.0, progress, easing="ease_out")
        distance = interpolate(54.0, float(spec["distance"]), progress, easing="ease_out")
        angle = float(spec["angle"])
        curve = float(spec["curve"]) * math.sin(progress * math.pi)
        tangent_x, tangent_y = -math.sin(angle), math.cos(angle)
        x = cx + sc(math.cos(angle) * distance + tangent_x * curve)
        y = cy + sc(math.sin(angle) * distance + tangent_y * curve + 16 * progress * progress)

        fade = 1.0 if progress < 0.52 else max(0.0, 1.0 - (progress - 0.52) / 0.48)
        alpha = int(255 * fade)
        color = tuple(spec["color"])
        size = sc(float(spec["size"]) * (0.72 + 0.35 * math.sin(progress * math.pi)))
        kind = int(spec["kind"])

        if kind in (0, 3):
            tail = sc(15 + 14 * (1.0 - eased))
            tx = int(x - math.cos(angle) * tail)
            ty = int(y - math.sin(angle) * tail)
            draw.line([(tx, ty), (x, y)], fill=(*color, alpha), width=max(sc(2.4), size // 2))
            draw.ellipse([x - size, y - size, x + size, y + size], fill=(*color, alpha))
        elif kind == 1:
            draw_star(
                layer,
                (x, y),
                max(sc(3), size),
                (*color, alpha),
                (235, 255, 247, min(220, alpha)),
                sc(1),
            )
        elif kind == 2:
            rotation = angle + progress * 2.2
            length = size * 2.5
            dx, dy = math.cos(rotation) * length, math.sin(rotation) * length
            draw.line(
                [(int(x - dx), int(y - dy)), (int(x + dx), int(y + dy))],
                fill=(*color, alpha),
                width=max(sc(3), size),
            )
        else:
            draw.rounded_rectangle(
                [x - size, y - size // 2, x + size, y + size // 2],
                radius=max(sc(1), size // 3),
                fill=(*color, alpha),
            )

    frame.alpha_composite(layer)


def draw_medallion(frame: Image.Image, t: float) -> None:
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    cx, cy = sc(CENTER[0]), sc(CENTER[1])

    burst = math.sin(min(1.0, t / 0.30) * math.pi)
    settle = math.exp(-5.8 * t) * math.sin(t * 8.0 * math.pi)
    scale = 1.0 + 0.055 * burst + 0.025 * settle
    radius = sc(81 * scale)

    glow_alpha = int(36 + 28 * burst)
    glow = sc(104 + 12 * burst)
    draw_circle(layer, (cx, cy), glow, (56, 219, 141, glow_alpha))

    ring_progress = max(0.0, min(1.0, (t - 0.06) / 0.62))
    if ring_progress < 1.0:
        ring_radius = sc(interpolate(86, 147, ring_progress, easing="ease_out"))
        ring_alpha = int(118 * (1.0 - ring_progress))
        draw = ImageDraw.Draw(layer)
        draw.ellipse(
            [cx - ring_radius, cy - ring_radius, cx + ring_radius, cy + ring_radius],
            outline=(111, 245, 174, ring_alpha),
            width=sc(4),
        )

    draw_circle(
        layer,
        (cx, cy + sc(5)),
        radius + sc(5),
        (2, 10, 24, 105),
    )
    draw_circle(
        layer,
        (cx, cy),
        radius,
        (39, 194, 123, 255),
        (134, 255, 190, 255),
        sc(4),
    )
    inner_radius = max(sc(5), radius - sc(13))
    draw_circle(
        layer,
        (cx, cy),
        inner_radius,
        (31, 169, 105, 255),
        (63, 217, 137, 255),
        sc(2),
    )

    check = ImageDraw.Draw(layer)
    points = [
        (cx - sc(40 * scale), cy + sc(1 * scale)),
        (cx - sc(12 * scale), cy + sc(30 * scale)),
        (cx + sc(47 * scale), cy - sc(35 * scale)),
    ]
    check.line(points, fill=(11, 88, 61, 210), width=sc(31), joint="curve")
    check.line(points, fill=(245, 255, 248, 255), width=sc(20), joint="curve")
    for px, py in (points[0], points[-1]):
        r = sc(10)
        check.ellipse([px - r, py - r, px + r, py + r], fill=(245, 255, 248, 255))

    highlight = ImageDraw.Draw(layer)
    highlight.arc(
        [cx - sc(56), cy - sc(57), cx + sc(56), cy + sc(57)],
        start=204,
        end=315,
        fill=(197, 255, 220, 150),
        width=sc(3),
    )
    frame.alpha_composite(layer)


def build(output_path: Path) -> dict[str, object]:
    specs = particle_specs()
    base = draw_background()
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)

    for index in range(FRAME_COUNT):
        t = index / (FRAME_COUNT - 1)
        frame = base.copy()
        draw_particles(frame, specs, t)
        draw_medallion(frame, t)
        frame = frame.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        builder.add_frame(frame)

    save_info = builder.save(
        output_path,
        num_colors=COLORS,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(output_path, is_emoji=False, verbose=True)

    with Image.open(output_path) as image:
        image.seek(0)
        used_colors = len(image.convert("P").getcolors(maxcolors=256) or [])
        frame_durations_ms: list[int] = []
        actual_frame_count = 0
        while True:
            frame_durations_ms.append(int(image.info.get("duration", round(1000 / FPS))))
            actual_frame_count += 1
            try:
                image.seek(actual_frame_count)
            except EOFError:
                break

    actual_duration_seconds = sum(frame_durations_ms) / 1000
    effective_fps = actual_frame_count / actual_duration_seconds

    metadata = {
        "file": output_path.name,
        "media_type": "image/gif",
        "content": "central check mark with an outward particle burst that fades",
        "dimensions": [WIDTH, HEIGHT],
        "target_fps": FPS,
        "encoded_frame_delay_ms": frame_durations_ms[0],
        "effective_fps": round(effective_fps, 3),
        "source_frame_count": FRAME_COUNT,
        "encoded_frame_count": actual_frame_count,
        "duration_seconds": actual_duration_seconds,
        "palette_budget": COLORS,
        "first_frame_color_count": used_colors,
        "size_bytes": output_path.stat().st_size,
        "size_kb": round(output_path.stat().st_size / 1024, 2),
        "size_budget_kb": 900,
        "within_size_budget": output_path.stat().st_size <= 900 * 1024,
        "within_fps_budget": 10 <= effective_fps <= 16,
        "message_gif_validation_passed": passes,
        "builder": {
            "dimensions": save_info["dimensions"],
            "colors": save_info["colors"],
        },
        "validator": {
            "width": validation.get("width"),
            "height": validation.get("height"),
            "frame_count": validation.get("frame_count"),
            "note": "Package validator checks dimensions; timing above is recomputed from all per-frame durations.",
        },
    }
    output_path.with_name("artifact-analysis.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


if __name__ == "__main__":
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "compact_check_burst.gif")
    result = build(destination)
    if not result["within_size_budget"]:
        raise SystemExit("GIF exceeds the 900KB task budget")
    if not result["within_fps_budget"]:
        raise SystemExit("GIF falls outside the 10-16 FPS task budget")
    print(json.dumps(result, ensure_ascii=False, indent=2))
