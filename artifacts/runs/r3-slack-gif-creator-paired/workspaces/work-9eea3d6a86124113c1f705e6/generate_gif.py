from __future__ import annotations

import json
import math
from pathlib import Path

import imageio.v3 as iio
import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "go_alert_pulse.gif"
SIZE = 128
SCALE = 4
FRAME_COUNT = 30
BG = (23, 33, 58)
CORAL = (255, 107, 107)
WHITE = (255, 255, 255)


def make_frame(index: int) -> Image.Image:
    phase = 2.0 * math.pi * index / FRAME_COUNT
    breath = (1.0 - math.cos(phase)) / 2.0
    radius = 40.0 + 7.0 * breath

    image = Image.new("RGB", (SIZE * SCALE, SIZE * SCALE), BG)
    draw = ImageDraw.Draw(image, "RGBA")
    cx = cy = 64 * SCALE

    # Two restrained coral echoes make the pulse visible without reducing text contrast.
    for extra, alpha in ((8 + 5 * breath, 25), (3 + 3 * breath, 54)):
        rr = (radius + extra) * SCALE
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=CORAL + (alpha,))

    rr = radius * SCALE
    draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=CORAL + (255,))

    # A fixed inner highlight provides a crisp alert emphasis throughout the cycle.
    inner = (radius - 4.0) * SCALE
    draw.ellipse(
        (cx - inner, cy - inner, cx + inner, cy + inner),
        outline=WHITE + (68,),
        width=2 * SCALE,
    )

    font = ImageFont.load_default(size=43 * SCALE)
    text = "GO"
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = cx - tw / 2 - box[0]
    y = cy - th / 2 - box[1] - 1 * SCALE
    draw.text(
        (x, y),
        text,
        font=font,
        fill=WHITE + (255,),
        stroke_width=2 * SCALE,
        stroke_fill=BG + (255,),
    )
    return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


frames = [make_frame(i) for i in range(FRAME_COUNT)]
durations = [80 if i % 3 else 90 for i in range(FRAME_COUNT)]
frames[0].save(
    OUTPUT,
    save_all=True,
    append_images=frames[1:],
    duration=durations,
    loop=0,
    disposal=2,
    optimize=False,
)

# Reopen the actual encoded file and verify its delivery properties.
with Image.open(OUTPUT) as gif:
    reopened = []
    encoded_durations = []
    for i in range(gif.n_frames):
        gif.seek(i)
        reopened.append(np.asarray(gif.convert("RGB")))
        encoded_durations.append(int(gif.info.get("duration", 0)))
    stack = np.stack(reopened)
    center_coral = np.all(stack[:, 64, 64] == np.array(CORAL), axis=1)
    coral_mask = np.all(stack == np.array(CORAL), axis=3)
    coral_areas = coral_mask.sum(axis=(1, 2))
    white_mask = np.all(stack == np.array(WHITE), axis=3)
    text_crop_white = white_mask[:, 42:86, 29:100].sum(axis=(1, 2))
    verification = {
        "filename": OUTPUT.name,
        "format": gif.format,
        "size": list(gif.size),
        "frame_count": gif.n_frames,
        "duration_ms": sum(encoded_durations),
        "frame_durations_ms": encoded_durations,
        "loop": gif.info.get("loop"),
        "center_coral_all_frames": bool(center_coral.all()),
        "coral_area_min_px": int(coral_areas.min()),
        "coral_area_max_px": int(coral_areas.max()),
        "pulse_area_ratio": round(float(coral_areas.max() / coral_areas.min()), 3),
        "white_text_pixels_min_in_crop": int(text_crop_white.min()),
        "white_text_pixels_max_in_crop": int(text_crop_white.max()),
        "text_visible_all_frames": bool((text_crop_white > 250).all()),
    }

assert verification["format"] == "GIF"
assert verification["size"] == [128, 128]
assert verification["frame_count"] == FRAME_COUNT
assert verification["duration_ms"] <= 2800
assert verification["loop"] == 0
assert verification["text_visible_all_frames"]
assert verification["pulse_area_ratio"] >= 1.25

(ROOT / "verification.json").write_text(
    json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(verification, ensure_ascii=False, indent=2))
