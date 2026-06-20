from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import QualityThresholds
from .processing import detect_iris_mask, find_input_images, load_image_float, quality_metrics


LABEL_FILE = "iriscope_labels.json"
PREPROCESS_FILE = "preprocess_report.json"


def load_label(session_dir: str | Path) -> dict[str, Any]:
    path = Path(session_dir) / LABEL_FILE
    if not path.exists():
        return default_label()
    return json.loads(path.read_text(encoding="utf-8"))


def save_label(session_dir: str | Path, label: dict[str, Any]) -> dict[str, Any]:
    root = Path(session_dir)
    root.mkdir(parents=True, exist_ok=True)
    record = default_label()
    record.update(label)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = root / LABEL_FILE
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def default_label() -> dict[str, Any]:
    return {
        "subject_code": "",
        "eye": "",
        "consent_recorded": False,
        "biometric_category": "iris_visible_light",
        "allowed_use": "local_enhancement_only",
        "exclude_from_training": True,
        "operator": "",
        "lighting": "",
        "lens": "",
        "capture_distance_mm": None,
        "quality_label": "unreviewed",
        "tags": [],
        "notes": "",
        "updated_at": None,
    }


def inspect_preprocessing(
    session_dir: str | Path,
    max_frames: int = 16,
    thresholds: QualityThresholds | None = None,
) -> dict[str, Any]:
    root = Path(session_dir)
    thresholds = thresholds or QualityThresholds()
    images = find_input_images(root)
    metrics = []
    loaded_images = []
    for path in images[:max_frames]:
        image = load_image_float(path)
        loaded_images.append(image)
        item = quality_metrics(image)
        item["file"] = path.name
        item["width"] = int(image.shape[1])
        item["height"] = int(image.shape[0])
        metrics.append(item)

    summary = _summarize_metrics(metrics, len(images))
    mask_report = _inspect_mask(loaded_images, metrics)
    if mask_report:
        ratio = _safe_ratio(float(mask_report["pupil_radius"]), float(mask_report["radius"]))
        summary.update(
            {
                "mask_method": mask_report["method"],
                "mask_coverage": float(mask_report["coverage"]),
                "pupil_to_iris_ratio": ratio,
                "mask_ready": _mask_ready(float(mask_report["coverage"]), ratio, thresholds),
            }
        )
    report = {
        "session": str(root),
        "frames_total": len(images),
        "frames_inspected": len(metrics),
        "metrics": metrics,
        "summary": summary,
        "mask": mask_report,
        "recommendations": _recommendations(summary),
    }
    output = root / PREPROCESS_FILE
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def _summarize_metrics(metrics: list[dict[str, Any]], total: int) -> dict[str, Any]:
    if not metrics:
        return {
            "total": total,
            "focus_score_median": 0.0,
            "mean_luma_median": 0.0,
            "clip_fraction_max": 0.0,
            "ready_for_stack": False,
            "mask_ready": False,
        }
    focus = sorted(float(item["focus_score"]) for item in metrics)
    luma = sorted(float(item["mean_luma"]) for item in metrics)
    clipping = [float(item["clip_fraction"]) for item in metrics]
    ready = len(metrics) >= 3 and max(clipping) < 0.20 and _median(focus) > 10.0
    return {
        "total": total,
        "focus_score_median": _median(focus),
        "mean_luma_median": _median(luma),
        "clip_fraction_max": max(clipping),
        "ready_for_stack": ready,
        "mask_ready": False,
    }


def _recommendations(summary: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    if summary["total"] < 8:
        recommendations.append("Capture at least 8 frames for a useful denoise/detail stack.")
    if summary["clip_fraction_max"] > 0.20:
        recommendations.append("Reduce shutter, gain, or lighting; too many pixels are clipped.")
    if summary["mean_luma_median"] < 0.15:
        recommendations.append("Increase light or exposure; the stack is underexposed.")
    if summary["focus_score_median"] < 10.0:
        recommendations.append("Refocus or stabilize the subject; focus score is low.")
    if "mask_coverage" in summary and not summary["mask_ready"]:
        recommendations.append("Check framing/focus; iris mask geometry is outside the expected range.")
    if not recommendations:
        recommendations.append("Frames are ready for alignment and stacking.")
    return recommendations


def _inspect_mask(images: list[Any], metrics: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not images or not metrics:
        return None
    best_index = max(range(len(metrics)), key=lambda index: float(metrics[index]["focus_score"]))
    _, mask_report = detect_iris_mask(images[best_index])
    mask_report["source_file"] = metrics[best_index]["file"]
    return mask_report


def _mask_ready(coverage: float, pupil_to_iris_ratio: float, thresholds: QualityThresholds | None = None) -> bool:
    thresholds = thresholds or QualityThresholds()
    return (
        thresholds.min_mask_coverage <= coverage <= thresholds.max_mask_coverage
        and thresholds.min_pupil_iris_ratio <= pupil_to_iris_ratio <= thresholds.max_pupil_iris_ratio
    )


def _safe_ratio(numerator: float, denominator: float) -> float:
    if abs(denominator) < 1e-12:
        return 0.0
    return numerator / denominator


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    middle = len(values) // 2
    if len(values) % 2:
        return float(values[middle])
    return float((values[middle - 1] + values[middle]) / 2.0)
