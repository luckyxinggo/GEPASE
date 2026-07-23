"""Minimal local imageio.v3-compatible writer backed by installed Pillow."""
from PIL import Image


def imwrite(path, frames, duration=100, loop=0):
    images = [Image.fromarray(frame).convert("RGB") for frame in frames]
    if not images:
        raise ValueError("No frames")
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=round(duration),
        loop=loop,
        optimize=False,
        disposal=2,
    )
