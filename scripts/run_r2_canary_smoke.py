"""Run the pinned slack-gif-creator import/build/validator technical smoke."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import sys
from pathlib import Path

from gepase.package.analyzer import PackageAnalyzer
from gepase.store.artifacts import ArtifactStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    before = PackageAnalyzer().analyze(package).snapshot.snapshot_hash
    sys.dont_write_bytecode = True
    sys.path.insert(0, package.as_posix())

    from core.easing import interpolate  # type: ignore[import-not-found]
    from core.frame_composer import (  # type: ignore[import-not-found]
        create_gradient_background,
        draw_star,
    )
    from core.gif_builder import GIFBuilder  # type: ignore[import-not-found]
    from core.validators import validate_gif  # type: ignore[import-not-found]
    from PIL import Image, ImageDraw  # type: ignore[import-untyped]

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    gif_path = output_dir / "slack-gif-creator-smoke.gif"
    builder = GIFBuilder(width=128, height=128, fps=12)
    for index in range(12):
        progress = index / 11
        frame = create_gradient_background(128, 128, (15, 28, 55), (38, 70, 104))
        y = int(interpolate(92, 46, progress, easing="bounce_out"))
        draw_star(
            frame,
            (64, y),
            26,
            fill_color=(255, 193, 75),
            outline_color=(64, 38, 20),
            outline_width=3,
        )
        draw = ImageDraw.Draw(frame)
        draw.ellipse((54, y - 12, 61, y - 5), fill=(255, 244, 190))
        builder.add_frame(Image.Image.convert(frame, "RGB"))
    build_info = builder.save(
        gif_path,
        num_colors=48,
        optimize_for_emoji=True,
        remove_duplicates=True,
    )
    validator_passed, validation = validate_gif(gif_path, is_emoji=True, verbose=False)
    build_info = {**build_info, "path": gif_path.name}
    validation = {**validation, "file": gif_path.name}
    after = PackageAnalyzer().analyze(package).snapshot.snapshot_hash
    digest = hashlib.sha256(gif_path.read_bytes()).hexdigest()
    store = ArtifactStore(output_dir)
    store.index_existing("slack-gif-creator-smoke.gif", "image/gif")
    report = {
        "valid": bool(
            validator_passed
            and before == after
            and validation.get("width") == 128
            and validation.get("height") == 128
            and int(validation.get("frame_count", 0)) >= 2
        ),
        "package_snapshot_before": before,
        "package_snapshot_after": after,
        "source_package_unchanged": before == after,
        "core_imported": True,
        "gif_sha256": digest,
        "build_info": build_info,
        "validation": validation,
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("pillow", "imageio", "imageio-ffmpeg", "numpy")
        },
    }
    store.write_json("smoke-report.json", report)
    print(report)
    if not report["valid"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
