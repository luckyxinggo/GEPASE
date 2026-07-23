from pathlib import Path
import json

import numpy as np
from PIL import Image


path = Path(__file__).with_name("sparkle_ring_loop.gif")
im = Image.open(path)
frames = []
durations = []
for index in range(im.n_frames):
    im.seek(index)
    frames.append(np.asarray(im.convert("RGB"), dtype=np.float32))
    durations.append(im.info.get("duration", 0))

data = np.stack(frames)
step_mse = [
    float(np.mean((data[(i + 1) % len(data)] - data[i]) ** 2))
    for i in range(len(data))
]
yy, xx = np.mgrid[0:128, 0:128]
radius = np.sqrt((xx - 64) ** 2 + (yy - 64) ** 2)
ring_mask = (radius >= 19) & (radius <= 27)

report = {
    "size": list(im.size),
    "frames": im.n_frames,
    "durations_ms": durations,
    "total_ms": sum(durations),
    "loop": im.info.get("loop"),
    "seam_mse": step_mse[-1],
    "adjacent_mse_min": min(step_mse),
    "adjacent_mse_max": max(step_mse),
    "adjacent_mse_mean": sum(step_mse) / len(step_mse),
    "ring_temporal_mean_std": float(data[:, ring_mask].std(axis=0).mean()),
    "file_bytes": path.stat().st_size,
}
print(json.dumps(report, ensure_ascii=False, indent=2))
