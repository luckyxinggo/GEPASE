#!/usr/bin/env python3
import json
import math
import os
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE / "_deps"))

from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = WORKSPACE.parents[4]
PACKAGE = REPO_ROOT / "benchmarks/canaries/slack-gif-creator/package"
sys.path.insert(0, str(PACKAGE))

from core.easing import interpolate
from core.frame_composer import create_gradient_background
from core.gif_builder import GIFBuilder

W = H = 480
FPS = 12
FRAME_COUNT = 22
OUT = WORKSPACE / "compact_check_burst.gif"


def rgba_layer():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def generate():
    builder = GIFBuilder(width=W, height=H, fps=FPS)
    palette = [(72, 225, 178), (92, 198, 255), (255, 205, 84), (255, 113, 159)]
    particle_count = 24

    for i in range(FRAME_COUNT):
        t = i / (FRAME_COUNT - 1)
        frame = create_gradient_background(W, H, (15, 24, 54), (24, 41, 77)).convert("RGBA")

        # Restrained halo gives depth while leaving large, compressible background areas.
        halo = rgba_layer()
        hd = ImageDraw.Draw(halo)
        halo_alpha = int(42 * math.sin(math.pi * min(1.0, t * 1.35)))
        hd.ellipse((119, 119, 361, 361), fill=(53, 231, 174, halo_alpha))
        halo = halo.filter(ImageFilter.GaussianBlur(25))
        frame = Image.alpha_composite(frame, halo)

        # Radial burst: rapid ease-out travel and late fade.
        particles = rgba_layer()
        pd = ImageDraw.Draw(particles)
        travel = interpolate(0, 1, min(1.0, t / 0.78), easing="ease_out")
        fade = 1.0 if t < 0.36 else max(0.0, 1 - (t - 0.36) / 0.64)
        for p in range(particle_count):
            angle = (2 * math.pi * p / particle_count) + (0.055 if p % 2 else -0.03)
            start_r = 83 + 8 * (p % 3)
            end_r = 185 + 14 * (p % 4)
            r = start_r + (end_r - start_r) * travel
            x = 240 + math.cos(angle) * r
            y = 240 + math.sin(angle) * r + 14 * t * t
            size = max(2, int((7 + (p % 3) * 2) * (0.55 + 0.45 * fade)))
            color = palette[p % len(palette)]
            alpha = int(255 * fade)
            if p % 4 == 0:
                # Compact diamond sparkle.
                pd.polygon([(x, y-size*1.5), (x+size, y), (x, y+size*1.5), (x-size, y)], fill=(*color, alpha))
            else:
                pd.ellipse((x-size, y-size, x+size, y+size), fill=(*color, alpha))
        frame = Image.alpha_composite(frame, particles)

        # Check badge pops in with eased scale, then holds to keep the silhouette readable.
        badge_t = min(1.0, t / 0.32)
        scale = interpolate(0.48, 1.0, badge_t, easing="back_out")
        scale = max(0.25, min(1.08, scale))
        badge = rgba_layer()
        bd = ImageDraw.Draw(badge)
        radius = int(104 * scale)
        box = (240-radius, 240-radius, 240+radius, 240+radius)
        bd.ellipse(box, fill=(22, 170, 126, 255), outline=(112, 255, 205, 255), width=max(5, int(9*scale)))
        # Two-segment check with rounded joints via endpoint circles.
        pts = [(184, 242), (224, 282), (303, 197)]
        pts = [(240 + (x-240)*scale, 240 + (y-240)*scale) for x, y in pts]
        width = max(10, int(22 * scale))
        bd.line(pts, fill=(247, 255, 252, 255), width=width, joint="curve")
        for x, y in pts:
            bd.ellipse((x-width/2, y-width/2, x+width/2, y+width/2), fill=(247, 255, 252, 255))
        frame = Image.alpha_composite(frame, badge)
        builder.add_frame(frame.convert("RGB"))

    return builder.save(OUT, num_colors=40, optimize_for_emoji=False, remove_duplicates=False)


def verify():
    with Image.open(OUT) as im:
        frames = []
        durations = []
        for idx in range(getattr(im, "n_frames", 1)):
            im.seek(idx)
            frames.append(im.convert("RGB").copy())
            durations.append(int(im.info.get("duration", 0)))
        center = frames[len(frames)//2]
        # Content evidence: bright check pixels in central ROI and vivid particle pixels outside badge.
        central_bright = sum(1 for r,g,b in center.crop((150,150,330,330)).getdata() if r > 215 and g > 225 and b > 215)
        vivid_outer = 0
        pix = center.load()
        for y in range(H):
            for x in range(W):
                if (x-240)**2 + (y-240)**2 > 125**2:
                    r,g,b = pix[x,y]
                    if max(r,g,b) - min(r,g,b) > 65 and max(r,g,b) > 150:
                        vivid_outer += 1
        return {
            "path": str(OUT),
            "format": im.format,
            "dimensions": list(frames[0].size),
            "frame_count": len(frames),
            "loop": im.info.get("loop"),
            "duration_ms_per_frame": durations,
            "effective_fps": round(1000 / (sum(durations)/len(durations)), 3),
            "file_size_bytes": OUT.stat().st_size,
            "file_size_kb": round(OUT.stat().st_size / 1024, 3),
            "central_bright_check_pixels_midframe": central_bright,
            "vivid_outer_particle_pixels_midframe": vivid_outer,
            "checks": {
                "dimensions_480x480": frames[0].size == (480,480),
                "fps_within_10_16": 10 <= 1000/(sum(durations)/len(durations)) <= 16,
                "under_900_kb": OUT.stat().st_size <= 900*1024,
                "infinite_loop": im.info.get("loop") == 0,
                "multi_frame": len(frames) > 1,
                "check_visibly_present": central_bright > 400,
                "particles_visibly_present": vivid_outer > 100,
            },
        }


if __name__ == "__main__":
    build_info = generate()
    verification = verify()
    print(json.dumps({"build": build_info, "verification": verification}, ensure_ascii=False, indent=2))
