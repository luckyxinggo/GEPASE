from pathlib import Path

import imageio.v3 as iio
import numpy as np
from PIL import Image, ImageDraw, ImageFilter


WORKSPACE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[5]
INPUT = ROOT / "benchmarks/canaries/slack-gif-creator/fixtures/input-badge.ppm"
OUTPUT = WORKSPACE / "uploaded_badge_lift.gif"

CANVAS = 128
FRAME_MS = 100
Y_POSITIONS = [61, 58, 54, 49, 44, 39, 34, 30, 27, 26, 27, 28, 30, 31, 31, 30, 30, 30, 30, 30]


def make_background(frame_index: int) -> Image.Image:
    y, x = np.mgrid[0:CANVAS, 0:CANVAS]
    radius = np.sqrt((x - 64) ** 2 + (y - 62) ** 2)
    glow = np.clip(1 - radius / 85, 0, 1)
    pulse = 0.92 + 0.08 * np.sin(2 * np.pi * frame_index / len(Y_POSITIONS))
    rgb = np.empty((CANVAS, CANVAS, 3), dtype=np.uint8)
    rgb[..., 0] = 8 + (6 * glow * pulse).astype(np.uint8)
    rgb[..., 1] = 20 + (18 * glow * pulse).astype(np.uint8)
    rgb[..., 2] = 34 + (23 * glow * pulse).astype(np.uint8)
    return Image.fromarray(rgb, "RGB").convert("RGBA")


def main() -> None:
    source = Image.open(INPUT).convert("RGBA")
    source_pixels = np.asarray(source)
    key = source_pixels[0, 0, :3]
    alpha = np.where(np.all(source_pixels[:, :, :3] == key, axis=2), 0, 255).astype(np.uint8)
    source.putalpha(Image.fromarray(alpha, "L"))

    badge = source.resize((72, 72), Image.Resampling.NEAREST)
    glow_mask = badge.getchannel("A").filter(ImageFilter.GaussianBlur(7))
    cyan_glow = Image.new("RGBA", badge.size, (42, 203, 190, 0))
    cyan_glow.putalpha(glow_mask.point(lambda p: int(p * 0.42)))

    frames = []
    for index, badge_y in enumerate(Y_POSITIONS):
        frame = make_background(index)
        draw = ImageDraw.Draw(frame, "RGBA")

        # A soft floor and tiny side sparks stay behind and outside the badge.
        rise = 61 - badge_y
        shadow_width = max(25, 49 - rise // 2)
        draw.ellipse((64 - shadow_width, 111, 64 + shadow_width, 119), fill=(0, 0, 0, 72))
        spark_alpha = int(65 + 45 * np.sin(np.pi * min(index, 10) / 10))
        for sx, sy, r in [(18, 77, 2), (108, 69, 2), (23, 42, 1), (104, 38, 1)]:
            draw.ellipse((sx-r, sy-r, sx+r, sy+r), fill=(42, 203, 190, spark_alpha))

        x = (CANVAS - badge.width) // 2
        frame.alpha_composite(cyan_glow, (x, badge_y))
        frame.alpha_composite(badge, (x, badge_y))
        frames.append(np.asarray(frame.convert("RGB")))

    iio.imwrite(
        OUTPUT,
        np.stack(frames),
        extension=".gif",
        duration=FRAME_MS,
        loop=0,
    )


if __name__ == "__main__":
    main()
