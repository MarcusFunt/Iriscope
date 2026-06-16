from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .config import ProcessingSettings
from .processing import find_input_images, load_image_float, process_session


SKIP_DIR_NAMES = {
    "__MACOSX",
    "_iriscope_eval",
    "eval-current",
    "eval-runs",
    "processed",
}


@dataclass(frozen=True)
class DatasetEvaluationResult:
    output_dir: Path
    summary_json: Path
    summary_csv: Path
    processed_count: int
    passed_count: int


def discover_dataset_sessions(dataset_dir: str | Path, min_images: int = 3) -> list[Path]:
    root = Path(dataset_dir)
    if not root.exists():
        raise FileNotFoundError(f"Dataset directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Dataset path is not a directory: {root}")

    sessions: list[Path] = []
    candidates = [root, *sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: str(item))]
    for path in candidates:
        if _should_skip(path):
            continue
        try:
            image_count = len(find_input_images(path))
        except OSError:
            continue
        if image_count >= min_images:
            sessions.append(path)
    return sessions


def evaluate_dataset(
    dataset_dir: str | Path,
    output_dir: str | Path | None = None,
    settings: ProcessingSettings | None = None,
    limit: int | None = 40,
    min_images: int = 3,
    offset: int = 0,
) -> DatasetEvaluationResult:
    root = Path(dataset_dir)
    sessions = discover_dataset_sessions(root, min_images=min_images)
    if offset > 0:
        sessions = sessions[offset:]
    if limit is not None and limit > 0:
        sessions = sessions[:limit]
    if not sessions:
        raise FileNotFoundError(f"No dataset image sessions with at least {min_images} images found under {root}")

    settings = settings or ProcessingSettings(save_intermediates=True)
    if not settings.save_intermediates:
        settings = ProcessingSettings(
            stack_method=settings.stack_method,
            sigma=settings.sigma,
            min_frames=settings.min_frames,
            save_intermediates=True,
            max_working_edge=settings.max_working_edge,
        )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = Path(output_dir) if output_dir else root.parent / "eval-runs" / f"{root.name}_{run_id}"
    session_outputs = out / "sessions"
    session_outputs.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for session in sessions:
        session_id = _session_id(root, session)
        process_out = session_outputs / session_id
        row: dict[str, Any] = {
            "session_id": session_id,
            "session_dir": str(session),
            "status": "failed",
            "error": "",
        }
        try:
            result = process_session(session, output_dir=process_out, settings=settings)
            row.update(summarize_processed_session(result.report_json))
            row["status"] = "ok"
        except Exception as exc:  # pragma: no cover - exercised by real-world corrupt datasets
            row["error"] = str(exc)
            row["passed"] = False
            row["flags"] = "processing_failed"
        rows.append(row)

    summary = _summarize_run(root, out, settings, sessions, rows)
    summary_json = out / "dataset_report.json"
    summary_csv = out / "dataset_sessions.csv"
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(summary_csv, rows)
    return DatasetEvaluationResult(
        output_dir=out,
        summary_json=summary_json,
        summary_csv=summary_csv,
        processed_count=sum(1 for row in rows if row.get("status") == "ok"),
        passed_count=sum(1 for row in rows if row.get("passed") is True),
    )


def summarize_processed_session(report_json: str | Path) -> dict[str, Any]:
    report_path = Path(report_json)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    mask = report.get("mask", {})
    frames = report.get("frames", [])
    outputs = report.get("outputs", {})
    input_count = len(report.get("inputs", []))
    kept_count = len(report.get("kept_indices", []))
    stack_count = len(report.get("stacked_input_indices", report.get("kept_indices", [])))
    alignment_scores = [
        float(item.get("score", 0.0))
        for item in report.get("alignment", [])
        if item.get("method") != "reference"
    ]
    focus_values = [float(item.get("focus_score", 0.0)) for item in frames]
    clip_values = [float(item.get("clip_fraction", 0.0)) for item in frames]

    image_shape = _read_image_shape(outputs.get("stacked_tif") or outputs.get("enhanced_tif") or outputs.get("enhanced_jpg"))
    geometry = _mask_geometry(mask, image_shape)
    before_stats = _image_stats(outputs.get("stacked_tif"), outputs.get("iris_mask"))
    after_stats = _image_stats(outputs.get("enhanced_tif") or outputs.get("enhanced_jpg"), outputs.get("iris_mask"))
    contrast_gain = _safe_ratio(after_stats.get("luma_std", 0.0), before_stats.get("luma_std", 0.0))
    edge_gain = _safe_ratio(after_stats.get("edge_score", 0.0), before_stats.get("edge_score", 0.0))

    flags = _quality_flags(
        geometry=geometry,
        kept_count=kept_count,
        stack_count=stack_count,
        min_frames=int(report.get("settings", {}).get("min_frames", 3)),
        focus_median=median(focus_values) if focus_values else 0.0,
        clip_max=max(clip_values) if clip_values else 0.0,
        alignment_median=median(alignment_scores) if alignment_scores else 1.0,
        contrast_gain=contrast_gain,
        edge_gain=edge_gain,
    )
    return {
        "report_json": str(report_path),
        "input_count": input_count,
        "kept_count": kept_count,
        "stack_count": stack_count,
        "rejected_count": max(0, input_count - kept_count),
        "focus_median": median(focus_values) if focus_values else 0.0,
        "clip_max": max(clip_values) if clip_values else 0.0,
        "alignment_median": median(alignment_scores) if alignment_scores else 1.0,
        "mask_method": mask.get("method", ""),
        "mask_coverage": geometry["coverage"],
        "pupil_to_iris_ratio": geometry["pupil_to_iris_ratio"],
        "iris_radius_fraction": geometry["iris_radius_fraction"],
        "center_offset_fraction": geometry["center_offset_fraction"],
        "before_luma_std": before_stats.get("luma_std", 0.0),
        "after_luma_std": after_stats.get("luma_std", 0.0),
        "contrast_gain": contrast_gain,
        "before_edge_score": before_stats.get("edge_score", 0.0),
        "after_edge_score": after_stats.get("edge_score", 0.0),
        "edge_gain": edge_gain,
        "passed": not flags,
        "flags": "|".join(flags),
    }


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def _session_id(root: Path, session: Path) -> str:
    try:
        relative = session.relative_to(root)
        parts = relative.parts or (root.name,)
    except ValueError:
        parts = (session.name,)
    clean = [part.replace(" ", "_").replace(".", "_") for part in parts]
    return "__".join(clean)


def _mask_geometry(mask: dict[str, Any], image_shape: tuple[int, int] | None) -> dict[str, float]:
    radius = float(mask.get("radius") or 0.0)
    pupil_radius = float(mask.get("pupil_radius") or 0.0)
    coverage = float(mask.get("coverage") or 0.0)
    if not image_shape:
        return {
            "coverage": coverage,
            "pupil_to_iris_ratio": _safe_ratio(pupil_radius, radius),
            "iris_radius_fraction": 0.0,
            "center_offset_fraction": 0.0,
        }
    h, w = image_shape
    center = mask.get("center") or [w / 2.0, h / 2.0]
    cx, cy = float(center[0]), float(center[1])
    offset = math.hypot(cx - w / 2.0, cy - h / 2.0) / max(1.0, min(h, w))
    return {
        "coverage": coverage,
        "pupil_to_iris_ratio": _safe_ratio(pupil_radius, radius),
        "iris_radius_fraction": radius / max(1.0, min(h, w)),
        "center_offset_fraction": offset,
    }


def _image_stats(image_path: str | Path | None, mask_path: str | Path | None) -> dict[str, float]:
    if not image_path or not Path(image_path).exists():
        return {}
    cv2, np = _cv2_np()
    image = load_image_float(image_path)
    gray = cv2.cvtColor(_to_uint8(image), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    mask = _load_mask(mask_path, gray.shape)
    if mask is None or not np.any(mask):
        mask = np.ones(gray.shape, dtype=bool)
    values = gray[mask]
    bbox = _mask_bbox(mask)
    roi = gray[bbox[0] : bbox[1], bbox[2] : bbox[3]]
    return {
        "luma_mean": float(np.mean(values)),
        "luma_std": float(np.std(values)),
        "luma_p01": float(np.percentile(values, 1)),
        "luma_p99": float(np.percentile(values, 99)),
        "edge_score": float(cv2.Laplacian(_to_uint8(roi), cv2.CV_64F).var()) if roi.size else 0.0,
    }


def _read_image_shape(image_path: str | Path | None) -> tuple[int, int] | None:
    if not image_path or not Path(image_path).exists():
        return None
    image = load_image_float(image_path)
    return int(image.shape[0]), int(image.shape[1])


def _load_mask(mask_path: str | Path | None, shape: tuple[int, int]):
    if not mask_path or not Path(mask_path).exists():
        return None
    cv2, np = _cv2_np()
    from PIL import Image

    mask = np.asarray(Image.open(mask_path).convert("L")) > 0
    if mask.shape != shape:
        mask = cv2.resize(mask.astype(np.uint8), (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST) > 0
    return mask


def _mask_bbox(mask) -> tuple[int, int, int, int]:
    _, np = _cv2_np()
    ys, xs = np.where(mask)
    if len(xs) == 0 or len(ys) == 0:
        return (0, mask.shape[0], 0, mask.shape[1])
    return max(0, int(ys.min())), min(mask.shape[0], int(ys.max()) + 1), max(0, int(xs.min())), min(
        mask.shape[1], int(xs.max()) + 1
    )


def _quality_flags(
    geometry: dict[str, float],
    kept_count: int,
    stack_count: int,
    min_frames: int,
    focus_median: float,
    clip_max: float,
    alignment_median: float,
    contrast_gain: float,
    edge_gain: float,
) -> list[str]:
    flags: list[str] = []
    if kept_count < min_frames:
        flags.append("too_few_kept_frames")
    if stack_count < min_frames:
        flags.append("too_few_aligned_frames")
    if not 0.06 <= geometry["coverage"] <= 0.48:
        flags.append("mask_coverage_out_of_range")
    if not 0.18 <= geometry["pupil_to_iris_ratio"] <= 0.68:
        flags.append("pupil_iris_ratio_out_of_range")
    if not 0.16 <= geometry["iris_radius_fraction"] <= 0.55:
        flags.append("iris_radius_out_of_range")
    if geometry["center_offset_fraction"] > 0.28:
        flags.append("iris_center_far_from_frame_center")
    if focus_median < 10.0:
        flags.append("low_focus")
    if clip_max > 0.35:
        flags.append("heavy_clipping")
    if alignment_median < 0.55:
        flags.append("weak_alignment")
    if edge_gain > 7.0 or (edge_gain > 5.5 and contrast_gain > 3.0):
        flags.append("possible_oversharpening")
    return flags


def _summarize_run(
    root: Path,
    output_dir: Path,
    settings: ProcessingSettings,
    sessions: list[Path],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    passed_rows = [row for row in ok_rows if row.get("passed") is True]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_root": str(root),
        "output_dir": str(output_dir),
        "settings": asdict(settings),
        "session_count": len(sessions),
        "processed_count": len(ok_rows),
        "passed_count": len(passed_rows),
        "failed_count": len(rows) - len(ok_rows),
        "flag_counts": _flag_counts(rows),
        "metric_medians": _metric_medians(ok_rows),
        "sessions": rows,
    }


def _flag_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        flags = str(row.get("flags", "")).split("|")
        for flag in flags:
            if not flag:
                continue
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items()))


def _metric_medians(rows: list[dict[str, Any]]) -> dict[str, float]:
    keys = [
        "focus_median",
        "clip_max",
        "alignment_median",
        "mask_coverage",
        "pupil_to_iris_ratio",
        "contrast_gain",
        "edge_gain",
    ]
    result: dict[str, float] = {}
    for key in keys:
        values = [float(row[key]) for row in rows if key in row and row[key] not in ("", None)]
        result[key] = float(median(values)) if values else 0.0
    return result


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "session_id",
        "status",
        "passed",
        "flags",
        "input_count",
        "kept_count",
        "stack_count",
        "rejected_count",
        "focus_median",
        "clip_max",
        "alignment_median",
        "mask_method",
        "mask_coverage",
        "pupil_to_iris_ratio",
        "iris_radius_fraction",
        "center_offset_fraction",
        "contrast_gain",
        "edge_gain",
        "report_json",
        "session_dir",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if abs(denominator) < 1e-12:
        return 0.0
    return float(numerator / denominator)


def _to_uint8(image):
    _, np = _cv2_np()
    return np.clip(image * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _cv2_np():
    try:
        import cv2
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("Dataset evaluation requires numpy and opencv-contrib-python.") from exc
    return cv2, np
