from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


WORKSPACE = Path(__file__).resolve().parent
PACKAGE = (
    Path(__file__).resolve().parents[5]
    / "benchmarks/canaries/slack-gif-creator/package"
)
sys.path.insert(0, str(PACKAGE))

from core.easing import interpolate  # noqa: E402
from core.frame_composer import create_gradient_background  # noqa: E402
from core.gif_builder import GIFBuilder  # noqa: E402
from core.validators import validate_gif  # noqa: E402


WIDTH = HEIGHT = 128
FPS = 12
FRAME_COUNT = 24
OUTPUT = WORKSPACE / "emoji_star_bounce.gif"


def motion(frame_index: int) -> tuple[float, float, float]:
    """Return center-y and squash/stretch scales for one fall and one rebound."""
    if frame_index <= 9:
        t = frame_index / 9
        return interpolate(-24, 91, t, "ease_in"), 0.95, 1.06
    if frame_index == 10:
        return 92, 1.17, 0.78
    if frame_index <= 15:
        t = (frame_index - 10) / 5
        return interpolate(92, 64, t, "ease_out"), 0.94, 1.07
    if frame_index <= 19:
        t = (frame_index - 15) / 4
        return interpolate(64, 92, t, "ease_in"), 0.97, 1.04
    if frame_index == 20:
        return 92, 1.12, 0.84
    settle = [90.5, 89.3, 90.0]
    return settle[frame_index - 21], 1.0, 1.0


def star_points(cx: float, cy: float, rx: float, ry: float) -> list[tuple[float, float]]:
    points = []
    for index in range(10):
        angle = math.radians(index * 36 - 90)
        radius = 1.0 if index % 2 == 0 else 0.43
        points.append((cx + rx * radius * math.cos(angle), cy + ry * radius * math.sin(angle)))
    return points


def make_frame(index: int) -> Image.Image:
    background = create_gradient_background(WIDTH, HEIGHT, (5, 15, 42), (45, 25, 112))
    frame = background.convert("RGBA")
    cy, sx, sy = motion(index)

    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.polygon(star_points(64, cy, 35 * sx, 35 * sy), fill=(255, 205, 45, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    frame = Image.alpha_composite(frame, glow)

    star = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(star)
    points = star_points(64, cy, 30 * sx, 30 * sy)
    draw.polygon(points, fill=(255, 207, 45, 255), outline=(47, 25, 71, 255), width=5)
    draw.line(points + [points[0]], fill=(47, 25, 71, 255), width=5, joint="curve")

    highlight_x = 55 - 2 * (sx - 1)
    highlight_y = cy - 13 * sy
    draw.ellipse(
        (highlight_x - 4, highlight_y - 3, highlight_x + 4, highlight_y + 3),
        fill=(255, 248, 190, 235),
    )
    draw.ellipse(
        (highlight_x - 2, highlight_y - 2, highlight_x + 1, highlight_y + 1),
        fill=(255, 255, 255, 255),
    )
    frame = Image.alpha_composite(frame, star)
    return frame.convert("RGB")


builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
builder.add_frames([make_frame(i) for i in range(FRAME_COUNT)])
build_info = builder.save(OUTPUT, num_colors=64, optimize_for_emoji=False, remove_duplicates=False)
passes, validation = validate_gif(OUTPUT, is_emoji=True, verbose=True)
if not passes:
    raise SystemExit(f"Slack validation failed: {validation}")
print({"build": build_info, "validation": validation})
