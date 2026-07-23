#!/usr/bin/env python3
"""Build and inspect the meeting reminder GIF for this isolated work item."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageSequence

from core.easing import interpolate
from core.frame_composer import create_blank_frame, draw_circle, draw_star
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


CANVAS = 480
SCALE = 3
FRAMES = 20
FPS = 10
IVORY = (247, 241, 232)  # #f7f1e8
DARK = (37, 34, 31)  # #25221f
BRICK = (181, 76, 47)  # #b54c2f


def scaled_box(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple(value * SCALE for value in box)


def centered_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    center: tuple[int, int],
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> tuple[int, int, int, int]:
    """Draw one centered label and return its high-resolution pixel bounds."""
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    x = center[0] * SCALE - width // 2 - bounds[0]
    y = center[1] * SCALE - height // 2 - bounds[1]
    draw.text((x, y), text, fill=fill, font=font)
    return (x + bounds[0], y + bounds[1], x + bounds[2], y + bounds[3])


def load_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a bold sans face when available, otherwise use Pillow's embedded face."""
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def build_frame(frame_index: int) -> Image.Image:
    frame = create_blank_frame(CANVAS * SCALE, CANVAS * SCALE, IVORY)
    draw = ImageDraw.Draw(frame)

    draw.rounded_rectangle(
        scaled_box((26, 26, 454, 454)),
        radius=42 * SCALE,
        fill=IVORY,
        outline=DARK,
        width=4 * SCALE,
    )

    phase = 0.5 - 0.5 * math.cos(2 * math.pi * frame_index / FRAMES)
    emphasis = interpolate(0.0, 1.0, phase, easing="ease_in_out")

    # Clock badge and balanced rule communicate a meeting without relying on emoji fonts.
    draw.line(
        [(84 * SCALE, 81 * SCALE), (187 * SCALE, 81 * SCALE)],
        fill=DARK,
        width=3 * SCALE,
    )
    draw.line(
        [(293 * SCALE, 81 * SCALE), (396 * SCALE, 81 * SCALE)],
        fill=DARK,
        width=3 * SCALE,
    )
    dot_radius = 4 + round(2 * emphasis)
    draw_circle(
        frame,
        (191 * SCALE, 81 * SCALE),
        dot_radius * SCALE,
        fill_color=BRICK,
    )
    draw_circle(
        frame,
        (289 * SCALE, 81 * SCALE),
        dot_radius * SCALE,
        fill_color=BRICK,
    )
    draw_circle(
        frame,
        (240 * SCALE, 81 * SCALE),
        27 * SCALE,
        fill_color=BRICK,
        outline_color=DARK,
        outline_width=3 * SCALE,
    )
    draw.line(
        [(240 * SCALE, 81 * SCALE), (240 * SCALE, 66 * SCALE)],
        fill=IVORY,
        width=5 * SCALE,
    )
    draw.line(
        [(240 * SCALE, 81 * SCALE), (250 * SCALE, 88 * SCALE)],
        fill=IVORY,
        width=5 * SCALE,
    )
    draw_circle(frame, (240 * SCALE, 81 * SCALE), 3 * SCALE, fill_color=IVORY)

    title_font = load_bold_font(82 * SCALE)
    time_font = load_bold_font(84 * SCALE)
    footer_font = load_bold_font(19 * SCALE)

    centered_text(draw, "SYNC", (240, 164), title_font, DARK)

    underline_half = 34 + round(14 * emphasis)
    draw.rounded_rectangle(
        scaled_box((240 - underline_half, 217, 240 + underline_half, 225)),
        radius=4 * SCALE,
        fill=BRICK,
    )

    # The high-contrast time card is fixed in every frame to maximize dwell time.
    draw.rounded_rectangle(
        scaled_box((70, 252, 410, 354)),
        radius=30 * SCALE,
        fill=BRICK,
        outline=DARK,
        width=(3 + round(2 * emphasis)) * SCALE,
    )
    centered_text(draw, "10:30", (240, 302), time_font, IVORY)

    centered_text(draw, "MEETING REMINDER", (240, 397), footer_font, DARK)
    draw_star(
        frame,
        (78 * SCALE, 397 * SCALE),
        (7 + round(2 * emphasis)) * SCALE,
        fill_color=BRICK,
    )
    draw_star(
        frame,
        (402 * SCALE, 397 * SCALE),
        (7 + round(2 * emphasis)) * SCALE,
        fill_color=BRICK,
    )

    return frame.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)


def linear_luminance(rgb: tuple[int, int, int]) -> float:
    values = []
    for channel in rgb:
        normalized = channel / 255.0
        values.append(
            normalized / 12.92
            if normalized <= 0.04045
            else ((normalized + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    light, dark = sorted((linear_luminance(first), linear_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def inspect_gif(path: Path) -> dict:
    with Image.open(path) as gif:
        frames = [frame.convert("RGB") for frame in ImageSequence.Iterator(gif)]
        durations_ms = []
        gif.seek(0)
        for index in range(gif.n_frames):
            gif.seek(index)
            durations_ms.append(int(gif.info.get("duration", 0)))

    arrays = [np.asarray(frame) for frame in frames]
    title_region = (slice(116, 205), slice(115, 365))
    time_text_region = (slice(267, 339), slice(100, 380))
    title_deltas = [
        float(
            np.abs(
                frame[title_region].astype(np.int16)
                - arrays[0][title_region].astype(np.int16)
            ).mean()
        )
        for frame in arrays[1:]
    ]
    time_text_deltas = [
        float(
            np.abs(
                frame[time_text_region].astype(np.int16)
                - arrays[0][time_text_region].astype(np.int16)
            ).mean()
        )
        for frame in arrays[1:]
    ]
    title_delta_max = max(title_deltas, default=0.0)
    time_text_delta_max = max(time_text_deltas, default=0.0)
    title_stable = title_delta_max <= 0.5
    time_text_stable = time_text_delta_max <= 1.0
    safe_margin = 26
    motion_pixel_counts = [
        int(np.count_nonzero(np.any(frame != arrays[0], axis=2))) for frame in arrays
    ]

    return {
        "dimensions": list(frames[0].size),
        "frame_count": len(frames),
        "durations_ms": durations_ms,
        "total_duration_ms": sum(durations_ms),
        "loop_seam_mean_abs_rgb_delta": round(
            float(np.abs(arrays[-1].astype(np.int16) - arrays[0].astype(np.int16)).mean()),
            4,
        ),
        "animated_pixel_count_max": max(motion_pixel_counts),
        "primary_text_regions_stable": {
            "SYNC": title_stable,
            "10:30": time_text_stable,
        },
        "primary_text_region_mean_abs_rgb_delta_max": {
            "SYNC": round(title_delta_max, 4),
            "10:30": round(time_text_delta_max, 4),
        },
        "declared_content_bounds_px": {
            "SYNC": [115, 116, 365, 205],
            "10:30": [100, 267, 380, 339],
        },
        "safe_margin_px": safe_margin,
        "contrast_ratios": {
            "dark_on_ivory": round(contrast_ratio(DARK, IVORY), 2),
            "ivory_on_brick": round(contrast_ratio(IVORY, BRICK), 2),
        },
        "file_size_bytes": path.stat().st_size,
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_and_validate.py OUTPUT_GIF VALIDATION_JSON")

    output_path = Path(sys.argv[1])
    validation_path = Path(sys.argv[2])
    output_path.parent.mkdir(parents=True, exist_ok=True)

    builder = GIFBuilder(width=CANVAS, height=CANVAS, fps=FPS)
    builder.add_frames([build_frame(index) for index in range(FRAMES)])
    build_info = builder.save(
        output_path,
        num_colors=48,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )
    passes, package_validation = validate_gif(output_path, is_emoji=False, verbose=True)
    frame_analysis = inspect_gif(output_path)

    checks = {
        "package_validator_passes": passes,
        "dimensions_exact": frame_analysis["dimensions"] == [CANVAS, CANVAS],
        "duration_exact": frame_analysis["total_duration_ms"] == 2000,
        "all_frame_holds_positive": all(
            value > 0 for value in frame_analysis["durations_ms"]
        ),
        "primary_text_stable": all(
            frame_analysis["primary_text_regions_stable"].values()
        ),
        "animation_present": frame_analysis["animated_pixel_count_max"] > 0,
        "loop_seam_subtle": frame_analysis["loop_seam_mean_abs_rgb_delta"] < 0.1,
        "strong_dark_ivory_contrast": frame_analysis["contrast_ratios"]["dark_on_ivory"]
        >= 7.0,
        "adequate_ivory_brick_contrast": frame_analysis["contrast_ratios"]["ivory_on_brick"]
        >= 4.5,
    }
    if not all(checks.values()):
        raise RuntimeError(f"validation failed: {checks}")

    report = {
        "output": output_path.as_posix(),
        "build": build_info,
        "package_validation": package_validation,
        "frame_analysis": frame_analysis,
        "checks": checks,
    }
    validation_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
