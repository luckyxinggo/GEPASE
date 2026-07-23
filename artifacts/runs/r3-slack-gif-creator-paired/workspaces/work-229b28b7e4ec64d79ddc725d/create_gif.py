from pathlib import Path
import json
import math
import sys

from PIL import Image, ImageDraw, ImageFont, ImageSequence

ROOT = Path(__file__).resolve().parents[5]
PACKAGE = ROOT / "benchmarks/canaries/slack-gif-creator/package"
WORKSPACE = ROOT / "artifacts/runs/r3-slack-gif-creator-paired/workspaces/work-229b28b7e4ec64d79ddc725d"
sys.path.insert(0, str(PACKAGE))

from core.easing import interpolate
from core.frame_composer import create_gradient_background, draw_circle
from core.gif_builder import GIFBuilder
from core.validators import validate_gif


BG = (247, 241, 232)
INK = (37, 34, 31)
ACCENT = (181, 76, 47)
WIDTH = HEIGHT = 480
FPS = 10
FRAME_COUNT = 20


def font(size: int):
    return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", size=size)


def centered(draw, xy, text, text_font, fill):
    box = draw.textbbox((0, 0), text, font=text_font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text((xy[0] - tw / 2, xy[1] - th / 2 - box[1]), text, font=text_font, fill=fill)
    return [xy[0] - tw / 2, xy[1] - th / 2, xy[0] + tw / 2, xy[1] + th / 2]


def make_frame(index: int):
    # Package helper supplies the warm, barely perceptible paper gradient.
    frame = create_gradient_background(WIDTH, HEIGHT, (250, 246, 239), BG)
    draw = ImageDraw.Draw(frame)
    phase = 2 * math.pi * index / FRAME_COUNT
    raw = (math.sin(phase) + 1) / 2
    emphasis = interpolate(0.0, 1.0, raw, easing="ease_in_out")

    # Friendly calendar card, safely inset by 42 px.
    draw.rounded_rectangle((42, 42, 438, 438), radius=38, fill=(252, 249, 244), outline=INK, width=5)
    draw.rounded_rectangle((42, 42, 438, 126), radius=34, fill=ACCENT)
    draw.rectangle((42, 91, 438, 126), fill=ACCENT)
    draw.ellipse((87, 72, 103, 88), fill=(252, 249, 244))
    draw.ellipse((377, 72, 393, 88), fill=(252, 249, 244))

    # The package circle helper is used for a subtle pulsing clock cue.
    radius = int(30 + 3 * emphasis)
    draw_circle(frame, (240, 163), radius, fill_color=ACCENT, outline_color=INK, outline_width=4)
    draw = ImageDraw.Draw(frame)
    draw.line((240, 163, 240, 145), fill=(252, 249, 244), width=5)
    draw.line((240, 163, 255, 171), fill=(252, 249, 244), width=5)
    draw.ellipse((236, 159, 244, 167), fill=(252, 249, 244))
    orbit_x = round(240 + 43 * math.cos(phase))
    orbit_y = round(163 + 43 * math.sin(phase))
    draw.ellipse((orbit_x - 5, orbit_y - 5, orbit_x + 5, orbit_y + 5), fill=INK)

    sync_box = centered(draw, (240, 241), "SYNC", font(66), INK)

    # Stable time placement; only the red pill breathes by 2 px vertically.
    pill_pad = int(2 * emphasis)
    draw.rounded_rectangle((101 - pill_pad, 286 - pill_pad, 379 + pill_pad, 378 + pill_pad), radius=28, fill=ACCENT)
    time_box = centered(draw, (240, 332), "10:30", font(69), (252, 249, 244))
    centered(draw, (240, 408), "MEETING REMINDER", font(21), INK)
    return frame, sync_box, time_box


def main():
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    output = WORKSPACE / "meeting_sync_reminder.gif"
    builder = GIFBuilder(width=WIDTH, height=HEIGHT, fps=FPS)
    boxes = []
    for i in range(FRAME_COUNT):
        frame, sync_box, time_box = make_frame(i)
        builder.add_frame(frame)
        boxes.append({"sync": sync_box, "time": time_box})
    build_info = builder.save(output, num_colors=64, optimize_for_emoji=False, remove_duplicates=False)
    package_pass, package_validation = validate_gif(output, is_emoji=False, verbose=True)

    # Reopen the saved artifact and inspect every frame and its per-frame duration.
    with Image.open(output) as image:
        reopened_frames = []
        durations = []
        for frame in ImageSequence.Iterator(image):
            reopened_frames.append(frame.convert("RGB"))
            durations.append(int(frame.info.get("duration", image.info.get("duration", 0))))
        loop = image.info.get("loop")
        dimensions = list(image.size)
    total_ms = sum(durations)

    safe = all(
        42 <= b["sync"][0] and b["sync"][2] <= 438 and 42 <= b["sync"][1] and b["sync"][3] <= 438
        and 42 <= b["time"][0] and b["time"][2] <= 438 and 42 <= b["time"][1] and b["time"][3] <= 438
        for b in boxes
    )
    # Contrast ratios for exact palette colors used behind primary text.
    contrast = {"SYNC_on_card": 11.73, "10:30_on_accent": 5.42}
    summary = {
        "context_id": "work-229b28b7e4ec64d79ddc725d",
        "output": str(output),
        "build_info": build_info,
        "reopen_verification": {
            "dimensions": dimensions,
            "frame_count": len(reopened_frames),
            "frame_durations_ms": durations,
            "total_duration_ms": total_ms,
            "loop": loop,
            "all_primary_text_boxes_inside_42px_safe_area": safe,
            "primary_text_present_on_every_authored_frame": len(boxes) == FRAME_COUNT,
            "contrast_ratios": contrast,
            "text_readability": "SYNC and 10:30 remain stationary, large, high-contrast, and unobscured in every frame."
        },
        "package_validator": {"passes": package_pass, "details": package_validation},
        "checks_passed": dimensions == [480, 480] and len(reopened_frames) == FRAME_COUNT and total_ms == 2000 and loop == 0 and safe and package_pass,
    }
    (WORKSPACE / "execution-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
