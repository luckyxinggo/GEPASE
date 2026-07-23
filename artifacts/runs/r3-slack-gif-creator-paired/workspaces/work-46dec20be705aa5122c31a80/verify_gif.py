from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageSequence


ROOT = Path(__file__).resolve().parent
GIF = ROOT / "emoji_star_bounce.gif"


with Image.open(GIF) as image:
    frames = [frame.convert("RGB") for frame in ImageSequence.Iterator(image)]
    durations = [frame.info.get("duration", image.info.get("duration")) for frame in ImageSequence.Iterator(image)]
    loop = image.info.get("loop")
    size = image.size

centers = []
for frame in frames:
    coords = []
    for y in range(frame.height):
        for x in range(frame.width):
            r, g, b = frame.getpixel((x, y))
            if r > 205 and g > 125 and b < 100:
                coords.append((x, y))
    centers.append(round(sum(y for _, y in coords) / len(coords), 2) if coords else None)

sheet = Image.new("RGB", (128 * 6, 128 * 4), "white")
draw = ImageDraw.Draw(sheet)
for i, frame in enumerate(frames):
    x, y = (i % 6) * 128, (i // 6) * 128
    sheet.paste(frame, (x, y))
    draw.text((x + 4, y + 4), str(i), fill="white", stroke_width=2, stroke_fill="black")
sheet.save(ROOT / "verification_contact_sheet.png")

print(json.dumps({
    "size": list(size),
    "frames": len(frames),
    "durations_ms": durations,
    "total_duration_ms": sum(durations),
    "loop": loop,
    "yellow_centroid_y": centers,
    "file_size_bytes": GIF.stat().st_size,
}, ensure_ascii=False, indent=2))
