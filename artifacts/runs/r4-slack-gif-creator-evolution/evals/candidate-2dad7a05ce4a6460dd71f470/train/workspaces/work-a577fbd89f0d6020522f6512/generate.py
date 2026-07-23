#!/usr/bin/env python3
"""Generate the requested three-stage Slack deployment status card."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.easing import interpolate
from core.frame_composer import create_gradient_background, draw_star
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


WIDTH = 480
HEIGHT = 480
FPS = 10
FRAMES_PER_PHASE = 8
OUTPUT = Path("deployment_status.gif")

PHASES = (
    {
        "name": "WAITING",
        "headline": "DEPLOY",
        "subtitle": "Waiting for a runner",
        "accent": (246, 190, 76),
        "top": (34, 29, 60),
        "bottom": (12, 20, 38),
    },
    {
        "name": "PROCESSING",
        "headline": "DEPLOY",
        "subtitle": "Building & testing",
        "accent": (102, 126, 255),
        "top": (25, 33, 75),
        "bottom": (10, 22, 44),
    },
    {
        "name": "COMPLETE",
        "headline": "DONE",
        "subtitle": "Live in production",
        "accent": (73, 214, 148),
        "top": (17, 59, 57),
        "bottom": (8, 27, 38),
    },
)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


FONT_LABEL = load_font(13, bold=True)
FONT_META = load_font(12, bold=True)
FONT_HEADLINE = load_font(57, bold=True)
FONT_SUBTITLE = load_font(17)
FONT_STEP = load_font(11, bold=True)

BLOCK_GLYPHS = {
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
}


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def draw_block_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    center_x: int,
    top_y: int,
    fill: tuple[int, int, int],
    scale: int = 7,
) -> None:
    glyph_width = 5 * scale
    gap = scale
    total_width = len(text) * glyph_width + (len(text) - 1) * gap
    start_x = round(center_x - total_width / 2)
    for glyph_index, char in enumerate(text):
        pattern = BLOCK_GLYPHS[char]
        origin_x = start_x + glyph_index * (glyph_width + gap)
        for row, line in enumerate(pattern):
            for col, bit in enumerate(line):
                if bit == "1":
                    x = origin_x + col * scale
                    y = top_y + row * scale
                    draw.rounded_rectangle(
                        (x, y, x + scale - 2, y + scale - 2),
                        radius=2,
                        fill=fill,
                    )


def glow(frame: Image.Image, center: tuple[int, int], accent: tuple[int, int, int], radius: int) -> None:
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    for r in range(radius, 12, -7):
        alpha = max(2, round(22 * (1 - r / radius) + 3))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*accent, alpha))
    frame.paste(Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB"))


def draw_waiting(draw: ImageDraw.ImageDraw, local_t: float, accent: tuple[int, int, int]) -> None:
    cx, cy = 240, 207
    pulse = 1.0 + 0.045 * math.sin(local_t * math.tau)
    r = round(56 * pulse)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(29, 35, 57), outline=accent, width=5)
    draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=accent)
    angle = -math.pi / 2 + local_t * math.pi * 0.7
    hand = (cx + math.cos(angle) * 32, cy + math.sin(angle) * 32)
    draw.line((cx, cy, hand[0], hand[1]), fill=(250, 244, 219), width=7)
    draw.line((cx, cy, cx - 21, cy + 13), fill=(250, 244, 219), width=6)
    for i in range(3):
        a = local_t * math.tau + i * math.tau / 3
        x = cx + math.cos(a) * 76
        y = cy + math.sin(a) * 76
        rr = 5 + (i == 0)
        draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=mix(accent, (255, 255, 255), i * 0.2))


def draw_processing(draw: ImageDraw.ImageDraw, local_t: float, accent: tuple[int, int, int]) -> None:
    cx, cy = 240, 207
    for ring, width, alpha_mix in ((68, 7, 0.0), (53, 4, 0.35)):
        start = round(local_t * 360 * (1 if ring == 68 else -1))
        color = mix(accent, (98, 218, 255), alpha_mix)
        for offset in (0, 120, 240):
            draw.arc((cx - ring, cy - ring, cx + ring, cy + ring), start=start + offset, end=start + offset + 66, fill=color, width=width)
    scale = interpolate(0.92, 1.08, 0.5 + 0.5 * math.sin(local_t * math.tau), easing="ease_in_out")
    box = round(46 * scale)
    draw.rounded_rectangle((cx - box, cy - box, cx + box, cy + box), radius=15, fill=(31, 42, 70), outline=(143, 159, 255), width=3)
    for y, width in ((190, 48), (206, 36), (222, 24)):
        draw.rounded_rectangle((cx - width // 2, y - 4, cx + width // 2, y + 4), radius=4, fill=mix(accent, (255, 255, 255), (y - 190) / 80))


def draw_complete(frame: Image.Image, draw: ImageDraw.ImageDraw, local_t: float, accent: tuple[int, int, int]) -> None:
    cx, cy = 240, 207
    appear = interpolate(0.78, 1.0, min(1.0, local_t * 2.2), easing="back_out")
    r = round(64 * appear)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(23, 72, 62), outline=accent, width=6)
    check_t = min(1.0, local_t * 2.8)
    p1 = (207, 207)
    p2 = (231, 231)
    p3 = (276, 181)
    if check_t < 0.42:
        t = check_t / 0.42
        end = (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)
        draw.line((p1, end), fill=(239, 255, 248), width=12)
    else:
        draw.line((p1, p2), fill=(239, 255, 248), width=12)
        t = (check_t - 0.42) / 0.58
        end = (p2[0] + (p3[0] - p2[0]) * t, p2[1] + (p3[1] - p2[1]) * t)
        draw.line((p2, end), fill=(239, 255, 248), width=12)
    sparkle = max(0.0, math.sin(local_t * math.pi))
    for i, angle in enumerate((-2.6, -1.7, -0.7, 0.3, 1.15, 2.35)):
        distance = 79 + 11 * sparkle
        x = round(cx + math.cos(angle) * distance)
        y = round(cy + math.sin(angle) * distance)
        size = round((7 if i % 2 == 0 else 5) * sparkle)
        if size >= 2:
            draw_star(frame, (x, y), size, mix(accent, (255, 255, 255), 0.45), None, 2)


def draw_progress(draw: ImageDraw.ImageDraw, phase_index: int, local_t: float, accent: tuple[int, int, int]) -> None:
    centers = (138, 240, 342)
    y = 409
    inactive = (66, 77, 101)
    draw.line((centers[0], y, centers[-1], y), fill=inactive, width=5)
    if phase_index > 0:
        completed_x = centers[phase_index - 1]
        target_x = centers[phase_index]
        grow = interpolate(completed_x, target_x, local_t, easing="ease_out")
        draw.line((centers[0], y, grow, y), fill=accent, width=5)
    for i, x in enumerate(centers):
        active = i <= phase_index
        fill = accent if active else (29, 37, 58)
        outline = accent if active else inactive
        radius = 10 if i == phase_index else 8
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=outline, width=3)
    labels = ("WAIT", "BUILD", "LIVE")
    for label, x in zip(labels, centers):
        draw.text((x, 434), label, font=FONT_STEP, fill=(158, 168, 191), anchor="mm")


def make_frame(phase_index: int, local_index: int) -> Image.Image:
    phase = PHASES[phase_index]
    local_t = local_index / FRAMES_PER_PHASE
    frame = create_gradient_background(WIDTH, HEIGHT, phase["top"], phase["bottom"])
    glow(frame, (240, 214), phase["accent"], 160)
    draw = ImageDraw.Draw(frame)

    draw.rounded_rectangle((35, 47, 445, 447), radius=34, fill=(9, 15, 29), outline=(65, 76, 104), width=2)
    draw.rounded_rectangle((36, 48, 444, 151), radius=33, fill=(17, 24, 43))
    draw.rectangle((36, 116, 444, 151), fill=(17, 24, 43))

    draw.ellipse((61, 73, 77, 89), fill=phase["accent"])
    draw.text((88, 81), "PRODUCTION", font=FONT_LABEL, fill=(220, 226, 240), anchor="lm")
    draw.rounded_rectangle((326, 67, 418, 95), radius=14, fill=(28, 36, 58), outline=(69, 80, 107), width=1)
    draw.text((372, 81), "DEPLOY #024", font=FONT_META, fill=(177, 189, 216), anchor="mm")
    draw.line((61, 116, 419, 116), fill=(45, 55, 80), width=2)
    draw.text((61, 134), phase["name"], font=FONT_META, fill=phase["accent"], anchor="lm")

    if phase_index == 0:
        draw_waiting(draw, local_t, phase["accent"])
    elif phase_index == 1:
        draw_processing(draw, local_t, phase["accent"])
    else:
        draw_complete(frame, draw, local_t, phase["accent"])

    draw_block_text(draw, phase["headline"], center_x=240, top_y=287, fill=(245, 248, 255))
    draw.text((240, 358), phase["subtitle"], font=FONT_SUBTITLE, fill=(171, 182, 207), anchor="mm")
    draw_progress(draw, phase_index, local_t, phase["accent"])
    return frame


def inspect_export(path: Path) -> dict:
    from PIL import Image

    with Image.open(path) as image:
        durations = []
        frames = []
        for index in range(image.n_frames):
            image.seek(index)
            durations.append(int(image.info.get("duration", 0)))
            if index in (3, 11, 19):
                frames.append({"index": index, "mode": image.mode, "size": list(image.size)})
        return {
            "dimensions": list(image.size),
            "frames": image.n_frames,
            "durations_ms": durations,
            "total_duration_ms": sum(durations),
            "loop": image.info.get("loop"),
            "sampled_frames": frames,
        }


def main() -> None:
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    for phase_index in range(len(PHASES)):
        for local_index in range(FRAMES_PER_PHASE):
            builder.add_frame(make_frame(phase_index, local_index))

    build_info = builder.save(OUTPUT, num_colors=96, optimize_for_emoji=False, remove_duplicates=False)
    passes, validator_info = validate_gif(OUTPUT, is_emoji=False, verbose=True)
    export_info = inspect_export(OUTPUT)
    checks = {
        "builder": build_info,
        "validator": validator_info,
        "export": export_info,
        "content_schedule": {
            "waiting": {"frames": [0, 7], "headline": "DEPLOY"},
            "processing": {"frames": [8, 15], "headline": "DEPLOY"},
            "complete": {"frames": [16, 23], "headline": "DONE"},
        },
        "passes": bool(
            passes
            and export_info["dimensions"] == [480, 480]
            and export_info["frames"] == 24
            and export_info["total_duration_ms"] == 2400
            and export_info["loop"] == 0
        ),
    }
    Path("validation.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not checks["passes"]:
        raise SystemExit("Export validation failed")


if __name__ == "__main__":
    main()
