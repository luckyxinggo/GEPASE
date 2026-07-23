from pathlib import Path
import math
from PIL import Image, ImageDraw, ImageSequence

OUT = Path(__file__).with_name("meeting_sync_reminder.gif")
BG = "#f7f1e8"
INK = "#25221f"
ACCENT = "#b54c2f"

GLYPHS = {
    "S": ["11111", "10000", "10000", "11111", "00001", "00001", "11111"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "N": ["10001", "11001", "11001", "10101", "10011", "10011", "10001"],
    "C": ["11111", "10000", "10000", "10000", "10000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    ":": ["0", "1", "1", "0", "1", "1", "0"],
}


def text_size(text, scale, gap=None):
    gap = gap if gap is not None else scale
    widths = [len(GLYPHS[ch][0]) * scale for ch in text]
    return sum(widths) + gap * (len(text) - 1), 7 * scale


def pixel_text(draw, xy, text, scale, fill, gap=None):
    x, y = xy
    gap = gap if gap is not None else scale
    for ch in text:
        glyph = GLYPHS[ch]
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    x0, y0 = x + col * scale, y + row * scale
                    draw.rectangle((x0, y0, x0 + scale - 1, y0 + scale - 1), fill=fill)
        x += len(glyph[0]) * scale + gap


frames = []
for i in range(20):
    phase = 2 * math.pi * i / 20
    pulse = (math.sin(phase) + 1) / 2
    im = Image.new("RGB", (480, 480), BG)
    d = ImageDraw.Draw(im)

    # Warm card, safely inset on every side.
    d.rounded_rectangle((34, 38, 446, 442), radius=34, fill="#fffaf3", outline=INK, width=5)
    d.rounded_rectangle((57, 62, 423, 103), radius=20, fill=ACCENT)
    d.ellipse((74, 77, 86, 89), fill=BG)
    d.ellipse((394, 77, 406, 89), fill=BG)

    # Fixed text positions: the only motion is a soft underline and dot pulse.
    sync_scale = 18
    sw, sh = text_size("SYNC", sync_scale, 12)
    pixel_text(d, ((480 - sw) // 2, 139), "SYNC", sync_scale, INK, 12)

    time_scale = 15
    tw, th = text_size("10:30", time_scale, 9)
    pixel_text(d, ((480 - tw) // 2, 274), "10:30", time_scale, ACCENT, 9)

    underline_half = int(88 + 18 * pulse)
    d.rounded_rectangle((240 - underline_half, 392, 240 + underline_half, 401), radius=5, fill=ACCENT)
    dot_r = int(5 + 3 * pulse)
    d.ellipse((240 - dot_r, 116 - dot_r, 240 + dot_r, 116 + dot_r), fill=ACCENT)
    # A tiny 20-step progress bead ensures every encoded frame is distinct.
    bead_x = 150 + i * 9
    d.ellipse((bead_x - 3, 417, bead_x + 3, 423), fill=ACCENT)
    frames.append(im)

frames[0].save(
    OUT,
    save_all=True,
    append_images=frames[1:],
    duration=100,
    loop=0,
    disposal=2,
    optimize=False,
)

# Decode the actual artifact and assert delivery-critical properties.
with Image.open(OUT) as check:
    decoded = [frame.copy().convert("RGB") for frame in ImageSequence.Iterator(check)]
    assert check.size == (480, 480)
    assert len(decoded) == 20
    assert check.info.get("loop") == 0
    assert sum(frame.info.get("duration", 100) for frame in ImageSequence.Iterator(check)) == 2000
    # Both high-contrast text colors must survive encoding in substantial areas.
    colors = decoded[0].getcolors(maxcolors=480 * 480)
    counts = {color: count for count, color in colors}
    assert counts.get((37, 34, 31), 0) > 12000
    assert counts.get((181, 76, 47), 0) > 7000

print(f"created={OUT}")
print(f"size={OUT.stat().st_size}")
print("dimensions=480x480 frames=20 duration_ms=2000 loop=0")
