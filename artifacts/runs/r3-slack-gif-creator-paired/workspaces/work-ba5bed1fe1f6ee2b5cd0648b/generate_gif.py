from math import cos, pi, sin
from pathlib import Path

from PIL import Image, ImageDraw


OUT = Path(__file__).with_name("satellite_orbit_ease.gif")
SIZE = 128
FRAME_COUNT = 24
DURATION_MS = 100
PLANET = (70, 69)


def orbit_point(progress: float) -> tuple[float, float]:
    # Nonlinear ease-out: quick initially, then progressively smaller steps.
    # The 1.8 exponent preserves a visibly moving final pixel at emoji scale.
    eased = 1.0 - (1.0 - progress) ** 1.8
    angle = (158.0 + (-57.0 - 158.0) * eased) * pi / 180.0
    return PLANET[0] + 48 * cos(angle), PLANET[1] + 39 * sin(angle)


def draw_satellite(draw: ImageDraw.ImageDraw, x: float, y: float, angle: float) -> None:
    cx, cy = int(round(x)), int(round(y))
    # A compact cyan spacecraft with a bright core and two solar fins.
    draw.rounded_rectangle((cx - 5, cy - 4, cx + 5, cy + 4), radius=3,
                           fill=(25, 225, 231), outline=(181, 255, 255), width=1)
    draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=(232, 255, 255))
    dx, dy = cos(angle), sin(angle)
    px, py = -dy, dx
    for side in (-1, 1):
        ox, oy = px * side * 7, py * side * 7
        draw.polygon([
            (int(cx + ox - dx * 3), int(cy + oy - dy * 3)),
            (int(cx + ox + dx * 3), int(cy + oy + dy * 3)),
            (int(cx + ox + px * side * 3 + dx * 3), int(cy + oy + py * side * 3 + dy * 3)),
            (int(cx + ox + px * side * 3 - dx * 3), int(cy + oy + py * side * 3 - dy * 3)),
        ], fill=(12, 143, 178), outline=(89, 238, 247))
    draw.line((cx - int(dx * 5), cy - int(dy * 5), cx - int(dx * 9), cy - int(dy * 9)),
              fill=(181, 255, 255), width=1)


frames = []
stars = [(8, 14), (18, 47), (35, 11), (52, 23), (77, 12), (100, 18),
         (117, 42), (110, 91), (89, 112), (47, 117), (14, 104), (123, 116)]
arc_points = []
for j in range(65):
    a = (158.0 + (-57.0 - 158.0) * (j / 64)) * pi / 180.0
    arc_points.append((int(PLANET[0] + 48 * cos(a)), int(PLANET[1] + 39 * sin(a))))

for i in range(FRAME_COUNT):
    im = Image.new("RGB", (SIZE, SIZE), (5, 9, 30))
    d = ImageDraw.Draw(im)
    for n, (sx, sy) in enumerate(stars):
        c = (104, 129, 183) if (i + n) % 4 else (210, 228, 255)
        r = 1 if n % 3 else 2
        d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=c)

    # The visible orbital guide makes the curved trajectory unmistakable at emoji size.
    for j in range(0, len(arc_points) - 2, 5):
        d.line((arc_points[j], arc_points[j + 2]), fill=(40, 73, 111), width=1)

    # Orange planet with rim, banding, and a small highlight.
    px, py = PLANET
    d.ellipse((px - 25, py - 25, px + 25, py + 25), fill=(242, 112, 35),
              outline=(255, 185, 68), width=2)
    d.arc((px - 22, py - 12, px + 22, py + 12), 8, 172, fill=(181, 61, 30), width=3)
    d.ellipse((px - 13, py - 14, px - 6, py - 7), fill=(255, 178, 73))
    d.ellipse((px + 8, py + 8, px + 14, py + 13), fill=(204, 71, 27))

    t = i / (FRAME_COUNT - 1)
    x, y = orbit_point(t)
    # Tangent heading along decreasing angle.
    eased = 1.0 - (1.0 - t) ** 1.8
    theta = (158.0 + (-57.0 - 158.0) * eased) * pi / 180.0
    tangent = theta - pi / 2
    draw_satellite(d, x, y, tangent)
    frames.append(im)

frames[0].save(
    OUT,
    save_all=True,
    append_images=frames[1:],
    duration=DURATION_MS,
    loop=0,
    disposal=2,
    optimize=False,
)
print(OUT.name)
