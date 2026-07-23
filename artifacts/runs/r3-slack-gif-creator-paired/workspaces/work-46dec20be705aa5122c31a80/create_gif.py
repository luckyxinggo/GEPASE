from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "emoji_star_bounce.gif"
SIZE = 128
FRAME_MS = 83


def ease_in(t: float) -> float:
    return t * t


def ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 2


def star_points(cx: float, cy: float, outer: float, inner: float) -> list[tuple[float, float]]:
    points = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        radius = outer if i % 2 == 0 else inner
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return points


def center_y(frame: int) -> float:
    # One contact, one rebound apex, then a final landing and hold.
    if frame <= 9:
        return 10 + (89 - 10) * ease_in(frame / 9)
    if frame <= 14:
        return 89 - (89 - 65) * ease_out((frame - 9) / 5)
    if frame <= 19:
        return 65 + (89 - 65) * ease_in((frame - 14) / 5)
    return 89


def make_background() -> Image.Image:
    image = Image.new("RGB", (SIZE, SIZE))
    pixels = image.load()
    top = (5, 15, 43)
    bottom = (42, 33, 112)
    for y in range(SIZE):
        mix = y / (SIZE - 1)
        row = tuple(round(top[c] * (1 - mix) + bottom[c] * mix) for c in range(3))
        for x in range(SIZE):
            # A subtle centered glow keeps the background rich without distracting.
            glow = max(0.0, 1.0 - math.hypot(x - 64, y - 66) / 92) * 7
            pixels[x, y] = tuple(min(255, round(v + glow)) for v in row)
    return image


def draw_frame(background: Image.Image, frame: int) -> Image.Image:
    scale = 4
    canvas = background.resize((SIZE * scale, SIZE * scale), Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(canvas)
    cx, cy = 64 * scale, center_y(frame) * scale

    # Contact squash happens only at the two landings; the star remains clearly readable.
    contact = 0.0
    if frame in (9, 19):
        contact = 0.12
    elif frame in (8, 10, 18, 20):
        contact = 0.05
    sx, sy = 1 + contact, 1 - contact

    raw = star_points(0, 0, 29 * scale, 13 * scale)
    points = [(cx + x * sx, cy + y * sy) for x, y in raw]
    draw.polygon(points, fill=(255, 211, 42), outline=(19, 18, 40), width=4 * scale)
    # Warm lower fill accent adds volume.
    accent = star_points(cx, cy + 2 * scale, 21 * scale, 9 * scale)
    draw.line(accent + [accent[0]], fill=(246, 174, 24), width=1 * scale, joint="curve")
    # Small high-contrast highlight near the upper-left point.
    draw.ellipse(
        (cx - 10 * scale, cy - 14 * scale, cx - 4 * scale, cy - 8 * scale),
        fill=(255, 248, 187),
    )

    return canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def main() -> None:
    background = make_background()
    frames = [draw_frame(background, frame) for frame in range(24)]
    # A compact adaptive palette is suitable for Slack emoji while preserving the gradient.
    paletted = [frame.quantize(colors=96, method=Image.Quantize.MEDIANCUT) for frame in frames]
    paletted[0].save(
        OUTPUT,
        save_all=True,
        append_images=paletted[1:],
        duration=FRAME_MS,
        loop=0,
        optimize=True,
        disposal=2,
    )


if __name__ == "__main__":
    main()
