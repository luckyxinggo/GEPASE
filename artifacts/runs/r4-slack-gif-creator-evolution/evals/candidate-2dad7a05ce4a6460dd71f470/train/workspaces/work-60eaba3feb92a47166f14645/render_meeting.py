import math
import sys
from pathlib import Path

from PIL import ImageDraw, ImageFont

from core.easing import interpolate
from core.frame_composer import create_blank_frame
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


WIDTH = 480
HEIGHT = 480
FPS = 10
FRAME_COUNT = 20

CREAM = (247, 241, 232)
DARK = (37, 34, 31)
BRICK = (181, 76, 47)


def font(size: int):
    system_font = (
        Path(Path.cwd().anchor)
        / "System"
        / "Library"
        / "Fonts"
        / "Supplemental"
        / "Arial Bold.ttf"
    )
    return ImageFont.truetype(str(system_font), size)


def centered_text(draw, xy, text, typeface, fill):
    box = draw.textbbox((0, 0), text, font=typeface)
    width = box[2] - box[0]
    height = box[3] - box[1]
    draw.text(
        (xy[0] - width / 2, xy[1] - height / 2 - box[1]),
        text,
        font=typeface,
        fill=fill,
    )


def make_frame(index: int):
    frame = create_blank_frame(WIDTH, HEIGHT, CREAM)
    draw = ImageDraw.Draw(frame)

    # A large, high-contrast card keeps the message inside a generous safe area.
    draw.rounded_rectangle((30, 30, 450, 450), radius=42, fill=DARK)
    draw.rounded_rectangle((45, 45, 435, 435), radius=31, outline=BRICK, width=4)

    # Friendly meeting cue: an always-readable clock whose hands move subtly.
    clock_center = (382, 95)
    draw.ellipse((350, 63, 414, 127), fill=CREAM, outline=BRICK, width=5)
    angle = 2 * math.pi * index / FRAME_COUNT
    minute_end = (
        clock_center[0] + 19 * math.sin(angle),
        clock_center[1] - 19 * math.cos(angle),
    )
    draw.line((clock_center, minute_end), fill=DARK, width=5)
    draw.line((clock_center, (370, 86)), fill=DARK, width=5)
    draw.ellipse((378, 91, 386, 99), fill=BRICK)

    # Static text positions maximize dwell time and legibility in every frame.
    centered_text(draw, (240, 108), "MEETING", font(25), BRICK)
    centered_text(draw, (240, 203), "SYNC", font(92), CREAM)

    phase = 0.5 - 0.5 * math.cos(2 * math.pi * index / FRAME_COUNT)
    emphasis = interpolate(0.0, 1.0, phase, easing="ease_in_out")
    underline_half = int(82 + 30 * emphasis)
    draw.rounded_rectangle(
        (240 - underline_half, 259, 240 + underline_half, 269),
        radius=5,
        fill=BRICK,
    )

    draw.rounded_rectangle((78, 292, 402, 397), radius=28, fill=CREAM)
    border_width = 4 + int(2 * emphasis)
    draw.rounded_rectangle(
        (78, 292, 402, 397), radius=28, outline=BRICK, width=border_width
    )
    centered_text(draw, (240, 343), "10:30", font(95), BRICK)

    # Small balanced accents reinforce the warm palette without competing with text.
    for x in (82, 110, 138):
        draw.ellipse((x - 5, 417, x + 5, 427), fill=BRICK)
    draw.line((333, 422, 398, 422), fill=CREAM, width=4)
    return frame


def main(output_path: str):
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    for index in range(FRAME_COUNT):
        builder.add_frame(make_frame(index))
    info = builder.save(
        output_path,
        num_colors=48,
        optimize_for_emoji=False,
        remove_duplicates=False,
    )
    passes, validation = validate_gif(output_path, is_emoji=False, verbose=True)
    if not passes:
        raise RuntimeError(f"Slack message GIF validation failed: {validation}")
    print(info)


if __name__ == "__main__":
    main(sys.argv[1])
