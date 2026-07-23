#!/usr/bin/env python3
"""Create a Slack-ready lift-and-settle GIF from the supplied badge PPM."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


WORKSPACE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[8]
PACKAGE = REPO / "artifacts/runs/r4-slack-gif-creator-evolution/candidate-workspaces/applications/application-1986620ac067029380557187"
FIXTURE = REPO / "benchmarks/canaries/slack-gif-creator/fixtures/input-badge.ppm"
OUTPUT = WORKSPACE / "uploaded_badge_lift.gif"

sys.path.insert(0, str(PACKAGE))

from core.easing import interpolate  # noqa: E402
from core.frame_composer import create_gradient_background  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


def load_badge() -> Image.Image:
    """Load the actual fixture and make only its known background transparent."""
    source = Image.open(FIXTURE).convert("RGB")
    pixels = np.asarray(source)
    background = pixels[0, 0]
    alpha = np.where(np.all(pixels == background, axis=2), 0, 255).astype(np.uint8)
    rgba = np.dstack([pixels, alpha])
    badge = Image.fromarray(rgba, mode="RGBA")
    return badge.resize((72, 72), Image.Resampling.NEAREST)


def base_frame() -> Image.Image:
    frame = create_gradient_background(128, 128, (8, 24, 38), (12, 31, 45)).convert("RGBA")
    decor = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(decor)
    draw.ellipse((14, 14, 114, 114), outline=(42, 203, 190, 32), width=2)
    draw.ellipse((22, 22, 106, 106), outline=(255, 143, 82, 22), width=1)
    draw.ellipse((10, 82, 22, 94), fill=(42, 203, 190, 36))
    draw.ellipse((106, 30, 112, 36), fill=(255, 143, 82, 42))
    return Image.alpha_composite(frame, decor)


def compose_frame(background: Image.Image, badge: Image.Image, y: int, progress: float) -> Image.Image:
    frame = background.copy()
    alpha = badge.getchannel("A")

    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    glow_mask = Image.new("L", frame.size, 0)
    glow_mask.paste(alpha, (28, y))
    glow_mask = glow_mask.filter(ImageFilter.GaussianBlur(radius=7))
    glow_strength = int(62 + 38 * progress)
    glow.putalpha(glow_mask.point(lambda value: value * glow_strength // 255))
    glow_color = Image.new("RGBA", frame.size, (42, 203, 190, 255))
    glow_color.putalpha(glow.getchannel("A"))
    frame = Image.alpha_composite(frame, glow_color)

    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_width = int(42 - 10 * progress)
    shadow_draw.ellipse(
        (64 - shadow_width // 2, 112, 64 + shadow_width // 2, 119),
        fill=(1, 8, 15, int(90 - 30 * progress)),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=3))
    frame = Image.alpha_composite(frame, shadow)

    frame.alpha_composite(badge, (28, y))
    return frame.convert("RGB")


def main() -> None:
    badge = load_badge()
    background = base_frame()
    builder = GIFBuilder(width=128, height=128, fps=10)

    y_positions: list[int] = []
    progress_values: list[float] = []
    for index in range(18):
        if index < 2:
            progress = 0.0
            y_value = 54.0
        elif index <= 13:
            t = (index - 2) / 11
            y_value = interpolate(54.0, 30.0, t, easing="back_out")
            progress = min(1.0, max(0.0, (54.0 - y_value) / 24.0))
        else:
            progress = 1.0
            y_value = 30.0
        y = int(round(y_value))
        y_positions.append(y)
        progress_values.append(progress)
        builder.add_frame(compose_frame(background, badge, y, progress))

    save_info = builder.save(
        OUTPUT,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(OUTPUT, is_emoji=True, verbose=True)
    with Image.open(OUTPUT) as encoded:
        encoded_durations_ms = []
        for frame_index in range(encoded.n_frames):
            encoded.seek(frame_index)
            encoded_durations_ms.append(encoded.info.get("duration", 0))

    artifact_path = OUTPUT.relative_to(REPO).as_posix()
    save_info = {**save_info, "path": artifact_path}
    validation = {**validation, "file": artifact_path}

    # Validate the integerized motion actually rendered: it rises past y=30,
    # returns exactly to y=30, and stays there through the tail.
    overshoot = min(y_positions) < 30
    settled_tail = y_positions[-5:] == [30] * 5
    exact_dock = y_positions[-1] == 30
    if not (passes and overshoot and settled_tail and exact_dock):
        raise RuntimeError("Rendered GIF failed motion or Slack validation")

    metrics = {
        "source_size": list(Image.open(FIXTURE).size),
        "source_used_directly": True,
        "source_resampling": "nearest",
        "y_positions": y_positions,
        "overshoot_y": min(y_positions),
        "final_y": y_positions[-1],
        "settled_tail_frames": 5,
        "save_info": save_info,
        "validation": validation,
        "encoded_frame_count": len(encoded_durations_ms),
        "encoded_frame_durations_ms": encoded_durations_ms,
        "encoded_total_duration_seconds": sum(encoded_durations_ms) / 1000,
    }
    (WORKSPACE / "motion-validation.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
