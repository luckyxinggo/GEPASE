#!/usr/bin/env python3
"""Create the requested Slack badge animation from the supplied PPM asset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def make_subject(source_path: Path, size: int) -> Image.Image:
    """Load the fixture directly and turn only its dark border color transparent."""
    source = Image.open(source_path).convert("RGB")
    dark = source.getpixel((0, 0))
    rgba = source.convert("RGBA")
    pixels = []
    for red, green, blue, _alpha in rgba.getdata():
        distance = abs(red - dark[0]) + abs(green - dark[1]) + abs(blue - dark[2])
        pixels.append((red, green, blue, 0 if distance <= 6 else 255))
    rgba.putdata(pixels)
    return rgba.resize((size, size), Image.Resampling.NEAREST)


def main() -> None:
    args = parse_args()
    sys.path.insert(0, str(args.skill_root))

    from core.easing import interpolate
    from core.frame_composer import create_gradient_background
    from core.gif_builder import GIFBuilder
    from core.validators import validate_gif

    width = height = 128
    fps = 12
    total_frames = 22
    start_hold = 2
    motion_frames = 17
    final_y = 61
    start_y = 96

    subject = make_subject(args.input, 76)
    subject_alpha = subject.getchannel("A")
    glow_mask = subject_alpha.filter(ImageFilter.GaussianBlur(radius=8))
    glow = Image.new("RGBA", subject.size, (42, 203, 190, 0))
    glow.putalpha(glow_mask.point(lambda value: int(value * 0.34)))

    # One continuous back-out path creates the lift, slight overshoot, and settlement.
    centers_y: list[int] = []
    progress_values: list[float] = []
    for frame_index in range(total_frames):
        if frame_index < start_hold:
            progress = 0.0
        elif frame_index < start_hold + motion_frames:
            progress = (frame_index - start_hold) / (motion_frames - 1)
        else:
            progress = 1.0
        progress_values.append(progress)
        centers_y.append(round(interpolate(start_y, final_y, progress, easing="back_out")))

    # Verify the final rendered docking point and pixel-rounded settlement tail.
    if centers_y[-1] != final_y:
        raise RuntimeError("Final frame did not land on the intended docking point")
    settle_start = centers_y.index(min(centers_y))
    tail_deltas = [
        abs(centers_y[index] - centers_y[index - 1])
        for index in range(settle_start + 1, len(centers_y))
    ]
    for previous, current in zip(tail_deltas, tail_deltas[1:]):
        if current > previous + 1:
            raise RuntimeError("Pixel-rounded final approach accelerates unexpectedly")

    builder = GIFBuilder(width=width, height=height, fps=fps)
    for frame_index, (progress, center_y) in enumerate(zip(progress_values, centers_y)):
        frame = create_gradient_background(
            width, height, top_color=(8, 22, 38), bottom_color=(18, 50, 63)
        ).convert("RGBA")
        draw = ImageDraw.Draw(frame, "RGBA")

        # A launch pad and sparse side glints add depth without covering the badge.
        lift = max(0.0, min(1.0, progress))
        draw.ellipse((30, 108, 98, 121), fill=(5, 18, 29, 95), outline=(42, 203, 190, 62), width=2)
        ring_radius = 30 + round(7 * lift)
        ring_alpha = round(58 + 46 * lift)
        draw.ellipse(
            (64 - ring_radius, center_y - ring_radius, 64 + ring_radius, center_y + ring_radius),
            outline=(42, 203, 190, ring_alpha),
            width=2,
        )
        for sparkle_x, sparkle_y, delay in ((22, 80, 0.10), (106, 71, 0.27), (20, 44, 0.44), (108, 39, 0.58)):
            strength = max(0.0, 1.0 - abs(lift - delay) * 3.2)
            if strength > 0:
                radius = 1 + round(2 * strength)
                alpha = round(185 * strength)
                draw.line((sparkle_x - radius, sparkle_y, sparkle_x + radius, sparkle_y), fill=(255, 143, 82, alpha), width=2)
                draw.line((sparkle_x, sparkle_y - radius, sparkle_x, sparkle_y + radius), fill=(42, 203, 190, alpha), width=2)

        # A tiny perimeter glint keeps held frames discrete while remaining unobtrusive.
        glint_x = 5 + (frame_index % 9)
        draw.point((glint_x, 5), fill=(42, 203, 190, 150))

        subject_x = 64 - subject.width // 2
        subject_y = center_y - subject.height // 2
        frame.alpha_composite(glow, (subject_x, subject_y))
        frame.alpha_composite(subject, (subject_x, subject_y))
        builder.add_frame(frame.convert("RGB"))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    info = builder.save(
        args.output,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )

    # Preserve the package's generated/quantized frames while writing an explicit
    # GIF delay. The imageio backend used by GIFBuilder can otherwise encode its
    # millisecond value with backend-dependent timing semantics.
    optimized_frames = builder.optimize_colors(num_colors=48, use_global_palette=True)
    pil_frames = [Image.fromarray(frame) for frame in optimized_frames]
    pil_frames[0].save(
        args.output,
        save_all=True,
        append_images=pil_frames[1:],
        duration=round(1000 / fps),
        loop=0,
        optimize=False,
        disposal=2,
    )
    info["size_kb"] = args.output.stat().st_size / 1024
    info["size_mb"] = info["size_kb"] / 1024
    passes, validation = validate_gif(args.output, is_emoji=True, verbose=True)
    if not passes:
        raise RuntimeError("Generated GIF did not pass Slack emoji validation")
    if validation["duration_seconds"] is None or validation["duration_seconds"] > 2.5:
        raise RuntimeError("Generated GIF exceeds the configured duration limit")

    report = {
        "source_asset": "benchmarks/canaries/slack-gif-creator/fixtures/input-badge.ppm",
        "output": "uploaded_badge_lift.gif",
        "direct_source_use": True,
        "background_color_removed": [12, 31, 45],
        "motion_easing": "back_out",
        "start_center_y": centers_y[0],
        "overshoot_center_y": min(centers_y),
        "final_center_y": centers_y[-1],
        "pixel_positions_y": centers_y,
        "settlement_tail_displacements_px": tail_deltas,
        "builder": {key: value for key, value in info.items() if key != "path"},
        "validation": {key: value for key, value in validation.items() if key != "file"},
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
