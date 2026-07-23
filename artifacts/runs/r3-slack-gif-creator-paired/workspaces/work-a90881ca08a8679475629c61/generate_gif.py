from pathlib import Path
import math

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFilter


OUT = Path(__file__).with_name("sparkle_ring_loop.gif")
SIZE = 128
FRAME_COUNT = 32
DURATION_MS = 64


def smooth_pulse(phase: float) -> float:
    # A fully periodic raised-cosine pulse: absent -> bright -> absent.
    return max(0.0, math.sin(math.pi * phase)) ** 2.2


def sparkle_layer(cx: float, cy: float, strength: float, color: tuple[int, int, int]):
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    radius = 2.0 + 6.5 * strength
    alpha = int(255 * min(1.0, 0.18 + strength))
    # Four-point diamond/star with slender arms.
    points = [
        (cx, cy - radius),
        (cx + radius * 0.24, cy - radius * 0.24),
        (cx + radius, cy),
        (cx + radius * 0.24, cy + radius * 0.24),
        (cx, cy + radius),
        (cx - radius * 0.24, cy + radius * 0.24),
        (cx - radius, cy),
        (cx - radius * 0.24, cy - radius * 0.24),
    ]
    draw.polygon(points, fill=(*color, alpha))
    core_r = 1.0 + 1.5 * strength
    draw.ellipse((cx-core_r, cy-core_r, cx+core_r, cy+core_r), fill=(255, 255, 245, alpha))

    glow = layer.filter(ImageFilter.GaussianBlur(2.2 + 1.8 * strength))
    glow.putalpha(glow.getchannel("A").point(lambda a: int(a * 0.42)))
    return Image.alpha_composite(glow, layer)


def make_frame(index: int) -> Image.Image:
    t = index / FRAME_COUNT
    image = Image.new("RGBA", (SIZE, SIZE), (18, 20, 35, 255))

    # Gentle radial backdrop improves readability at Slack emoji scale.
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    dist = np.sqrt((xx - 64) ** 2 + (yy - 64) ** 2)
    halo = np.clip(1 - dist / 70, 0, 1)
    bg = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    bg[..., 0] = 28 + (16 * halo).astype(np.uint8)
    bg[..., 1] = 27 + (20 * halo).astype(np.uint8)
    bg[..., 2] = 48 + (34 * halo).astype(np.uint8)
    bg[..., 3] = 255
    image = Image.fromarray(bg, "RGBA")

    # Stable central ring, with a restrained glow.
    ring_glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(ring_glow)
    gd.ellipse((38, 38, 90, 90), outline=(118, 92, 255, 120), width=8)
    ring_glow = ring_glow.filter(ImageFilter.GaussianBlur(7))
    image = Image.alpha_composite(image, ring_glow)

    ring = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((39, 39, 89, 89), outline=(176, 157, 255, 255), width=5)
    rd.ellipse((44, 44, 84, 84), outline=(99, 79, 210, 190), width=2)
    image = Image.alpha_composite(image, ring)

    colors = [(255, 212, 78), (107, 231, 255), (255, 122, 211)]
    base_angles = [-math.pi / 2, math.pi / 6, 5 * math.pi / 6]
    for j, (base_angle, color) in enumerate(zip(base_angles, colors)):
        local = (t - j / 3) % 1.0
        strength = smooth_pulse(local)
        # Small periodic orbit makes motion legible while preserving the ring as anchor.
        angle = base_angle + 0.22 * math.sin(2 * math.pi * (t + j / 3))
        orbit = 43 + 2.5 * math.sin(2 * math.pi * (t + j / 3))
        cx = 64 + orbit * math.cos(angle)
        cy = 64 + orbit * math.sin(angle)
        image = Image.alpha_composite(image, sparkle_layer(cx, cy, strength, color))

    return image.convert("RGB")


frames = [make_frame(i) for i in range(FRAME_COUNT)]
# Quantize through imageio's Pillow-backed path, then use Pillow's explicit
# millisecond timing so every frame carries an unambiguous GIF delay.
arrays = [np.asarray(frame) for frame in frames]
paletted = [Image.fromarray(array).convert("P", palette=Image.Palette.ADAPTIVE, colors=256) for array in arrays]
paletted[0].save(
    OUT,
    save_all=True,
    append_images=paletted[1:],
    duration=[DURATION_MS] * FRAME_COUNT,
    loop=0,
    disposal=2,
    optimize=False,
)
