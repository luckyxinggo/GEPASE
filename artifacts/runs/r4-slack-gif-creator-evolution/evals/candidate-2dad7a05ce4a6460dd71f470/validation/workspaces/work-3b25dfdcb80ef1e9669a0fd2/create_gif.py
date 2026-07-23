#!/usr/bin/env python3
"""Create the requested Slack alert emoji with the candidate Package."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


REPOSITORY_ROOT = Path.cwd()
PACKAGE_ROOT = REPOSITORY_ROOT / (
    "artifacts/runs/r4-slack-gif-creator-evolution/candidate-workspaces/"
    "applications/application-20ada438d49e648f1bb86749"
)
WORKSPACE = REPOSITORY_ROOT / (
    "artifacts/runs/r4-slack-gif-creator-evolution/evals/"
    "candidate-2dad7a05ce4a6460dd71f470/validation/workspaces/"
    "work-3b25dfdcb80ef1e9669a0fd2"
)
OUTPUT = WORKSPACE / "go_alert_pulse.gif"

sys.path.insert(0, str(PACKAGE_ROOT))

from core.frame_composer import create_blank_frame, draw_circle  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


NAVY = (23, 33, 58)  # #17213a
CORAL = (255, 107, 107)  # #ff6b6b
WHITE = (255, 255, 255)  # #ffffff
SIZE = 128
FPS = 12
FRAME_COUNT = 24


def load_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a portable bold face supplied by common Pillow installations."""
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def binary_text_mask(text: str, font: ImageFont.ImageFont) -> Image.Image:
    """Make crisp, palette-safe white lettering without introducing extra colors."""
    scratch = Image.new("L", (SIZE, SIZE), 0)
    draw = ImageDraw.Draw(scratch)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = (SIZE - width) // 2 - bbox[0]
    y = (SIZE - height) // 2 - bbox[1] - 2
    draw.text((x, y), text, font=font, fill=255, stroke_width=1, stroke_fill=255)
    return scratch.point(lambda value: 255 if value >= 96 else 0, mode="1")


def build_frames() -> list[Image.Image]:
    font = load_bold_font(42)
    text_mask = binary_text_mask("GO", font)
    frames: list[Image.Image] = []

    for index in range(FRAME_COUNT):
        # One complete, smooth breathing cycle. The fixed-size label remains legible.
        phase = 2.0 * math.pi * index / FRAME_COUNT
        breath = 0.5 - 0.5 * math.cos(phase)
        badge_radius = 43 + round(6 * breath)
        halo_radius = badge_radius + 6 + round(2 * breath)

        frame = create_blank_frame(SIZE, SIZE, NAVY)
        draw_circle(
            frame,
            center=(64, 64),
            radius=halo_radius,
            outline_color=CORAL,
            outline_width=3,
        )
        draw_circle(frame, center=(64, 64), radius=badge_radius, fill_color=CORAL)

        # A small navy notch and white highlight give the badge visual depth while
        # retaining the exact three-color palette.
        detail = ImageDraw.Draw(frame)
        detail.arc((31, 31, 97, 97), 210, 310, fill=NAVY, width=2)
        detail.arc((35, 35, 93, 93), 207, 300, fill=WHITE, width=2)

        # Two tiny orbiting signal lights keep every exported time slice distinct,
        # reinforcing the alert motif without competing with the primary pulse.
        orbit = halo_radius + 3
        orbit_angle = phase - math.pi / 2.0
        signal_x = round(64 + orbit * math.cos(orbit_angle))
        signal_y = round(64 + orbit * math.sin(orbit_angle))
        detail.ellipse(
            (signal_x - 2, signal_y - 2, signal_x + 2, signal_y + 2),
            fill=CORAL,
        )
        echo_x = round(64 - orbit * math.cos(orbit_angle))
        echo_y = round(64 - orbit * math.sin(orbit_angle))
        detail.ellipse((echo_x - 1, echo_y - 1, echo_x + 1, echo_y + 1), fill=CORAL)
        frame.paste(WHITE, (0, 0), text_mask)
        frames.append(frame)

    return frames


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    frames = build_frames()
    builder = GIFBuilder(width=SIZE, height=SIZE, fps=FPS)
    builder.add_frames(frames)
    info = builder.save(
        OUTPUT,
        num_colors=4,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )

    passes, validation = validate_gif(OUTPUT, is_emoji=True, verbose=True)
    if not passes:
        raise RuntimeError(f"Slack validation failed: {validation}")

    with Image.open(OUTPUT) as image:
        exported: list[np.ndarray] = []
        palette_colors: set[tuple[int, int, int]] = set()
        for frame_index in range(image.n_frames):
            image.seek(frame_index)
            rgb = np.asarray(image.convert("RGB"))
            exported.append(rgb)
            palette_colors.update(map(tuple, np.unique(rgb.reshape(-1, 3), axis=0)))
        loop_seam_pixels = int(np.count_nonzero(np.any(exported[0] != exported[-1], axis=2)))

    expected_colors = {NAVY, CORAL, WHITE}
    if palette_colors != expected_colors:
        raise RuntimeError(f"Unexpected exported palette: {sorted(palette_colors)}")
    if info["duration_seconds"] > 2.8:
        raise RuntimeError(f"Duration exceeds budget: {info['duration_seconds']}")

    print(f"Exact palette: {sorted(palette_colors)}")
    print(f"End-to-start changed pixels: {loop_seam_pixels}")


if __name__ == "__main__":
    main()
