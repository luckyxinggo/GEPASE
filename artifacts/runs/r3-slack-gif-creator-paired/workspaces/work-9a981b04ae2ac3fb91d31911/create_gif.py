from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WORKSPACE = Path(__file__).resolve().parent
PACKAGE = Path("benchmarks/canaries/slack-gif-creator/package").resolve()
sys.path.insert(0, str(PACKAGE))

from core.easing import interpolate
from core.frame_composer import create_gradient_background
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


W = H = 480
FPS = 10
FRAMES_PER_PHASE = 8


def font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def center_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
                text_font: ImageFont.ImageFont, fill: tuple[int, int, int]) -> None:
    box = draw.textbbox((0, 0), text, font=text_font)
    draw.text((xy[0] - (box[2] - box[0]) / 2, xy[1] - (box[3] - box[1]) / 2),
              text, font=text_font, fill=fill)


def rounded_panel(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle((53, 62, 427, 418), radius=34, fill=(15, 23, 42),
                           outline=(75, 93, 125), width=3)
    draw.rounded_rectangle((76, 86, 404, 122), radius=18, fill=(29, 41, 64))


def frame_for(phase_index: int, local_index: int) -> Image.Image:
    phases = ("WAITING", "PROCESSING", "COMPLETE")
    accents = ((245, 179, 65), (77, 171, 247), (64, 211, 140))
    top = ((32, 36, 62), (23, 45, 72), (18, 56, 50))[phase_index]
    bottom = ((9, 13, 28), (8, 19, 39), (7, 25, 27))[phase_index]
    accent = accents[phase_index]
    t = local_index / (FRAMES_PER_PHASE - 1)
    eased = interpolate(0.0, 1.0, t, easing="ease_in_out")

    img = create_gradient_background(W, H, top, bottom)
    draw = ImageDraw.Draw(img)
    rounded_panel(draw)

    draw.ellipse((91, 98, 101, 108), fill=accent)
    draw.text((115, 94), "DEPLOYMENT PIPELINE", font=font(16), fill=(187, 199, 220))

    # Three-node progress rail: filled nodes encode completed/current stages.
    y = 180
    xs = (130, 240, 350)
    draw.line((xs[0], y, xs[-1], y), fill=(57, 69, 91), width=10)
    progress_end = xs[phase_index] + int((xs[min(phase_index + 1, 2)] - xs[phase_index]) * eased
                                        if phase_index < 2 else 0)
    draw.line((xs[0], y, max(xs[phase_index], progress_end), y), fill=accent, width=10)
    for idx, x in enumerate(xs):
        active = idx <= phase_index
        draw.ellipse((x - 19, y - 19, x + 19, y + 19),
                     fill=accent if active else (33, 45, 67),
                     outline=(235, 242, 252) if idx == phase_index else (88, 102, 127), width=4)
        if idx < phase_index or phase_index == 2:
            draw.line((x - 8, y, x - 2, y + 7, x + 10, y - 8), fill=(10, 30, 29), width=4)
        elif idx == 1 and phase_index == 1:
            angle = local_index * math.pi / 4
            tip = (x + int(9 * math.cos(angle)), y + int(9 * math.sin(angle)))
            draw.line((x, y, tip[0], tip[1]), fill=(8, 30, 52), width=4)
        elif idx == 0:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(74, 45, 5))

    labels = ("QUEUE", "BUILD", "LIVE")
    for x, label in zip(xs, labels):
        center_text(draw, (x, 220), label, font(13), (145, 158, 181))

    # Main status copy stays DEPLOY in the first two phases and changes to DONE only on completion.
    main = "DONE" if phase_index == 2 else "DEPLOY"
    pulse = int(3 * math.sin(math.pi * t))
    center_text(draw, (240, 285 + pulse), main, font(50), (245, 248, 255))
    center_text(draw, (240, 327), phases[phase_index], font(20), accent)

    if phase_index == 0:
        for j in range(3):
            fill = accent if j == local_index % 3 else (72, 78, 95)
            draw.ellipse((220 + j * 18, 365, 228 + j * 18, 373), fill=fill)
    elif phase_index == 1:
        left, right = 126, 354
        draw.rounded_rectangle((left, 362, right, 374), radius=6, fill=(34, 53, 74))
        edge = left + int((right - left) * (0.18 + 0.72 * eased))
        draw.rounded_rectangle((left, 362, edge, 374), radius=6, fill=accent)
    else:
        radius = 15 + int(5 * math.sin(math.pi * t))
        draw.ellipse((240 - radius, 369 - radius, 240 + radius, 369 + radius),
                     fill=(20, 91, 69), outline=accent, width=3)
        draw.line((232, 369, 238, 375, 250, 360), fill=(232, 255, 246), width=4)
        # A small moving completion glint keeps each hold frame distinct in the encoded GIF.
        glint_x = 208 + local_index * 9
        draw.ellipse((glint_x - 3, 397, glint_x + 3, 403), fill=(151, 255, 213))
    return img


def main() -> None:
    builder = GIFBuilder(width=W, height=H, fps=FPS)
    for phase_index in range(3):
        for local_index in range(FRAMES_PER_PHASE):
            builder.add_frame(frame_for(phase_index, local_index))
    output = WORKSPACE / "deployment_status.gif"
    info = builder.save(output, num_colors=96, optimize_for_emoji=False, remove_duplicates=False)
    passes, validation = validate_gif(output, is_emoji=False, verbose=True)
    if not passes:
        raise RuntimeError(validation)
    print({"builder": info, "validator": validation})


if __name__ == "__main__":
    main()
