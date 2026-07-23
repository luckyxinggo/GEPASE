#!/usr/bin/env python3
"""Generate the requested Slack emoji GIF using the supplied Skill Package."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from core.easing import calculate_arc_motion, interpolate
from core.frame_composer import create_gradient_background, draw_star
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


WIDTH = HEIGHT = 128
SCALE = 3
FRAME_COUNT = 20
FPS = 10
START = (16.0, 101.0)
DOCK = (98.0, 54.0)
ARC_HEIGHT = 64.0


def scaled(point: tuple[float, float]) -> tuple[int, int]:
    return (round(point[0] * SCALE), round(point[1] * SCALE))


def rendered_path() -> tuple[list[tuple[int, int]], list[float], list[float]]:
    positions: list[tuple[int, int]] = []
    progresses: list[float] = []
    eased_progresses: list[float] = []
    for index in range(FRAME_COUNT):
        progress = index / (FRAME_COUNT - 1)
        eased = interpolate(0.0, 1.0, progress, easing="ease_out")
        point = calculate_arc_motion(START, DOCK, ARC_HEIGHT, eased)
        positions.append((round(point[0]), round(point[1])))
        progresses.append(progress)
        eased_progresses.append(eased)

    displacements = [
        math.dist(positions[index - 1], positions[index])
        for index in range(1, len(positions))
    ]
    tail = displacements[-6:]
    tolerance = 0.01
    assert all(
        later <= earlier + tolerance for earlier, later in zip(tail, tail[1:])
    ), f"Tail displacement increased after pixel rounding: {tail}"
    assert positions[-1] == (round(DOCK[0]), round(DOCK[1]))
    return positions, displacements, eased_progresses


def draw_space(frame: Image.Image, frame_index: int) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    stars = [
        (8, 13, 1), (21, 24, 1), (37, 8, 1), (55, 26, 1),
        (75, 10, 1), (93, 20, 2), (116, 9, 1), (121, 36, 1),
        (8, 52, 1), (27, 66, 1), (112, 72, 1), (14, 83, 1),
        (44, 114, 1), (72, 120, 1), (105, 108, 1), (121, 94, 1),
    ]
    for star_index, (x, y, radius) in enumerate(stars):
        shimmer = 185 + ((frame_index * 13 + star_index * 29) % 55)
        sx, sy = scaled((x, y))
        r = radius * SCALE
        draw.ellipse((sx - r, sy - r, sx + r, sy + r), fill=(160, 215, 255, shimmer))
    draw_star(frame, scaled((31, 34)), 2 * SCALE, (224, 245, 255), (103, 179, 235), SCALE)
    draw_star(frame, scaled((115, 54)), 2 * SCALE, (211, 244, 255), None, SCALE)


def draw_orbit(frame: Image.Image) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    points = [
        scaled(calculate_arc_motion(START, DOCK, ARC_HEIGHT, sample / 80))
        for sample in range(81)
    ]
    for index in range(0, len(points) - 2, 5):
        segment = points[index : min(index + 4, len(points))]
        draw.line(segment, fill=(75, 210, 239, 98), width=1 * SCALE)


def draw_planet(frame: Image.Image) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    cx, cy = scaled((68, 78))
    radius = 24 * SCALE
    draw.ellipse(
        (cx - radius - 4 * SCALE, cy - radius + 4 * SCALE,
         cx + radius + 6 * SCALE, cy + radius + 8 * SCALE),
        fill=(3, 5, 24, 145),
    )
    for r in range(radius, 0, -2 * SCALE):
        fraction = 1 - r / radius
        color = (
            round(225 + 27 * fraction),
            round(91 + 56 * fraction),
            round(31 + 27 * fraction),
            255,
        )
        offset = round(fraction * 4 * SCALE)
        draw.ellipse((cx - r - offset, cy - r - offset, cx + r - offset, cy + r - offset), fill=color)
    draw.arc(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        110,
        300,
        fill=(118, 38, 27, 255),
        width=2 * SCALE,
    )
    craters = [(-9, 5, 4), (6, -8, 3), (10, 8, 2), (-3, 14, 2)]
    for ox, oy, r in craters:
        x, y = scaled((68 + ox, 78 + oy))
        rr = r * SCALE
        draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=(184, 65, 34, 140))
        draw.arc((x - rr, y - rr, x + rr, y + rr), 190, 345, fill=(255, 181, 86, 210), width=SCALE)
    hx, hy = scaled((60, 68))
    draw.ellipse((hx - 5 * SCALE, hy - 4 * SCALE, hx + 5 * SCALE, hy + 4 * SCALE), fill=(255, 203, 105, 178))


def draw_dock(frame: Image.Image, frame_index: int) -> None:
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    x, y = scaled(DOCK)
    pulse = (frame_index % 5) / 4
    radius = round((8 + 3 * pulse) * SCALE)
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=(55, 237, 244, round(80 * (1 - pulse))), width=SCALE)
    bracket = 8 * SCALE
    gap = 4 * SCALE
    color = (139, 247, 255, 190)
    draw.line((x - bracket, y - bracket, x - gap, y - bracket), fill=color, width=SCALE)
    draw.line((x - bracket, y - bracket, x - bracket, y - gap), fill=color, width=SCALE)
    draw.line((x + gap, y + bracket, x + bracket, y + bracket), fill=color, width=SCALE)
    draw.line((x + bracket, y + gap, x + bracket, y + bracket), fill=color, width=SCALE)
    frame.alpha_composite(overlay)


def draw_satellite(
    frame: Image.Image,
    center: tuple[int, int],
    eased_progress: float,
) -> None:
    p1 = max(0.0, eased_progress - 0.004)
    p2 = min(1.0, eased_progress + 0.004)
    before = calculate_arc_motion(START, DOCK, ARC_HEIGHT, p1)
    after = calculate_arc_motion(START, DOCK, ARC_HEIGHT, p2)
    angle = math.degrees(math.atan2(after[1] - before[1], after[0] - before[0]))

    sprite = Image.new("RGBA", (54 * SCALE, 34 * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sprite, "RGBA")
    mx, my = sprite.width // 2, sprite.height // 2
    panel_color = (21, 172, 194, 255)
    edge_color = (5, 47, 75, 255)
    for sign in (-1, 1):
        px = mx + sign * 12 * SCALE
        draw.rounded_rectangle(
            (px - 8 * SCALE, my - 5 * SCALE, px + 8 * SCALE, my + 5 * SCALE),
            radius=SCALE,
            fill=panel_color,
            outline=edge_color,
            width=2 * SCALE,
        )
        draw.line((px, my - 4 * SCALE, px, my + 4 * SCALE), fill=(91, 239, 245, 220), width=SCALE)
        draw.line((px - 7 * SCALE, my, px + 7 * SCALE, my), fill=(8, 92, 123, 220), width=SCALE)
    draw.line((mx - 7 * SCALE, my, mx - 11 * SCALE, my), fill=edge_color, width=2 * SCALE)
    draw.line((mx + 7 * SCALE, my, mx + 11 * SCALE, my), fill=edge_color, width=2 * SCALE)
    draw.rounded_rectangle(
        (mx - 8 * SCALE, my - 6 * SCALE, mx + 8 * SCALE, my + 6 * SCALE),
        radius=5 * SCALE,
        fill=(46, 237, 242, 255),
        outline=edge_color,
        width=2 * SCALE,
    )
    draw.ellipse((mx - 4 * SCALE, my - 4 * SCALE, mx + 1 * SCALE, my + SCALE), fill=(190, 255, 255, 235))
    draw.line((mx + 3 * SCALE, my - 5 * SCALE, mx + 8 * SCALE, my - 11 * SCALE), fill=(112, 244, 248, 255), width=SCALE)
    draw.ellipse((mx + 6 * SCALE, my - 13 * SCALE, mx + 10 * SCALE, my - 9 * SCALE), fill=(246, 255, 255, 255))

    rotated = sprite.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True)
    cx, cy = scaled(center)
    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow.alpha_composite(rotated, (cx - rotated.width // 2 + 2 * SCALE, cy - rotated.height // 2 + 3 * SCALE))
    shadow = shadow.filter(ImageFilter.GaussianBlur(2 * SCALE))
    alpha = shadow.getchannel("A").point(lambda value: round(value * 0.38))
    shadow.putalpha(alpha)
    frame.alpha_composite(shadow)
    frame.alpha_composite(rotated, (cx - rotated.width // 2, cy - rotated.height // 2))


def make_frame(index: int, center: tuple[int, int], eased_progress: float) -> Image.Image:
    frame = create_gradient_background(
        WIDTH * SCALE,
        HEIGHT * SCALE,
        (3, 7, 31),
        (16, 14, 66),
    ).convert("RGBA")
    draw_space(frame, index)
    draw_orbit(frame)
    draw_planet(frame)
    draw_dock(frame, index)
    draw_satellite(frame, center, eased_progress)
    return frame.convert("RGB")


def main() -> None:
    output_dir = Path(__file__).resolve().parent
    output_path = output_dir / "satellite_orbit_ease.gif"
    positions, displacements, eased_progresses = rendered_path()

    frames = [
        make_frame(index, positions[index], eased_progresses[index])
        for index in range(FRAME_COUNT)
    ]
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    builder.add_frames(frames)
    build_info = builder.save(
        output_path,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(output_path, is_emoji=True, verbose=True)
    assert passes
    assert validation["width"] == WIDTH and validation["height"] == HEIGHT
    assert validation["duration_seconds"] <= 2.7

    artifact_path = (
        "workspaces/work-2feaa3a4dbb47f95a5b9aefa/"
        "satellite_orbit_ease.gif"
    )
    build_info["path"] = artifact_path
    validation["file"] = artifact_path

    preview_frames = [frames[index].resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS) for index in (0, 5, 10, 15, 19)]
    preview = Image.new("RGB", (WIDTH * len(preview_frames), HEIGHT), (0, 0, 0))
    for index, preview_frame in enumerate(preview_frames):
        preview.paste(preview_frame, (index * WIDTH, 0))
    preview.save(output_dir / "satellite_orbit_ease_preview.png")

    report = {
        "output": output_path.name,
        "build": build_info,
        "validation": validation,
        "motion": {
            "easing": "ease_out",
            "path": "continuous parabolic arc",
            "start_pixel": list(positions[0]),
            "dock_pixel": list(positions[-1]),
            "adjacent_pixel_displacements": [round(value, 3) for value in displacements],
            "tail_displacements": [round(value, 3) for value in displacements[-6:]],
            "tail_non_increasing": True,
            "dock_exact_after_integerization": positions[-1] == (round(DOCK[0]), round(DOCK[1])),
        },
    }
    (output_dir / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
