from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


WORKSPACE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[5]
PACKAGE = REPO / "benchmarks/canaries/slack-gif-creator/package"
FIXTURE = REPO / "benchmarks/canaries/slack-gif-creator/fixtures/input-badge.ppm"
sys.path.insert(0, str(PACKAGE))

from core.easing import interpolate  # noqa: E402
from core.frame_composer import create_gradient_background  # noqa: E402
from core.validators import validate_gif  # noqa: E402


def lift_y(index: int, count: int) -> float:
    t = index / (count - 1)
    if t <= 0.58:
        return interpolate(56, 22, t / 0.58, "ease_out")
    if t <= 0.82:
        return interpolate(22, 31, (t - 0.58) / 0.24, "ease_in_out")
    return interpolate(31, 29, (t - 0.82) / 0.18, "ease_out")


def main() -> None:
    frame_count = 18
    source = Image.open(FIXTURE).convert("RGB")
    badge = source.resize((72, 72), Image.Resampling.NEAREST).convert("RGBA")
    frames = []

    for i in range(frame_count):
        frame = create_gradient_background(128, 128, (7, 24, 39), (16, 55, 65)).convert("RGBA")
        y = round(lift_y(i, frame_count))
        x = 28

        glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        glow_alpha = max(20, 72 - abs(y - 28) * 2)
        gd.ellipse((22, y - 8, 106, y + 79), fill=(42, 203, 190, glow_alpha))
        glow = glow.filter(ImageFilter.GaussianBlur(13))
        frame.alpha_composite(glow)

        shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        altitude = max(0, 56 - y)
        shadow_w = max(25, 54 - altitude // 2)
        sd.ellipse((64 - shadow_w, 112, 64 + shadow_w, 120), fill=(0, 4, 10, 95))
        shadow = shadow.filter(ImageFilter.GaussianBlur(5))
        frame.alpha_composite(shadow)

        frame.alpha_composite(badge, (x, y))
        frames.append(frame.convert("RGB"))

    output = WORKSPACE / "uploaded_badge_lift.gif"
    palette_frames = [frame.quantize(colors=48, method=Image.Quantize.MEDIANCUT) for frame in frames]
    palette_frames[0].save(
        output,
        save_all=True,
        append_images=palette_frames[1:],
        duration=60,
        loop=0,
        optimize=False,
        disposal=2,
    )
    build_info = {
        "path": output.name,
        "dimensions": "128x128",
        "requested_frame_count": len(palette_frames),
        "requested_frame_duration_ms": 60,
        "colors": 48,
        "size_bytes": output.stat().st_size,
    }
    passes, validator_info = validate_gif(output, is_emoji=True, verbose=True)
    print(json.dumps({"build": build_info, "validator_passes": passes, "validator": validator_info}, ensure_ascii=False))


if __name__ == "__main__":
    main()
