"""Deterministic content and metadata oracle for the frozen R2 GIF EvalPlan.

This module belongs to the public canary, not the generic Eval Core.  It reads
the actual GIF and fixtures, writes replayable inspection evidence, and never
creates or edits the business output.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageSequence


def _load_gif(path: Path) -> tuple[list[np.ndarray], list[int], int | None]:
    with Image.open(path) as image:
        loop = image.info.get("loop")
        frames: list[np.ndarray] = []
        durations: list[int] = []
        for frame in ImageSequence.Iterator(image):
            converted = frame.convert("RGB")
            frames.append(np.asarray(converted, dtype=np.uint8))
            durations.append(int(frame.info.get("duration", image.info.get("duration", 0))))
    if not frames:
        raise ValueError("GIF contains no decodable frames")
    return frames, durations, int(loop) if loop is not None else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frame_hash(frame: np.ndarray) -> str:
    return hashlib.sha256(frame.tobytes()).hexdigest()


def _pixel_delta(first: np.ndarray, second: np.ndarray) -> float:
    return float(np.abs(first.astype(np.int16) - second.astype(np.int16)).mean())


def _color_mask(frame: np.ndarray, color: tuple[int, int, int], tolerance: int = 55) -> np.ndarray:
    delta = np.linalg.norm(frame.astype(np.int16) - np.asarray(color, dtype=np.int16), axis=2)
    return delta <= tolerance


def _centroid(mask: np.ndarray) -> tuple[float, float] | None:
    points = np.argwhere(mask)
    if len(points) == 0:
        return None
    y, x = points.mean(axis=0)
    return float(x), float(y)


def _largest_component(mask: np.ndarray) -> np.ndarray:
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    best: list[tuple[int, int]] = []
    for start_y, start_x in np.argwhere(mask):
        if visited[start_y, start_x]:
            continue
        queue: deque[tuple[int, int]] = deque([(int(start_y), int(start_x))])
        visited[start_y, start_x] = True
        component: list[tuple[int, int]] = []
        while queue:
            y, x = queue.popleft()
            component.append((y, x))
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ny, nx = y + dy, x + dx
                if (
                    0 <= ny < height
                    and 0 <= nx < width
                    and mask[ny, nx]
                    and not visited[ny, nx]
                ):
                    visited[ny, nx] = True
                    queue.append((ny, nx))
        if len(component) > len(best):
            best = component
    result = np.zeros_like(mask, dtype=bool)
    for y, x in best:
        result[y, x] = True
    return result


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    points = np.argwhere(mask)
    if len(points) == 0:
        return None
    y0, x0 = points.min(axis=0)
    y1, x1 = points.max(axis=0)
    return int(x0), int(y0), int(x1 + 1), int(y1 + 1)


def _palette_presence(
    frames: list[np.ndarray], colors: list[tuple[int, int, int]], tolerance: int = 48
) -> list[float]:
    representative = frames[len(frames) // 2]
    return [float(_color_mask(representative, color, tolerance).mean()) for color in colors]


def _trajectory(frames: list[np.ndarray], color: tuple[int, int, int]) -> list[tuple[float, float]]:
    values: list[tuple[float, float]] = []
    for frame in frames:
        center = _centroid(_largest_component(_color_mask(frame, color)))
        if center is not None:
            values.append(center)
    return values


def _direction_changes(values: list[float], epsilon: float = 0.8) -> int:
    signs: list[int] = []
    for first, second in pairwise(values):
        delta = second - first
        sign = 1 if delta > epsilon else -1 if delta < -epsilon else 0
        if sign and (not signs or signs[-1] != sign):
            signs.append(sign)
    return max(0, len(signs) - 1)


def _template_mask(text: str, size: int = 72) -> np.ndarray:
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    canvas = Image.new("L", (max(256, size * len(text)), size * 2), 0)
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 4), text, font=font, fill=255)
    array = np.asarray(canvas) > 96
    box = _bbox(array)
    if box is None:
        return array
    x0, y0, x1, y1 = box
    return array[y0:y1, x0:x1]


def _resize_mask(mask: np.ndarray, size: tuple[int, int] = (192, 48)) -> np.ndarray:
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    return np.asarray(image.resize(size, Image.Resampling.NEAREST)) > 96


def _glyph_similarity(frame: np.ndarray, text: str, color: tuple[int, int, int]) -> float:
    mask = _color_mask(frame, color, tolerance=62)
    component = _largest_component(mask)
    box = _bbox(component)
    if box is None:
        return 0.0
    x0, y0, x1, y1 = box
    candidate = _resize_mask(component[y0:y1, x0:x1])
    template = _resize_mask(_template_mask(text))
    intersection = float(np.logical_and(candidate, template).sum())
    union = float(np.logical_or(candidate, template).sum())
    return intersection / union if union else 0.0


def _write_contact_sheet(frames: list[np.ndarray], path: Path) -> None:
    indices = np.linspace(0, len(frames) - 1, min(12, len(frames)), dtype=int)
    thumbnails: list[Image.Image] = []
    for index in indices:
        image = Image.fromarray(frames[int(index)], mode="RGB")
        image.thumbnail((160, 160), Image.Resampling.LANCZOS)
        thumbnails.append(image.copy())
    columns = min(4, len(thumbnails))
    rows = math.ceil(len(thumbnails) / columns)
    sheet = Image.new("RGB", (columns * 176, rows * 192), "#f3f0e9")
    draw = ImageDraw.Draw(sheet)
    for position, image in enumerate(thumbnails):
        x = (position % columns) * 176 + 8
        y = (position // columns) * 192 + 8
        sheet.paste(image, (x, y))
        draw.text((x, y + 164), f"frame {int(indices[position])}", fill="#25221f")
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, format="PNG")


def _common_measurements(
    frames: list[np.ndarray], durations: list[int], loop: int | None, artifact: Path
) -> dict[str, Any]:
    adjacent = [_pixel_delta(first, second) for first, second in pairwise(frames)]
    total = sum(durations)
    return {
        "width": int(frames[0].shape[1]),
        "height": int(frames[0].shape[0]),
        "frame_count": len(frames),
        "unique_frame_count": len({_frame_hash(frame) for frame in frames}),
        "frame_durations_ms": durations,
        "total_duration_ms": total,
        "effective_fps": (len(frames) * 1000 / total if total else 0.0),
        "loop_count": loop,
        "file_size_bytes": artifact.stat().st_size,
        "mean_adjacent_pixel_delta": float(np.mean(adjacent)) if adjacent else 0.0,
        "adjacent_pixel_deltas": adjacent,
        "first_last_pixel_delta": _pixel_delta(frames[0], frames[-1]),
    }


def _container(
    common: dict[str, Any], width: int, height: int, minimum_unique: int = 2
) -> tuple[bool, dict[str, Any]]:
    passed = bool(
        common["width"] == width
        and common["height"] == height
        and common["frame_count"] >= 2
        and common["unique_frame_count"] >= minimum_unique
        and common["loop_count"] == 0
    )
    return passed, {
        key: common[key]
        for key in (
            "width",
            "height",
            "frame_count",
            "unique_frame_count",
            "loop_count",
        )
    }


def _duration(
    common: dict[str, Any], minimum: int = 1, maximum: int = 10_000
) -> tuple[bool, dict[str, Any]]:
    durations = common["frame_durations_ms"]
    passed = minimum <= common["total_duration_ms"] <= maximum and all(
        value > 0 for value in durations
    )
    return passed, {
        "total_duration_ms": common["total_duration_ms"],
        "minimum_ms": minimum,
        "maximum_ms": maximum,
        "non_positive_frames": sum(value <= 0 for value in durations),
    }


def _evaluate_specific(
    expectation_id: str, frames: list[np.ndarray], common: dict[str, Any]
) -> tuple[bool, str, dict[str, Any]]:
    if expectation_id == "emoji-bounce-container":
        passed, metrics = _container(common, 128, 128, 6)
    elif expectation_id == "emoji-bounce-duration":
        passed, metrics = _duration(common, maximum=2400)
    elif expectation_id == "emoji-bounce-subject":
        yellow = [_color_mask(frame, (245, 196, 55), 78) for frame in frames]
        visible = [float(mask.mean()) for mask in yellow]
        highlights = [float(_color_mask(frame, (255, 245, 190), 50).mean()) for frame in frames]
        passed = sum(value > 0.01 for value in visible) >= math.ceil(len(frames) * 0.6) and max(
            highlights
        ) > 0.0005
        metrics = {"yellow_fraction_by_frame": visible, "highlight_peak": max(highlights)}
    elif expectation_id == "emoji-bounce-motion":
        trajectory = _trajectory(frames, (245, 196, 55))
        y_values = [point[1] for point in trajectory]
        changes = _direction_changes(y_values)
        passed = len(y_values) >= 6 and max(y_values) - min(y_values) >= 12 and 1 <= changes <= 3
        metrics = {"centroid_y": y_values, "direction_changes": changes}
    elif expectation_id == "emoji-bounce-loop":
        threshold = max(15.0, float(np.percentile(common["adjacent_pixel_deltas"], 95)) * 1.5)
        passed = common["loop_count"] == 0 and common["first_last_pixel_delta"] <= threshold
        metrics = {"first_last_delta": common["first_last_pixel_delta"], "threshold": threshold}
    elif expectation_id == "status-container":
        passed, metrics = _container(common, 480, 480)
    elif expectation_id == "status-duration":
        passed, metrics = _duration(common, minimum=2200, maximum=2600)
        metrics["max_frame_share"] = max(common["frame_durations_ms"]) / max(
            1, common["total_duration_ms"]
        )
        passed = passed and metrics["max_frame_share"] < 0.6
    elif expectation_id == "status-text":
        split = max(1, len(frames) // 3)
        deploy = max(
            _glyph_similarity(frame, "DEPLOY", (255, 255, 255)) for frame in frames[: split * 2]
        )
        done = max(
            _glyph_similarity(frame, "DONE", (255, 255, 255))
            for frame in frames[split * 2 :]
        )
        passed = deploy >= 0.1 and done >= 0.1
        metrics = {"deploy_glyph_similarity": deploy, "done_glyph_similarity": done}
    elif expectation_id in {"status-phases", "status-state-change"}:
        thirds = [frames[0], frames[len(frames) // 2], frames[-1]]
        deltas = [_pixel_delta(first, second) for first, second in pairwise(thirds)]
        passed = len(frames) >= 6 and min(deltas) >= 2.0
        metrics = {
            "representative_phase_deltas": deltas,
            "frames_per_phase_floor": len(frames) // 3,
        }
    elif expectation_id == "badge-source-use":
        cyan = [float(_color_mask(frame, (42, 203, 190), 45).mean()) for frame in frames]
        orange = [float(_color_mask(frame, (255, 143, 82), 45).mean()) for frame in frames]
        passed = max(cyan) > 0.003 and max(orange) > 0.0002
        metrics = {"cyan_fraction": cyan, "orange_fraction": orange}
    elif expectation_id == "badge-container":
        passed, metrics = _container(common, 128, 128)
    elif expectation_id == "badge-duration":
        passed, metrics = _duration(common, maximum=2500)
        passed = passed and common["unique_frame_count"] >= 6
        metrics["unique_frame_count"] = common["unique_frame_count"]
    elif expectation_id == "badge-palette":
        cyan = [_centroid(_color_mask(frame, (42, 203, 190), 48)) for frame in frames]
        orange = [_centroid(_color_mask(frame, (255, 143, 82), 48)) for frame in frames]
        paired = [
            (cyan_center, orange_center)
            for cyan_center, orange_center in zip(cyan, orange, strict=True)
            if cyan_center is not None and orange_center is not None
        ]
        distances = [math.dist(c, o) for c, o in paired]
        passed = len(paired) >= math.ceil(len(frames) * 0.6) and np.median(distances) < 25
        metrics = {"paired_visible_frames": len(paired), "centroid_distances": distances}
    elif expectation_id == "badge-overshoot":
        y_values = [point[1] for point in _trajectory(frames, (42, 203, 190))]
        changes = _direction_changes(y_values)
        tail_span = max(y_values[-3:]) - min(y_values[-3:]) if len(y_values) >= 3 else 999.0
        passed = (
            len(y_values) >= 6
            and max(y_values) - min(y_values) >= 8
            and changes >= 1
            and tail_span <= 3
        )
        metrics = {"centroid_y": y_values, "direction_changes": changes, "tail_span": tail_span}
    elif expectation_id == "readable-container":
        passed, metrics = _container(common, 480, 480)
    elif expectation_id == "readable-duration":
        passed, metrics = _duration(common, minimum=1800, maximum=2200)
    elif expectation_id == "readable-ocr":
        sync_scores = [_glyph_similarity(frame, "SYNC", (37, 34, 31)) for frame in frames]
        time_scores = [_glyph_similarity(frame, "10:30", (37, 34, 31)) for frame in frames]
        recognized = sum(
            sync >= 0.08 and time >= 0.06
            for sync, time in zip(sync_scores, time_scores, strict=True)
        )
        passed = recognized / len(frames) >= 0.7
        metrics = {
            "recognized_frame_ratio": recognized / len(frames),
            "method": "glyph-template-iou",
        }
    elif expectation_id == "readable-contrast":
        luminance = [frame.mean(axis=2) for frame in frames]
        contrast = [
            float(np.percentile(value, 95) - np.percentile(value, 5))
            for value in luminance
        ]
        passed = min(contrast) >= 105
        metrics = {"frame_luminance_spread": contrast, "minimum_required": 105}
    elif expectation_id == "readable-palette":
        fractions = _palette_presence(frames, [(247, 241, 232), (37, 34, 31), (181, 76, 47)])
        passed = all(value > 0.001 for value in fractions)
        metrics = {"palette_fractions": fractions}
    elif expectation_id == "orbit-container":
        passed, metrics = _container(common, 128, 128, 8)
    elif expectation_id == "orbit-duration":
        passed, metrics = _duration(common, maximum=2700)
    elif expectation_id == "orbit-subjects":
        cyan = [float(_color_mask(frame, (55, 205, 210), 70).mean()) for frame in frames]
        orange = [float(_color_mask(frame, (242, 142, 55), 70).mean()) for frame in frames]
        passed = min(cyan) > 0.0002 and min(orange) > 0.002 and np.mean(orange) > np.mean(cyan)
        metrics = {"cyan_fraction": cyan, "orange_fraction": orange}
    elif expectation_id in {"orbit-arc", "orbit-ease-out"}:
        points = _trajectory(frames, (55, 205, 210))
        if expectation_id == "orbit-arc" and len(points) >= 4:
            start = np.asarray(points[0])
            end = np.asarray(points[-1])
            span = end - start
            norm = float(np.linalg.norm(span)) or 1.0
            distances = []
            for point in points:
                offset = np.asarray(point) - start
                determinant = float(span[0] * offset[1] - span[1] * offset[0])
                distances.append(abs(determinant) / norm)
            peak_index = int(np.argmax(distances))
            passed = (
                max(distances) >= common["width"] * 0.08
                and 0.2 <= peak_index / (len(points) - 1) <= 0.8
            )
            metrics = {"arc_distances": distances, "peak_index": peak_index}
        elif expectation_id == "orbit-ease-out" and len(points) >= 5:
            displacements = [math.dist(a, b) for a, b in pairwise(points)]
            tail = displacements[max(0, int(len(displacements) * 0.6)) :]
            slope = float(np.polyfit(np.arange(len(tail)), tail, 1)[0]) if len(tail) >= 2 else 0.0
            passed = len(tail) >= 2 and slope < 0 and tail[-1] <= tail[0] * 0.5
            metrics = {"tail_displacements": tail, "linear_slope": slope}
        else:
            passed, metrics = False, {"tracked_points": len(points)}
    elif expectation_id == "loop-container":
        passed, metrics = _container(common, 128, 128, 8)
    elif expectation_id == "loop-duration":
        passed, metrics = _duration(common, maximum=2200)
    elif expectation_id == "loop-ring":
        centers: list[tuple[float, float]] = []
        for frame in frames:
            gray = frame.mean(axis=2)
            height, width = gray.shape
            yy, xx = np.ogrid[:height, :width]
            central = (xx - width / 2) ** 2 + (yy - height / 2) ** 2 <= (width * 0.22) ** 2
            center = _centroid(central & (gray > np.percentile(gray, 75)))
            if center is not None:
                centers.append(center)
        drift = max((math.dist(a, b) for a in centers for b in centers), default=999.0)
        passed = len(centers) == len(frames) and drift <= 3
        metrics = {"tracked_centers": centers, "maximum_drift": drift}
    elif expectation_id == "loop-three-sparkles":
        height, width = frames[0].shape[:2]
        yy, xx = np.ogrid[:height, :width]
        angle = np.arctan2(yy - height / 2, xx - width / 2)
        radius = np.sqrt((xx - width / 2) ** 2 + (yy - height / 2) ** 2)
        ranges: list[float] = []
        for sector in range(3):
            center_angle = -math.pi + (sector + 0.5) * 2 * math.pi / 3
            angular = np.abs(np.angle(np.exp(1j * (angle - center_angle)))) < math.pi / 3
            mask = angular & (radius > width * 0.22)
            values = [float(frame.mean(axis=2)[mask].mean()) for frame in frames]
            ranges.append(max(values) - min(values))
        passed = all(value >= 8 for value in ranges)
        metrics = {"sector_brightness_ranges": ranges}
    elif expectation_id == "loop-seam":
        adjacent = common["adjacent_pixel_deltas"] or [0.0]
        threshold = min(15.0, float(np.percentile(adjacent, 95)) * 1.25)
        passed = common["first_last_pixel_delta"] <= threshold
        metrics = {"first_last_delta": common["first_last_pixel_delta"], "threshold": threshold}
    elif expectation_id == "efficiency-container":
        passed, metrics = _container(common, 480, 480)
    elif expectation_id == "efficiency-size":
        passed = common["file_size_bytes"] <= 921_600
        metrics = {"file_size_bytes": common["file_size_bytes"], "maximum_bytes": 921_600}
    elif expectation_id == "efficiency-fps":
        passed = 10 <= common["effective_fps"] <= 16
        metrics = {"effective_fps": common["effective_fps"], "allowed": [10, 16]}
    elif expectation_id == "efficiency-duration":
        passed, metrics = _duration(common, minimum=1400, maximum=3000)
        share = max(common["frame_durations_ms"]) / max(1, common["total_duration_ms"])
        passed = passed and share < 0.5
        metrics["max_frame_share"] = share
    elif expectation_id == "efficiency-check":
        visible: list[float] = []
        for frame in frames:
            center = frame[60:-60, 60:-60]
            spread = center.max(axis=2) - center.min(axis=2)
            bright = center.mean(axis=2) > 155
            visible.append(float((bright & (spread > 20)).mean()))
        passed = sum(value > 0.002 for value in visible) / len(frames) >= 0.6
        metrics = {"central_high_contrast_fraction": visible}
    elif expectation_id == "efficiency-burst":
        height, width = frames[0].shape[:2]
        yy, xx = np.ogrid[:height, :width]
        radius = np.sqrt((xx - width / 2) ** 2 + (yy - height / 2) ** 2)
        medians: list[float] = []
        visible_area: list[float] = []
        for frame in frames:
            saturation = frame.max(axis=2) - frame.min(axis=2)
            mask = (saturation > 45) & (radius > width * 0.12)
            medians.append(float(np.median(radius[mask])) if mask.any() else 0.0)
            visible_area.append(float(mask.mean()))
        middle = max(1, len(frames) // 2)
        passed = (
            max(medians[middle:]) > min(medians[:middle]) + 8
            and visible_area[-1] < max(visible_area)
        )
        metrics = {"particle_median_radius": medians, "visible_area": visible_area}
    elif expectation_id == "pulse-container":
        passed, metrics = _container(common, 128, 128)
    elif expectation_id == "pulse-duration":
        passed, metrics = _duration(common, maximum=2800)
    elif expectation_id == "pulse-fps":
        passed = 10 <= common["effective_fps"] <= 14 and common["unique_frame_count"] >= 8
        metrics = {
            "effective_fps": common["effective_fps"],
            "unique_frames": common["unique_frame_count"],
        }
    elif expectation_id == "pulse-text":
        scores = [_glyph_similarity(frame, "GO", (255, 255, 255)) for frame in frames]
        passed = sum(score >= 0.12 for score in scores) / len(scores) >= 0.75
        metrics = {"glyph_similarity": scores, "method": "white-glyph-template-iou"}
    elif expectation_id == "pulse-palette":
        fractions = _palette_presence(frames, [(23, 33, 58), (255, 107, 107), (255, 255, 255)])
        passed = all(value > 0.001 for value in fractions)
        metrics = {"palette_fractions": fractions}
    elif expectation_id == "pulse-cycle":
        areas = [float(_color_mask(frame, (255, 107, 107), 55).mean()) for frame in frames]
        amplitude = max(areas) - min(areas)
        seam = abs(areas[-1] - areas[0])
        passed = amplitude > 0.01 and seam <= amplitude * 0.15
        metrics = {"coral_area": areas, "amplitude": amplitude, "seam": seam}
    else:
        passed, metrics = False, {"unsupported_expectation": expectation_id}
    detail = "通过内容/元数据检查" if passed else "未满足已冻结 expectation 的可执行判据"
    return bool(passed), detail, metrics


def evaluate(
    case_payload: dict[str, Any],
    artifact_path: str | Path,
    project_root: str | Path,
    evidence_dir: str | Path,
) -> dict[str, Any]:
    """Evaluate one real GIF and return JSON-compatible replayable evidence."""
    artifact = Path(artifact_path).resolve(strict=True)
    root = Path(project_root).resolve(strict=True)
    if not artifact.is_relative_to(root):
        raise ValueError("artifact must remain inside the project")
    evidence = Path(evidence_dir).resolve()
    if not evidence.is_relative_to(root):
        raise ValueError("evidence directory must remain inside the project")
    frames, durations, loop = _load_gif(artifact)
    common = _common_measurements(frames, durations, loop, artifact)
    contact_sheet = evidence / "contact-sheet.png"
    measurements_path = evidence / "measurements.json"
    _write_contact_sheet(frames, contact_sheet)
    assertions: list[dict[str, Any]] = []
    for expectation in case_payload["expectations"]:
        passed, detail, metrics = _evaluate_specific(expectation["expectation_id"], frames, common)
        assertions.append(
            {
                "assertion_id": expectation["expectation_id"],
                "family": expectation["evidence_kind"],
                "passed": passed,
                "weight": expectation["weight"],
                "detail": detail,
                "measurements": metrics,
            }
        )
    measurements = {
        "schema_version": "1.0.0",
        "case_id": case_payload["case_id"],
        "artifact_sha256": _sha256(artifact),
        "common": common,
        "assertions": assertions,
    }
    evidence.mkdir(parents=True, exist_ok=True)
    measurements_path.write_text(
        json.dumps(measurements, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "inspection": {
            "artifact_sha256": measurements["artifact_sha256"],
            **common,
            "contact_sheet_path": contact_sheet.as_posix(),
            "measurements_path": measurements_path.as_posix(),
        },
        "assertions": assertions,
    }
