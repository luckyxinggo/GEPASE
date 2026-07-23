import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageSequence

ROOT = Path(__file__).resolve().parents[5]
PACKAGE = ROOT / "benchmarks/canaries/slack-gif-creator/package"
sys.path.insert(0, str(PACKAGE))

import core.easing as easing
import core.frame_composer as frame_composer
import core.gif_builder as gif_builder
import core.validators as validators


OUT = Path(__file__).resolve().parent
GIF_PATH = OUT / "satellite_orbit_ease.gif"
FRAME_COUNT = 20
FPS = 10
START = (17.0, 103.0)
END = (103.0, 43.0)
ARC_HEIGHT = 58.0


def add_glow(base, center, radii, colors):
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    x, y = center
    for radius, color in zip(radii, colors):
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(3))
    return Image.alpha_composite(base.convert("RGBA"), glow)


def make_frame(index, positions):
    t = index / (FRAME_COUNT - 1)
    eased = easing.interpolate(0.0, 1.0, t, easing="ease_out")
    sx, sy = easing.calculate_arc_motion(START, END, ARC_HEIGHT, eased)
    positions.append({"frame": index, "t": t, "eased_t": eased, "x": sx, "y": sy})

    frame = frame_composer.create_gradient_background(128, 128, (4, 9, 31), (16, 17, 55)).convert("RGBA")
    draw = ImageDraw.Draw(frame)
    # Fixed star field and a faint orbital guide make the arc legible at emoji scale.
    stars = [(10,18,1),(23,38,1),(41,17,2),(60,30,1),(83,13,1),(110,19,2),
             (119,69,1),(12,72,1),(32,116,1),(74,112,1),(112,108,1),(94,89,1)]
    for x, y, r in stars:
        c = (207, 231, 255, 220) if r == 1 else (255, 240, 167, 235)
        draw.ellipse((x-r, y-r, x+r, y+r), fill=c)
    draw.arc((7, 3, 120, 116), 196, 348, fill=(65, 115, 164, 115), width=2)

    # Orange planet with glow, rings, terminator shading, and craters.
    frame = add_glow(frame, (67, 73), (31, 26), ((255,118,30,28),(255,151,42,35)))
    draw = ImageDraw.Draw(frame)
    draw.ellipse((35, 69, 100, 89), outline=(255, 191, 89, 190), width=4)
    draw.ellipse((43, 49, 91, 97), fill=(244, 113, 30, 255), outline=(255, 178, 65, 255), width=3)
    draw.ellipse((51, 54, 72, 91), fill=(255, 147, 40, 255))
    draw.ellipse((76, 59, 84, 66), fill=(193, 72, 31, 230))
    draw.ellipse((69, 80, 77, 87), fill=(215, 83, 29, 230))
    draw.arc((35, 69, 100, 89), 182, 352, fill=(255, 216, 125, 255), width=3)

    # Cyan satellite: glow, body, solar panels, antenna, and thrust trail.
    frame = add_glow(frame, (sx, sy), (12, 9), ((20,235,255,30),(15,209,231,50)))
    draw = ImageDraw.Draw(frame)
    if index < FRAME_COUNT - 3:
        trail = 8 + int(4 * (1-eased))
        draw.line((sx-trail, sy+5, sx-4, sy+2), fill=(63, 224, 255, 150), width=3)
    draw.rectangle((sx-11, sy-4, sx-5, sy+5), fill=(20, 160, 198, 255), outline=(104, 243, 255, 255), width=2)
    draw.rectangle((sx+5, sy-4, sx+11, sy+5), fill=(20, 160, 198, 255), outline=(104, 243, 255, 255), width=2)
    draw.ellipse((sx-6, sy-6, sx+6, sy+6), fill=(38, 221, 235, 255), outline=(193, 255, 250, 255), width=2)
    draw.ellipse((sx-2, sy-3, sx+2, sy+1), fill=(220, 255, 250, 255))
    draw.line((sx, sy-6, sx+3, sy-11), fill=(122, 246, 255, 255), width=2)
    draw.ellipse((sx+1, sy-13, sx+5, sy-9), fill=(255, 230, 112, 255))

    # Docking beacon grows gently at the destination.
    pulse = 2 + int(2 * max(0, eased - 0.75) / 0.25)
    draw.ellipse((END[0]-pulse, END[1]-pulse, END[0]+pulse, END[1]+pulse), outline=(128,255,241,180), width=1)
    return frame.convert("RGB")


def inspect_gif(path, planned_positions):
    with Image.open(path) as im:
        frames = [f.convert("RGB") for f in ImageSequence.Iterator(im)]
        durations = [f.info.get("duration", im.info.get("duration", 0)) for f in ImageSequence.Iterator(im)]
        loop = im.info.get("loop")
        size = im.size
    centroids = []
    for idx, frame in enumerate(frames):
        pixels = frame.load()
        pts = []
        for y in range(128):
            for x in range(128):
                r, g, b = pixels[x, y]
                if g > 155 and b > 175 and b-r > 55 and g-r > 35:
                    pts.append((x, y))
        centroids.append({"frame": idx, "x": sum(p[0] for p in pts)/len(pts), "y": sum(p[1] for p in pts)/len(pts), "pixels": len(pts)})
    planned_steps = [math.dist((a["x"],a["y"]),(b["x"],b["y"])) for a,b in zip(planned_positions, planned_positions[1:])]
    last_steps = planned_steps[-6:]
    return {
        "dimensions": list(size), "frame_count": len(frames), "durations_ms": durations,
        "total_duration_ms": sum(durations), "loop": loop,
        "cyan_centroids": centroids,
        "planned_step_distances": planned_steps,
        "last_six_step_distances": last_steps,
        "ease_out_last_steps_strictly_decreasing": all(a > b for a,b in zip(last_steps,last_steps[1:])),
        "arc_peak_y": min(p["y"] for p in planned_positions),
        "start_position": planned_positions[0], "end_position": planned_positions[-1],
    }


def main():
    positions = []
    builder = gif_builder.GIFBuilder(width=128, height=128, fps=FPS)
    for i in range(FRAME_COUNT):
        builder.add_frame(make_frame(i, positions))
    build_info = builder.save(GIF_PATH, num_colors=64, optimize_for_emoji=False, remove_duplicates=False)
    slack_pass, slack_info = validators.validate_gif(GIF_PATH, is_emoji=True, verbose=True)
    slack_ready = validators.is_slack_ready(GIF_PATH, is_emoji=True, verbose=False)
    inspection = inspect_gif(GIF_PATH, positions)
    build_info["path"] = "artifacts/runs/r3-slack-gif-creator-paired/workspaces/work-8b18ac4a317bfa0522d70aca/satellite_orbit_ease.gif"
    slack_info["file"] = build_info["path"]
    verification = {"build_info": build_info, "validator": slack_info, "slack_pass": slack_pass,
                    "is_slack_ready": slack_ready, "inspection": inspection}
    (OUT / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
    if not (slack_pass and slack_ready and inspection["dimensions"] == [128,128]
            and inspection["frame_count"] == FRAME_COUNT and inspection["total_duration_ms"] <= 2700
            and inspection["loop"] == 0 and inspection["ease_out_last_steps_strictly_decreasing"]):
        raise SystemExit("verification failed")


if __name__ == "__main__":
    main()
