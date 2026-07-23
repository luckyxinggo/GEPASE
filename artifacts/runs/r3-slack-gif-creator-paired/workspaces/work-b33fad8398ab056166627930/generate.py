from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageSequence


WORKSPACE = Path(__file__).resolve().parent
PACKAGE = Path(__file__).resolve().parents[5] / "benchmarks/canaries/slack-gif-creator/package"
sys.path.insert(0, str(PACKAGE))

from core.frame_composer import create_gradient_background, draw_circle  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


SIZE = 128
FRAME_COUNT = 12
FPS = 10
OUTPUT = WORKSPACE / "sparkle_ring_loop.gif"


def smooth_peak(phase: float) -> float:
    """A seamless periodic sparkle envelope in [0, 1]."""
    return ((1.0 - math.cos(2.0 * math.pi * phase)) * 0.5) ** 1.8


def draw_sparkle(layer: Image.Image, x: int, y: int, strength: float, tint: tuple[int, int, int]) -> None:
    glow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    glow_r = 4 + int(9 * strength)
    alpha = int(115 * strength)
    gd.ellipse((x - glow_r, y - glow_r, x + glow_r, y + glow_r), fill=(*tint, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=3.2))
    layer.alpha_composite(glow)

    d = ImageDraw.Draw(layer)
    arm = 2.5 + 7.5 * strength
    waist = 1.0 + 1.8 * strength
    fill = (*tint, int(100 + 155 * strength))
    points = [(x, y - arm), (x + waist, y - waist), (x + arm, y),
              (x + waist, y + waist), (x, y + arm), (x - waist, y + waist),
              (x - arm, y), (x - waist, y - waist)]
    d.polygon(points, fill=fill)
    core = max(1.0, 1.5 + 2.0 * strength)
    d.ellipse((x - core, y - core, x + core, y + core), fill=(255, 255, 245, int(170 + 85 * strength)))


def make_frame(index: int) -> Image.Image:
    t = index / FRAME_COUNT
    base = create_gradient_background(SIZE, SIZE, (18, 13, 46), (24, 46, 78)).convert("RGBA")

    # Stable central ring: layered outlines create depth while its geometry never changes.
    ring_glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw_circle(ring_glow, (64, 64), 29, None, (99, 220, 255, 95), 9)
    ring_glow = ring_glow.filter(ImageFilter.GaussianBlur(5))
    base.alpha_composite(ring_glow)
    draw_circle(base, (64, 64), 28, (21, 25, 58, 255), (94, 224, 246, 255), 5)
    draw_circle(base, (64, 64), 21, None, (164, 249, 255, 190), 2)
    draw_circle(base, (57, 55), 3, (218, 255, 255, 145), None, 1)

    sparkles = [
        ((64, 20), 0.00, (255, 221, 92)),
        ((103, 80), 1.0 / 3.0, (255, 128, 215)),
        ((29, 91), 2.0 / 3.0, (133, 255, 188)),
    ]
    for (x, y), offset, tint in sparkles:
        strength = smooth_peak((t - offset) % 1.0)
        draw_sparkle(base, x, y, strength, tint)

    return base.convert("RGB")


def seam_metrics(frames: list[Image.Image]) -> dict[str, float]:
    arrays = [np.asarray(frame, dtype=np.float32) for frame in frames]
    consecutive = [float(np.mean(np.abs(arrays[(i + 1) % len(arrays)] - arrays[i]))) for i in range(len(arrays))]
    seam = consecutive[-1]
    return {
        "seam_mean_abs_diff": round(seam, 4),
        "neighbor_mean_abs_diff": round(float(np.mean(consecutive[:-1])), 4),
        "neighbor_max_abs_diff": round(float(max(consecutive[:-1])), 4),
        "seam_to_neighbor_mean_ratio": round(seam / max(float(np.mean(consecutive[:-1])), 1e-9), 4),
    }


frames = [make_frame(i) for i in range(FRAME_COUNT)]
builder = GIFBuilder(width=SIZE, height=SIZE, fps=FPS)
builder.add_frames(frames)
save_info = builder.save(OUTPUT, num_colors=48, optimize_for_emoji=True, remove_duplicates=False)
passes, validator_info = validate_gif(OUTPUT, is_emoji=True, verbose=True)

with Image.open(OUTPUT) as reopened:
    decoded = [frame.convert("RGB") for frame in ImageSequence.Iterator(reopened)]
    durations = [frame.info.get("duration", reopened.info.get("duration", 0)) for frame in ImageSequence.Iterator(reopened)]
    reopened_info = {
        "dimensions": list(reopened.size),
        "frame_count": reopened.n_frames,
        "durations_ms": durations,
        "total_duration_ms": sum(durations),
        "loop": reopened.info.get("loop"),
        **seam_metrics(decoded),
    }

assert passes
assert reopened_info["dimensions"] == [128, 128]
assert reopened_info["frame_count"] == FRAME_COUNT
assert reopened_info["total_duration_ms"] <= 2200
assert reopened_info["loop"] == 0
assert reopened_info["seam_to_neighbor_mean_ratio"] < 1.6

print({"save": save_info, "validator": validator_info, "reopened": reopened_info})
