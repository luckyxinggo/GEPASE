#!/usr/bin/env python3
"""Generate the requested Slack deployment-status message GIF."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.easing import interpolate
from core.frame_composer import create_gradient_background
from core.gif_builder import GIFBuilder
from core.validators import is_slack_ready, validate_gif


WIDTH = 480
HEIGHT = 480
FPS = 10
FRAME_COUNT = 24
OUTPUT = Path(__file__).with_name("deployment_status.gif")

WAIT = {
    "accent": (163, 112, 255),
    "bright": (216, 193, 255),
    "top": (36, 24, 74),
    "bottom": (12, 18, 35),
}
PROCESS = {
    "accent": (40, 185, 255),
    "bright": (148, 225, 255),
    "top": (15, 46, 78),
    "bottom": (8, 23, 42),
}
COMPLETE = {
    "accent": (47, 211, 143),
    "bright": (168, 255, 215),
    "top": (12, 58, 52),
    "bottom": (7, 27, 34),
}


def font(size: int) -> ImageFont.ImageFont:
    """Use Pillow's bundled scalable default to avoid platform font dependencies."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


FONT_META = font(17)
FONT_PILL = font(15)
FONT_TITLE = font(54)
FONT_SUB = font(18)
FONT_STEP = font(14)


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    eased = interpolate(0.0, 1.0, t, "ease_in_out")
    return tuple(round(x + (y - x) * eased) for x, y in zip(a, b))


def mix_theme(a: dict, b: dict, t: float) -> dict:
    return {key: mix(a[key], b[key], t) for key in a}


def frame_state(index: int) -> tuple[float, dict, str, str]:
    """Return logical progress, blended theme, phase name, and subtitle."""
    if index <= 4:
        return 0.0, WAIT, "WAITING", "WAITING FOR RUNNER"
    if index <= 7:
        p = (index - 4) / 4
        return p, mix_theme(WAIT, PROCESS, p), "WAITING", "RUNNER ACCEPTED"
    if index <= 13:
        return 1.0, PROCESS, "PROCESSING", "DEPLOYING BUILD"
    if index <= 16:
        p = (index - 13) / 4
        return 1 + p, mix_theme(PROCESS, COMPLETE, p), "PROCESSING", "FINALIZING RELEASE"
    if index <= 20:
        return 2.0, COMPLETE, "COMPLETE", "DEPLOYMENT COMPLETE"
    p = (index - 20) / 3
    progress = 2 * (1 - p)
    phase = "COMPLETE" if index < 23 else "WAITING"
    subtitle = "READY TO REPLAY" if index < 23 else "WAITING FOR RUNNER"
    return progress, mix_theme(COMPLETE, WAIT, p), phase, subtitle


def centered(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, text_font, fill, **kwargs) -> None:
    box = draw.textbbox((0, 0), text, font=text_font, stroke_width=kwargs.get("stroke_width", 0))
    x = xy[0] - (box[2] - box[0]) / 2
    y = xy[1] - (box[3] - box[1]) / 2 - box[1]
    draw.text((x, y), text, font=text_font, fill=fill, **kwargs)


def rounded_card(frame: Image.Image, accent: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    draw.rounded_rectangle((34, 38, 454, 458), radius=38, fill=(0, 0, 0, 72))
    draw.rounded_rectangle(
        (27, 27, 453, 453),
        radius=38,
        fill=(9, 15, 27, 238),
        outline=(*accent, 120),
        width=2,
    )
    draw.line((56, 105, 424, 105), fill=(*accent, 62), width=2)


def draw_background(frame: Image.Image, accent: tuple[int, int, int], index: int) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    for x in range(24, WIDTH, 48):
        offset = (index * 3 + x // 3) % 48
        draw.ellipse((x, offset - 14, x + 3, offset - 11), fill=(*accent, 35))
    for radius, alpha in ((172, 12), (125, 16)):
        draw.ellipse(
            (240 - radius, 212 - radius, 240 + radius, 212 + radius),
            outline=(*accent, alpha),
            width=2,
        )


def draw_header(frame: Image.Image, theme: dict, phase: str) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    accent = theme["accent"]
    draw.rounded_rectangle((56, 58, 69, 71), radius=4, fill=(*accent, 255))
    draw.ellipse((60, 61, 65, 66), fill=(245, 250, 255, 240))
    draw.text((78, 54), "PRODUCTION / DEPLOYMENT", font=FONT_META, fill=(198, 209, 224, 255))

    pill_width = max(92, draw.textlength(phase, font=FONT_PILL) + 34)
    left = 424 - pill_width
    draw.rounded_rectangle((left, 52, 424, 79), radius=14, fill=(*accent, 34), outline=(*accent, 150), width=2)
    draw.ellipse((left + 11, 62, left + 18, 69), fill=(*theme["bright"], 255))
    draw.text((left + 25, 56), phase, font=FONT_PILL, fill=(*theme["bright"], 255))


def icon_waiting(layer: Image.Image, index: int, alpha: int) -> None:
    draw = ImageDraw.Draw(layer, "RGBA")
    pulse = 1 + 0.07 * math.sin(index * math.pi / 2)
    for n, x in enumerate((213, 240, 267)):
        local = 0.72 + 0.28 * math.sin((index - n) * 1.35) ** 2
        radius = round(7 * pulse * local)
        draw.ellipse((x - radius, 207 - radius, x + radius, 207 + radius), fill=(216, 193, 255, round(alpha * local)))


def icon_processing(layer: Image.Image, index: int, alpha: int) -> None:
    draw = ImageDraw.Draw(layer, "RGBA")
    start = (index * 44) % 360
    for offset, width, color in ((0, 9, (148, 225, 255)), (180, 5, (40, 185, 255))):
        draw.arc((180, 147, 300, 267), start=start + offset, end=start + offset + 86, fill=(*color, alpha), width=width)
    draw.polygon(((240, 178), (267, 211), (251, 211), (251, 237), (229, 237), (229, 211), (213, 211)), fill=(148, 225, 255, alpha))


def icon_complete(layer: Image.Image, index: int, alpha: int) -> None:
    draw = ImageDraw.Draw(layer, "RGBA")
    glow = 3 + round(3 * math.sin(index * math.pi / 2) ** 2)
    draw.ellipse((184 - glow, 151 - glow, 296 + glow, 263 + glow), fill=(47, 211, 143, round(alpha * 0.10)), outline=(47, 211, 143, round(alpha * 0.35)), width=5)
    draw.line(((210, 208), (233, 231), (274, 184)), fill=(168, 255, 215, alpha), width=14, joint="curve")
    for x, y in ((184, 169), (303, 188), (287, 267)):
        draw.line(((x - 6, y), (x + 6, y)), fill=(168, 255, 215, round(alpha * 0.8)), width=3)
        draw.line(((x, y - 6), (x, y + 6)), fill=(168, 255, 215, round(alpha * 0.8)), width=3)


def draw_status_orb(frame: Image.Image, progress: float, theme: dict, index: int) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    accent = theme["accent"]
    pulse = 4 * math.sin(index * math.pi / 4) ** 2
    draw.ellipse((166 - pulse, 133 - pulse, 314 + pulse, 281 + pulse), fill=(*accent, 13), outline=(*accent, 28), width=3)
    draw.ellipse((178, 145, 302, 269), fill=(12, 22, 36, 245), outline=(*accent, 140), width=3)

    wait_alpha = round(255 * max(0.0, 1 - progress)) if progress <= 1 else 0
    process_alpha = round(255 * (progress if progress <= 1 else max(0.0, 2 - progress)))
    complete_alpha = round(255 * max(0.0, progress - 1)) if progress >= 1 else 0
    if progress > 1 and index >= 21:
        wait_alpha = round(255 * (1 - progress / 2))
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    if wait_alpha:
        icon_waiting(layer, index, wait_alpha)
    if process_alpha:
        icon_processing(layer, index, process_alpha)
    if complete_alpha:
        icon_complete(layer, index, complete_alpha)
    frame.alpha_composite(layer)


def draw_title(frame: Image.Image, index: int, phase: str, subtitle: str, theme: dict) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    title = "DONE" if phase == "COMPLETE" else "DEPLOY"
    centered(draw, (240, 317), title, FONT_TITLE, (*theme["bright"], 255), stroke_width=1, stroke_fill=(*theme["accent"], 210))
    centered(draw, (240, 354), subtitle, FONT_SUB, (175, 188, 205, 255))


def draw_stepper(frame: Image.Image, progress: float, theme: dict) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    xs = (97, 240, 383)
    y = 397
    draw.line((xs[0], y, xs[-1], y), fill=(77, 91, 111, 160), width=5)
    completed_fraction = max(0.0, min(1.0, progress / 2))
    endpoint = xs[0] + (xs[-1] - xs[0]) * completed_fraction
    draw.line((xs[0], y, endpoint, y), fill=(*theme["accent"], 230), width=5)
    labels = ("WAIT", "BUILD", "DONE")
    for step, (x, label) in enumerate(zip(xs, labels)):
        active = progress >= step - 0.05
        fill = (*theme["accent"], 255) if active else (25, 36, 52, 255)
        outline = (*theme["bright"], 240) if active else (91, 105, 124, 200)
        draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=fill, outline=outline, width=3)
        if progress > step + 0.65:
            draw.line(((x - 4, y), (x - 1, y + 4), (x + 6, y - 5)), fill=(238, 255, 248, 255), width=2)
        centered(draw, (x, 425), label, FONT_STEP, outline)


def make_frame(index: int) -> Image.Image:
    progress, theme, phase, subtitle = frame_state(index)
    render_index = 0 if index == FRAME_COUNT - 1 else index
    base = create_gradient_background(WIDTH, HEIGHT, theme["top"], theme["bottom"]).convert("RGBA")
    draw_background(base, theme["accent"], render_index)
    rounded_card(base, theme["accent"])
    draw_header(base, theme, phase)
    draw_status_orb(base, progress, theme, render_index)
    draw_title(base, render_index, phase, subtitle, theme)
    draw_stepper(base, progress, theme)
    return base.convert("RGB")


def main() -> None:
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    for index in range(FRAME_COUNT):
        builder.add_frame(make_frame(index))
    info = builder.save(OUTPUT, num_colors=96, optimize_for_emoji=False, remove_duplicates=False)
    passes, details = validate_gif(OUTPUT, is_emoji=False, verbose=True)
    if not passes or not is_slack_ready(OUTPUT, is_emoji=False, verbose=False):
        raise RuntimeError(f"Slack message GIF validation failed: {details}")
    if details["width"] != 480 or details["height"] != 480 or details["frame_count"] != FRAME_COUNT:
        raise RuntimeError(f"Unexpected GIF metadata: {details}")
    if abs((details["duration_seconds"] or 0) - 2.4) > 0.05:
        raise RuntimeError(f"Unexpected GIF duration: {details}")
    print({"builder": info, "validator": details})


if __name__ == "__main__":
    main()
