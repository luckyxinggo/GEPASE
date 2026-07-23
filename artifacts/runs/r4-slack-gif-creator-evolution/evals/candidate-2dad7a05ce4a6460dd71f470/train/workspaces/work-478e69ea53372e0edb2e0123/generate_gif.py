from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


SKILL_ROOT = Path(
    "artifacts/runs/r4-slack-gif-creator-evolution/candidate-workspaces/"
    "applications/application-20ada438d49e648f1bb86749"
)
WORKSPACE = Path(
    "artifacts/runs/r4-slack-gif-creator-evolution/evals/"
    "candidate-2dad7a05ce4a6460dd71f470/train/workspaces/"
    "work-478e69ea53372e0edb2e0123"
)
OUTPUT = WORKSPACE / "satellite_orbit_ease.gif"

sys.path.insert(0, str(SKILL_ROOT))

from core.easing import calculate_arc_motion, interpolate  # noqa: E402
from core.frame_composer import create_gradient_background  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


SCALE = 3
WIDTH = HEIGHT = 128
FPS = 10
FRAME_COUNT = 24
START = (16.0, 102.0)
END = (108.0, 48.0)
ARC_HEIGHT = 45.0


def sc(value: float) -> int:
    return round(value * SCALE)


def point_sc(point: tuple[float, float]) -> tuple[int, int]:
    return sc(point[0]), sc(point[1])


def draw_stars(frame: Image.Image, frame_index: int) -> None:
    stars = [
        (8, 13, 0), (20, 28, 2), (35, 11, 4), (51, 18, 1),
        (73, 9, 3), (94, 16, 5), (117, 10, 1), (113, 29, 4),
        (9, 51, 3), (28, 59, 5), (46, 48, 0), (119, 67, 2),
        (14, 79, 4), (33, 87, 1), (110, 91, 5), (94, 108, 0),
        (54, 115, 3), (19, 119, 2), (121, 118, 4),
    ]
    draw = ImageDraw.Draw(frame)
    for x, y, phase in stars:
        pulse = 0.5 + 0.5 * math.sin((frame_index + phase) * math.pi / 6)
        level = round(132 + 98 * pulse)
        radius = 1 if (x + y) % 3 else 1.45
        r = sc(radius)
        draw.ellipse(
            [sc(x) - r, sc(y) - r, sc(x) + r, sc(y) + r],
            fill=(level, min(255, level + 14), 255),
        )
        if radius > 1:
            draw.line([(sc(x - 2.2), sc(y)), (sc(x + 2.2), sc(y))], fill=(90, 150, 202), width=sc(0.5))
            draw.line([(sc(x), sc(y - 2.2)), (sc(x), sc(y + 2.2))], fill=(90, 150, 202), width=sc(0.5))


def arc_position(raw_progress: float) -> tuple[float, float]:
    eased = interpolate(0.0, 1.0, raw_progress, easing="ease_out")
    return calculate_arc_motion(START, END, ARC_HEIGHT, eased)


def draw_orbit_path(frame: Image.Image) -> None:
    draw = ImageDraw.Draw(frame)
    samples = [calculate_arc_motion(START, END, ARC_HEIGHT, i / 60) for i in range(61)]
    for i in range(0, 60, 3):
        p1, p2 = point_sc(samples[i]), point_sc(samples[min(i + 1, 60)])
        draw.line([p1, p2], fill=(44, 126, 162), width=sc(1.25))


def draw_planet(frame: Image.Image) -> None:
    center = (73, 77)
    cx, cy = point_sc(center)
    draw = ImageDraw.Draw(frame)

    # Back half of a warm orbital ring.
    ring_box = [sc(42), sc(62), sc(105), sc(94)]
    draw.ellipse(ring_box, outline=(139, 72, 31), width=sc(3))
    draw.ellipse([v + sc(1) for v in ring_box], outline=(245, 145, 52), width=sc(1))

    # Soft orange atmospheric glow.
    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        [cx - sc(31), cy - sc(31), cx + sc(31), cy + sc(31)],
        fill=(255, 111, 32, 70),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(sc(5)))
    frame.paste(Image.alpha_composite(frame.convert("RGBA"), glow).convert("RGB"))
    draw = ImageDraw.Draw(frame)

    # Concentric shading makes the orange planet feel spherical.
    for radius in range(sc(27), 0, -1):
        ratio = radius / sc(27)
        red = round(232 + 19 * (1 - ratio))
        green = round(75 + 58 * (1 - ratio))
        blue = round(23 + 17 * (1 - ratio))
        offset_x = round(sc(4) * (1 - ratio))
        offset_y = round(sc(4) * (1 - ratio))
        draw.ellipse(
            [cx - radius + offset_x, cy - radius + offset_y,
             cx + radius + offset_x, cy + radius + offset_y],
            fill=(red, green, blue),
        )
    draw.ellipse(
        [cx - sc(27), cy - sc(27), cx + sc(27), cy + sc(27)],
        outline=(120, 47, 28), width=sc(2),
    )

    # Craters, terminator shadow, and highlight.
    draw.ellipse([sc(58), sc(68), sc(68), sc(76)], fill=(182, 67, 28), outline=(255, 157, 66), width=sc(1))
    draw.ellipse([sc(79), sc(83), sc(91), sc(92)], fill=(190, 62, 29), outline=(244, 132, 54), width=sc(1))
    draw.ellipse([sc(70), sc(56), sc(77), sc(62)], fill=(221, 91, 31))
    draw.arc([sc(50), sc(54), sc(91), sc(94)], 245, 80, fill=(104, 38, 38), width=sc(3))
    draw.ellipse([sc(57), sc(57), sc(64), sc(64)], fill=(255, 184, 92))

    # Front half of the ring crosses the planet for depth.
    draw.arc(ring_box, 9, 171, fill=(255, 176, 66), width=sc(3))
    draw.arc(ring_box, 12, 168, fill=(255, 225, 128), width=sc(1))


def make_satellite_sprite(angle_degrees: float, thrust: float) -> Image.Image:
    sprite = Image.new("RGBA", (sc(36), sc(24)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sprite)

    # Speed-dependent ion trail, strongest early and fading toward docking.
    if thrust > 0.02:
        trail = round(sc(7) * thrust)
        draw.polygon(
            [(sc(8), sc(10)), (sc(8 - trail), sc(12)), (sc(8), sc(14))],
            fill=(64, 245, 255, round(180 * thrust)),
        )

    # Solar-panel wings with cell lines.
    draw.rounded_rectangle([sc(1), sc(8), sc(10), sc(16)], radius=sc(1), fill=(18, 94, 174), outline=(69, 222, 244), width=sc(1))
    draw.rounded_rectangle([sc(26), sc(8), sc(35), sc(16)], radius=sc(1), fill=(18, 94, 174), outline=(69, 222, 244), width=sc(1))
    for x in (4, 7, 29, 32):
        draw.line([(sc(x), sc(9)), (sc(x), sc(15))], fill=(104, 201, 242), width=1)
    draw.line([(sc(2), sc(12)), (sc(9), sc(12))], fill=(104, 201, 242), width=1)
    draw.line([(sc(27), sc(12)), (sc(34), sc(12))], fill=(104, 201, 242), width=1)

    # Cyan craft body, window, antenna, and small docking nose.
    draw.rounded_rectangle([sc(9), sc(6), sc(27), sc(18)], radius=sc(5), fill=(36, 224, 225), outline=(7, 74, 102), width=sc(1.4))
    draw.ellipse([sc(15), sc(8), sc(21), sc(14)], fill=(9, 62, 106), outline=(191, 255, 255), width=sc(1))
    draw.ellipse([sc(16), sc(8.5), sc(18), sc(10.5)], fill=(230, 255, 255))
    draw.line([(sc(20), sc(6)), (sc(23), sc(2.5))], fill=(117, 250, 245), width=sc(1))
    draw.ellipse([sc(22), sc(1.5), sc(24), sc(3.5)], fill=(255, 202, 74))
    draw.rectangle([sc(27), sc(10), sc(30), sc(14)], fill=(130, 255, 245), outline=(7, 74, 102), width=sc(1))

    return sprite.rotate(angle_degrees, resample=Image.Resampling.BICUBIC, expand=True)


def draw_docking_target(frame: Image.Image, settle: float) -> None:
    draw = ImageDraw.Draw(frame)
    cx, cy = point_sc(END)
    pulse = sc(3 + 2 * settle)
    color = (65, round(170 + 75 * settle), 232)
    draw.arc([cx - pulse, cy - pulse, cx + pulse, cy + pulse], 115, 245, fill=color, width=sc(1))
    draw.ellipse([cx + sc(4), cy - sc(1.5), cx + sc(7), cy + sc(1.5)], fill=(255, 205, 72))


def render_frame(frame_index: int) -> Image.Image:
    raw = frame_index / (FRAME_COUNT - 1)
    position = arc_position(raw)
    next_raw = min(1.0, raw + 0.008)
    previous_raw = max(0.0, raw - 0.008)
    p_next = arc_position(next_raw)
    p_previous = arc_position(previous_raw)
    angle = -math.degrees(math.atan2(p_next[1] - p_previous[1], p_next[0] - p_previous[0]))

    frame = create_gradient_background(
        WIDTH * SCALE,
        HEIGHT * SCALE,
        (4, 12, 39),
        (11, 30, 64),
    )
    draw_stars(frame, frame_index)
    draw_orbit_path(frame)
    draw_planet(frame)

    settle = max(0.0, (raw - 0.72) / 0.28)
    draw_docking_target(frame, settle)

    # A short after-image trail reinforces direction without obscuring the subject.
    draw = ImageDraw.Draw(frame)
    for lag, alpha in ((0.035, 120), (0.065, 72), (0.095, 38)):
        trail_pos = arc_position(max(0.0, raw - lag))
        radius = sc(1.6)
        tx, ty = point_sc(trail_pos)
        blend = alpha / 255
        color = (round(30 * blend), round(210 * blend), round(230 * blend))
        draw.ellipse([tx - radius, ty - radius, tx + radius, ty + radius], fill=color)

    speed_fraction = max(0.0, 1.0 - interpolate(0.0, 1.0, raw, easing="ease_out"))
    sprite = make_satellite_sprite(angle, speed_fraction)
    px, py = point_sc(position)
    frame.paste(sprite, (px - sprite.width // 2, py - sprite.height // 2), sprite)

    return frame.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    centers = []
    for frame_index in range(FRAME_COUNT):
        raw = frame_index / (FRAME_COUNT - 1)
        centers.append(arc_position(raw))
        builder.add_frame(render_frame(frame_index))

    save_info = builder.save(
        OUTPUT,
        num_colors=96,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(OUTPUT, is_emoji=True, verbose=True)
    late_displacements = [
        round(math.dist(centers[i - 1], centers[i]), 4)
        for i in range(FRAME_COUNT - 6, FRAME_COUNT)
    ]
    assert all(
        earlier > later
        for earlier, later in zip(late_displacements, late_displacements[1:])
    ), late_displacements
    assert passes
    assert validation["width"] == validation["height"] == 128
    assert validation["duration_seconds"] <= 2.7
    print(
        json.dumps(
            {
                "save_info": save_info,
                "validation": validation,
                "late_displacements_px": late_displacements,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
