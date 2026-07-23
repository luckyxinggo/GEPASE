from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw


OUT = Path(__file__).with_name("compact_check_burst.gif")
SIZE = 480
SCALE = 2
FRAMES = 18
DURATION_MS = 80  # 12.5 fps


def ease_out_cubic(x: float) -> float:
    return 1.0 - (1.0 - x) ** 3


rng = random.Random(73191)
particles = []
palette = [(72, 219, 160), (255, 211, 92), (111, 195, 255), (255, 116, 151), (183, 137, 255)]
for i in range(28):
    angle = (2 * math.pi * i / 28) + rng.uniform(-0.09, 0.09)
    particles.append(
        {
            "angle": angle,
            "distance": rng.uniform(125, 205),
            "delay": rng.uniform(0.0, 0.14),
            "size": rng.uniform(5, 10),
            "color": palette[i % len(palette)],
            "kind": i % 3,
        }
    )

rendered = []
for frame_index in range(FRAMES):
    t = frame_index / (FRAMES - 1)
    im = Image.new("RGB", (SIZE * SCALE, SIZE * SCALE), (15, 24, 40))
    d = ImageDraw.Draw(im)
    cx = cy = SIZE * SCALE // 2

    # A quiet halo supports silhouette readability without adding noisy gradients.
    halo_progress = min(1.0, t / 0.28)
    halo_radius = int((60 + 40 * halo_progress) * SCALE)
    halo_alpha_proxy = max(0.0, 1.0 - t * 0.55)
    halo_color = tuple(int(a + (b - a) * 0.18 * halo_alpha_proxy) for a, b in zip((15, 24, 40), (72, 219, 160)))
    d.ellipse((cx - halo_radius, cy - halo_radius, cx + halo_radius, cy + halo_radius), fill=halo_color)

    for p in particles:
        local = max(0.0, min(1.0, (t - p["delay"]) / (0.78 - p["delay"])))
        if local <= 0:
            continue
        travel = ease_out_cubic(local)
        radius = (42 + p["distance"] * travel) * SCALE
        x = cx + math.cos(p["angle"]) * radius
        y = cy + math.sin(p["angle"]) * radius
        fade = max(0.0, 1.0 - max(0.0, local - 0.52) / 0.48)
        size = p["size"] * SCALE * (0.65 + 0.35 * (1.0 - local))
        color = tuple(int(15 + (channel - 15) * fade) for channel in p["color"])
        if p["kind"] == 0:
            d.ellipse((x - size, y - size, x + size, y + size), fill=color)
        elif p["kind"] == 1:
            d.rounded_rectangle((x - size * 0.7, y - size * 1.5, x + size * 0.7, y + size * 1.5), radius=size * 0.35, fill=color)
        else:
            d.polygon([(x, y - size * 1.3), (x + size * 0.8, y), (x, y + size * 1.3), (x - size * 0.8, y)], fill=color)

    # The badge pops in rapidly and stays stable while particles dissipate.
    pop_t = min(1.0, t / 0.24)
    pop = 1.0 + 0.12 * math.sin(math.pi * min(1.0, pop_t))
    badge_r = 78 * pop * SCALE
    d.ellipse((cx - badge_r, cy - badge_r, cx + badge_r, cy + badge_r), fill=(52, 201, 133), outline=(101, 236, 178), width=4 * SCALE)

    # A thick, high-contrast check mark with rounded joints.
    check = [
        (cx - 38 * SCALE * pop, cy + 1 * SCALE * pop),
        (cx - 10 * SCALE * pop, cy + 29 * SCALE * pop),
        (cx + 46 * SCALE * pop, cy - 35 * SCALE * pop),
    ]
    d.line(check, fill=(255, 255, 255), width=18 * SCALE, joint="curve")
    for x, y in check:
        r = 9 * SCALE
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255))

    im = im.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    rendered.append(im)

# A shared, compact adaptive palette avoids per-frame palette bloat.
strip = Image.new("RGB", (SIZE, SIZE * len(rendered)))
for i, im in enumerate(rendered):
    strip.paste(im, (0, SIZE * i))
master = strip.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
paletted = [im.quantize(palette=master, dither=Image.Dither.NONE) for im in rendered]
paletted[0].save(
    OUT,
    save_all=True,
    append_images=paletted[1:],
    duration=DURATION_MS,
    loop=0,
    optimize=True,
    disposal=2,
)
print(OUT)
