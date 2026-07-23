from PIL import Image, ImageDraw, ImageFont
import math


SIZE = 480
FRAMES_PER_PHASE = 8
FRAME_MS = 100
OUT = "deployment_status.gif"


def font(size):
    return ImageFont.load_default(size=size)


def centered(draw, xy, text, fnt, fill):
    box = draw.textbbox((0, 0), text, font=fnt)
    w = box[2] - box[0]
    h = box[3] - box[1]
    draw.text((xy[0] - w / 2, xy[1] - h / 2 - box[1]), text, font=fnt, fill=fill)


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def frame_for(index):
    phase_index = index // FRAMES_PER_PHASE
    local = index % FRAMES_PER_PHASE
    t = local / FRAMES_PER_PHASE
    names = ["WAITING", "PROCESSING", "COMPLETE"]
    colors = [(238, 174, 57), (69, 148, 255), (52, 199, 121)]
    bg_tints = [(36, 30, 30), (25, 34, 51), (23, 43, 37)]
    phase_color = colors[phase_index]

    im = Image.new("RGB", (SIZE, SIZE), bg_tints[phase_index])
    d = ImageDraw.Draw(im)

    # Soft concentric background treatment.
    for radius, alpha in [(220, 12), (165, 18), (110, 24)]:
        mix = tuple((c * alpha + bg_tints[phase_index][j] * (255 - alpha)) // 255 for j, c in enumerate(phase_color))
        d.ellipse((240-radius, 220-radius, 240+radius, 220+radius), fill=mix)

    # Header badge.
    rounded(d, (50, 40, 430, 104), 22, (18, 23, 32), (72, 82, 99), 2)
    centered(d, (240, 72), "DEPLOY" if phase_index < 2 else "DONE", font(42), (246, 249, 255))

    # Central animated state glyph.
    cx, cy = 240, 224
    if phase_index == 0:
        pulse = 5 + int(4 * (1 + math.sin(2 * math.pi * t)))
        d.ellipse((cx-65-pulse, cy-65-pulse, cx+65+pulse, cy+65+pulse), outline=(116, 88, 43), width=5)
        d.ellipse((cx-58, cy-58, cx+58, cy+58), fill=(238, 174, 57))
        # Clock face: advancing second hand reinforces waiting, without implying failure.
        d.ellipse((cx-34, cy-34, cx+34, cy+34), outline=(39, 35, 30), width=6)
        angle = -math.pi/2 + 2 * math.pi * t
        d.line((cx, cy, cx + 25*math.cos(angle), cy + 25*math.sin(angle)), fill=(39, 35, 30), width=6)
        d.ellipse((cx-5, cy-5, cx+5, cy+5), fill=(39, 35, 30))
    elif phase_index == 1:
        d.ellipse((cx-70, cy-70, cx+70, cy+70), outline=(52, 78, 112), width=8)
        # Eight-segment spinner with a bright moving head.
        for k in range(8):
            a = 2 * math.pi * k / 8 - math.pi/2
            distance = (k - local) % 8
            strength = max(0.22, 1.0 - distance * 0.12)
            col = tuple(int(c * strength + 22 * (1-strength)) for c in phase_color)
            x1, y1 = cx + 39*math.cos(a), cy + 39*math.sin(a)
            x2, y2 = cx + 62*math.cos(a), cy + 62*math.sin(a)
            d.line((x1, y1, x2, y2), fill=col, width=13)
        centered(d, (cx, cy), "...", font(38), (235, 244, 255))
    else:
        pop = min(1.0, (local + 1) / 4)
        r = int(66 * pop)
        d.ellipse((cx-r, cy-r, cx+r, cy+r), fill=(52, 199, 121), outline=(109, 239, 167), width=5)
        if pop > 0.25:
            pts = [(cx-31*pop, cy+1*pop), (cx-9*pop, cy+25*pop), (cx+37*pop, cy-28*pop)]
            d.line(pts, fill=(17, 46, 34), width=max(5, int(12*pop)), joint="curve")
        # A restrained orbiting success sparkle keeps every completion frame distinct.
        sparkle_angle = -math.pi / 2 + 2 * math.pi * t
        sx, sy = cx + 88 * math.cos(sparkle_angle), cy + 88 * math.sin(sparkle_angle)
        d.ellipse((sx-6, sy-6, sx+6, sy+6), fill=(184, 255, 215))

    centered(d, (240, 320), names[phase_index], font(27), phase_color)

    # Three-stage progress track.
    xs = [100, 240, 380]
    d.line((xs[0], 374, xs[-1], 374), fill=(67, 74, 87), width=8)
    progress_end = xs[phase_index] + (xs[min(phase_index+1, 2)] - xs[phase_index]) * (t if phase_index < 2 else 0)
    d.line((xs[0], 374, progress_end, 374), fill=phase_color, width=8)
    for k, x in enumerate(xs):
        active = k <= phase_index
        d.ellipse((x-15, 359, x+15, 389), fill=(colors[k] if active else (67, 74, 87)), outline=(220, 227, 238), width=2)
        centered(d, (x, 416), ["WAIT", "BUILD", "DONE"][k], font(16), ((226, 232, 241) if active else (125, 134, 149)))

    return im


frames = [frame_for(i) for i in range(FRAMES_PER_PHASE * 3)]
frames[0].save(
    OUT,
    save_all=True,
    append_images=frames[1:],
    duration=FRAME_MS,
    loop=0,
    disposal=2,
    optimize=False,
)
