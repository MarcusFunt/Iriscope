from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import ProcessingSettings


IMAGE_SUFFIXES = {".dng", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}
CALIBRATION_PREFIXES = ("dark", "flat", "white", "color")


@dataclass(frozen=True)
class ProcessResult:
    output_dir: Path
    enhanced_jpg: Path
    enhanced_tif: Path
    report_json: Path
    contact_sheet: Path


def process_session(
    session_dir: str | Path,
    output_dir: str | Path | None = None,
    settings: ProcessingSettings | None = None,
    dark_path: str | Path | None = None,
    flat_path: str | Path | None = None,
) -> ProcessResult:
    cv2, np = _cv2_np()
    session_path = Path(session_dir)
    if not session_path.exists():
        raise FileNotFoundError(f"Session directory does not exist: {session_path}")
    settings = settings or ProcessingSettings()
    out = Path(output_dir) if output_dir else session_path / "processed"
    out.mkdir(parents=True, exist_ok=True)

    inputs = find_input_images(session_path)
    if not inputs:
        raise FileNotFoundError(f"No input images found in {session_path}")

    dark_source = Path(dark_path) if dark_path else _auto_find(session_path, "dark")
    flat_source = Path(flat_path) if flat_path else _auto_find(session_path, "flat")
    dark = _load_optional_calibration(dark_source, settings)
    flat = _load_optional_calibration(flat_source, settings)

    frames: list[Any] = []
    frame_metrics: list[dict[str, Any]] = []
    for path in inputs:
        image = load_image_float(path)
        image = _resize_for_settings(image, settings)
        image = apply_calibration(image, dark, flat)
        image = clean_bad_pixels(image)
        metrics = quality_metrics(image)
        metrics["file"] = path.name
        frame_metrics.append(metrics)
        frames.append(image)

    frames = _crop_to_common_size(frames)
    kept_indices, rejection_reasons = select_frames(frame_metrics, settings.min_frames)
    if not kept_indices:
        raise RuntimeError("All frames were rejected; inspect report data or lower quality thresholds.")

    kept_frames = [frames[index] for index in kept_indices]
    reference_local_index = max(
        range(len(kept_indices)),
        key=lambda local_index: frame_metrics[kept_indices[local_index]]["focus_score"],
    )
    reference = kept_frames[reference_local_index]

    aligned_frames, alignment_report = align_frames(kept_frames, reference)
    aligned_frames, alignment_report, stacked_local_indices = select_aligned_frames(
        aligned_frames,
        alignment_report,
        settings.min_frames,
    )
    stacked = stack_frames(aligned_frames, method=settings.stack_method, sigma=settings.sigma)
    iris_mask, mask_report = detect_iris_mask(stacked)
    enhanced = enhance_iris(stacked, iris_mask)

    stacked_tif = out / "stacked.tif"
    mask_png = out / "iris_mask.png"
    enhanced_tif = out / "enhanced.tif"
    enhanced_jpg = out / "enhanced.jpg"
    contact_sheet = out / "contact_sheet.jpg"
    report_json = out / "report.json"

    if settings.save_intermediates:
        write_tiff(stacked_tif, stacked)
        write_mask(mask_png, iris_mask)
    write_tiff(enhanced_tif, enhanced)
    write_jpg(enhanced_jpg, enhanced, quality=96)
    create_contact_sheet(contact_sheet, frames, stacked, enhanced, iris_mask, kept_indices)

    report = {
        "version": 1,
        "session": str(session_path),
        "inputs": [path.name for path in inputs],
        "settings": asdict(settings),
        "calibration": {
            "dark": str(dark_source) if dark_source else None,
            "flat": str(flat_source) if flat_source else None,
        },
        "frames": frame_metrics,
        "kept_indices": kept_indices,
        "stacked_input_indices": [kept_indices[index] for index in stacked_local_indices],
        "rejection_reasons": rejection_reasons,
        "reference_input": inputs[kept_indices[reference_local_index]].name,
        "alignment": alignment_report,
        "mask": mask_report,
        "outputs": {
            "enhanced_tif": str(enhanced_tif),
            "enhanced_jpg": str(enhanced_jpg),
            "stacked_tif": str(stacked_tif) if settings.save_intermediates else None,
            "iris_mask": str(mask_png) if settings.save_intermediates else None,
            "contact_sheet": str(contact_sheet),
        },
    }
    report_json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    return ProcessResult(out, enhanced_jpg, enhanced_tif, report_json, contact_sheet)


def find_input_images(session_dir: str | Path) -> list[Path]:
    root = Path(session_dir)
    images: list[Path] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        stem = path.stem.lower()
        if stem.startswith(CALIBRATION_PREFIXES):
            continue
        images.append(path)
    dngs = [path for path in images if path.suffix.lower() == ".dng"]
    return dngs or images


def load_image_float(path: str | Path):
    cv2, np = _cv2_np()
    image_path = Path(path)
    if image_path.suffix.lower() == ".dng":
        try:
            import rawpy
        except ModuleNotFoundError as exc:
            raise RuntimeError("DNG processing requires rawpy. Install with `pip install -e .`.") from exc
        with rawpy.imread(str(image_path)) as raw:
            rgb = raw.postprocess(
                output_bps=16,
                no_auto_bright=True,
                use_camera_wb=True,
                user_flip=0,
            )
        return np.clip(rgb.astype(np.float32) / 65535.0, 0.0, 1.0)

    try:
        import imageio.v3 as iio

        arr = iio.imread(image_path)
    except Exception:
        from PIL import Image

        arr = np.asarray(Image.open(image_path))

    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    if arr.shape[2] > 3:
        arr = arr[:, :, :3]
    if arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    if arr.dtype == np.uint16:
        return arr.astype(np.float32) / 65535.0
    arr = arr.astype(np.float32)
    max_value = float(np.nanmax(arr)) if arr.size else 1.0
    if max_value > 1.0:
        arr = arr / max_value
    return np.clip(arr, 0.0, 1.0)


def apply_calibration(image, dark=None, flat=None):
    cv2, np = _cv2_np()
    calibrated = image.astype(np.float32, copy=True)
    if dark is not None:
        calibrated = np.clip(calibrated - _match_shape(dark, calibrated.shape[:2]), 0.0, 1.0)
    if flat is not None:
        flat_matched = _match_shape(flat, calibrated.shape[:2])
        if dark is not None:
            flat_matched = np.clip(flat_matched - _match_shape(dark, flat_matched.shape[:2]), 0.0, 1.0)
        luminance = cv2.cvtColor(_to_uint8(flat_matched), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        valid = luminance > 0.03
        if np.any(valid):
            scale = np.median(luminance[valid])
            correction = np.clip(luminance / max(scale, 1e-6), 0.2, 5.0)
            calibrated = calibrated / correction[:, :, None]
    return np.clip(calibrated, 0.0, 1.0)


def clean_bad_pixels(image):
    cv2, np = _cv2_np()
    median = np.empty_like(image)
    for channel in range(3):
        median[:, :, channel] = cv2.medianBlur(image[:, :, channel], 3)
    diff = np.abs(image - median)
    channel_mad = np.median(diff.reshape(-1, 3), axis=0)
    threshold = np.maximum(0.08, channel_mad * 8.0)
    mask = diff > threshold.reshape(1, 1, 3)
    cleaned = image.copy()
    cleaned[mask] = median[mask]
    return cleaned


def quality_metrics(image) -> dict[str, float]:
    cv2, np = _cv2_np()
    gray = cv2.cvtColor(_to_uint8(image), cv2.COLOR_RGB2GRAY)
    focus_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    luma = gray.astype(np.float32) / 255.0
    return {
        "focus_score": focus_score,
        "mean_luma": float(np.mean(luma)),
        "clip_fraction": float(np.mean((luma <= 0.002) | (luma >= 0.998))),
    }


def select_frames(metrics: list[dict[str, Any]], min_frames: int = 3) -> tuple[list[int], dict[str, str]]:
    if not metrics:
        return [], {}
    focus_values = [float(item["focus_score"]) for item in metrics]
    median_focus = _median(focus_values)
    kept: list[int] = []
    reasons: dict[str, str] = {}
    for index, item in enumerate(metrics):
        reason = ""
        if float(item["clip_fraction"]) > 0.20:
            reason = "too much clipping"
        elif float(item["focus_score"]) < max(1e-9, median_focus * 0.35):
            reason = "low focus score"
        elif not 0.02 <= float(item["mean_luma"]) <= 0.98:
            reason = "mean luminance out of range"
        if reason:
            reasons[str(index)] = reason
        else:
            kept.append(index)

    if len(kept) < min_frames:
        ranked = sorted(
            range(len(metrics)),
            key=lambda index: (
                float(metrics[index]["clip_fraction"]) > 0.35,
                -float(metrics[index]["focus_score"]),
            ),
        )
        kept = sorted(ranked[: min(min_frames, len(metrics))])
        reasons = {
            str(index): reason
            for index, reason in reasons.items()
            if int(index) not in kept
        }
    return kept, reasons


def align_frames(frames: list[Any], reference) -> tuple[list[Any], list[dict[str, Any]]]:
    aligned = []
    report = []
    for index, frame in enumerate(frames):
        if frame is reference:
            aligned.append(frame)
            report.append({"index": index, "method": "reference", "score": 1.0})
            continue
        warped, info = align_to_reference(frame, reference)
        info["index"] = index
        aligned.append(warped)
        report.append(info)
    return aligned, report


def select_aligned_frames(
    frames: list[Any],
    report: list[dict[str, Any]],
    min_frames: int = 3,
    min_score: float = 0.75,
) -> tuple[list[Any], list[dict[str, Any]], list[int]]:
    if len(frames) <= max(1, min_frames):
        for item in report:
            item["used_for_stack"] = True
        return frames, report, list(range(len(frames)))

    keep = [
        index
        for index, item in enumerate(report)
        if item.get("method") == "reference" or float(item.get("score", 0.0)) >= min_score
    ]
    if len(keep) < min_frames:
        ranked = sorted(
            range(len(report)),
            key=lambda index: (
                report[index].get("method") != "reference",
                -float(report[index].get("score", 0.0)),
            ),
        )
        keep = sorted(ranked[: min(min_frames, len(frames))])
    keep_set = set(keep)
    for index, item in enumerate(report):
        item["used_for_stack"] = index in keep_set
        if index not in keep_set:
            item["rejection_reason"] = "weak alignment score"
    return [frames[index] for index in keep], report, keep


def align_to_reference(moving, reference):
    cv2, np = _cv2_np()
    ref_gray = _gray_float(reference)
    mov_gray = _gray_float(moving)
    max_edge = max(ref_gray.shape)
    scale = min(1.0, 1200.0 / max_edge)
    if scale < 1.0:
        size = (max(1, int(ref_gray.shape[1] * scale)), max(1, int(ref_gray.shape[0] * scale)))
        ref_small = cv2.resize(ref_gray, size, interpolation=cv2.INTER_AREA)
        mov_small = cv2.resize(mov_gray, size, interpolation=cv2.INTER_AREA)
    else:
        ref_small = ref_gray
        mov_small = mov_gray

    ref_small = cv2.GaussianBlur(ref_small, (0, 0), 1.0)
    mov_small = cv2.GaussianBlur(mov_small, (0, 0), 1.0)
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5)
    try:
        score, warp = cv2.findTransformECC(
            ref_small,
            mov_small,
            warp,
            cv2.MOTION_EUCLIDEAN,
            criteria,
            None,
            3,
        )
        if scale < 1.0:
            warp[0, 2] /= scale
            warp[1, 2] /= scale
        warped = cv2.warpAffine(
            moving,
            warp,
            (reference.shape[1], reference.shape[0]),
            flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REFLECT,
        )
        return np.clip(warped, 0.0, 1.0), {
            "method": "ecc_euclidean",
            "score": float(score),
            "matrix": warp.tolist(),
        }
    except cv2.error as exc:
        return moving, {"method": "none", "score": 0.0, "error": str(exc).splitlines()[0]}


def stack_frames(frames: list[Any], method: str = "sigma", sigma: float = 2.5):
    cv2, np = _cv2_np()
    if not frames:
        raise ValueError("Cannot stack an empty frame list.")
    stack = np.stack(frames, axis=0).astype(np.float32)
    method_value = method.lower()
    if method_value == "median":
        return np.median(stack, axis=0).astype(np.float32)
    if method_value == "mean" or len(frames) < 3:
        return np.mean(stack, axis=0).astype(np.float32)
    if method_value != "sigma":
        raise ValueError("Stack method must be one of: sigma, median, mean.")
    median = np.median(stack, axis=0)
    std = np.std(stack, axis=0)
    keep = np.abs(stack - median) <= (float(sigma) * std + 1e-6)
    weighted = np.where(keep, stack, 0.0)
    counts = np.maximum(np.sum(keep, axis=0), 1)
    result = np.sum(weighted, axis=0) / counts
    result = np.where(np.sum(keep, axis=0) == 0, median, result)
    return np.clip(result.astype(np.float32), 0.0, 1.0)


def detect_iris_mask(image) -> tuple[Any, dict[str, Any]]:
    cv2, np = _cv2_np()
    h, w = image.shape[:2]
    min_dim = min(h, w)
    gray = cv2.cvtColor(_to_uint8(image), cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (0, 0), 1.2)

    pupil = _find_pupil(gray)
    if pupil:
        center_x, center_y, pupil_radius = pupil
    else:
        center_x, center_y, pupil_radius = w / 2.0, h / 2.0, min_dim * 0.08

    iris = _find_outer_iris_radial(gray, (center_x, center_y), pupil_radius)
    if iris is None:
        iris = _find_outer_iris(gray, (center_x, center_y), pupil_radius)
    if iris:
        iris_x, iris_y, iris_radius = iris
        method = "radial_or_hough_circle"
    else:
        iris_x, iris_y = center_x, center_y
        if pupil:
            iris_radius = min(min_dim * 0.38, max(pupil_radius * 2.05, min_dim * 0.20))
        else:
            iris_radius = min_dim * 0.32
        method = "fallback_circle"

    yy, xx = np.ogrid[:h, :w]
    outer = (xx - iris_x) ** 2 + (yy - iris_y) ** 2 <= iris_radius**2
    inner = (xx - center_x) ** 2 + (yy - center_y) ** 2 <= (pupil_radius * 1.05) ** 2
    mask = np.where(outer & ~inner, 255, 0).astype(np.uint8)
    report = {
        "method": method,
        "center": [float(iris_x), float(iris_y)],
        "radius": float(iris_radius),
        "pupil_center": [float(center_x), float(center_y)],
        "pupil_radius": float(pupil_radius),
        "coverage": float(np.mean(mask > 0)),
    }
    return mask, report


def enhance_iris(image, mask):
    cv2, np = _cv2_np()
    base = np.clip(image.astype(np.float32), 0.0, 1.0)
    mask_f = (mask.astype(np.float32) / 255.0)[:, :, None]
    mask_f = cv2.GaussianBlur(mask_f, (0, 0), max(3.0, min(mask.shape) * 0.015))
    if mask_f.ndim == 2:
        mask_f = mask_f[:, :, None]
    mask_f = np.clip(mask_f, 0.0, 1.0)

    toned = _percentile_stretch(base, mask > 0)
    work = base * (1.0 - mask_f * 0.35) + toned * (mask_f * 0.35)

    lab = cv2.cvtColor(_to_uint8(work), cv2.COLOR_RGB2LAB)
    clahe = cv2.createCLAHE(clipLimit=1.65, tileGridSize=(8, 8))
    l_channel = lab[:, :, 0]
    l_clahe = clahe.apply(l_channel)
    l_blended = l_channel.astype(np.float32) * (1.0 - mask_f[:, :, 0] * 0.55) + l_clahe.astype(np.float32) * (
        mask_f[:, :, 0] * 0.55
    )
    lab[:, :, 0] = np.clip(l_blended, 0, 255).astype(np.uint8)
    work = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB).astype(np.float32) / 255.0

    blur = cv2.GaussianBlur(work, (0, 0), 1.05)
    sharp = np.clip(work + 0.52 * (work - blur), 0.0, 1.0)
    work = work * (1.0 - mask_f * 0.68) + sharp * (mask_f * 0.68)

    hsv = cv2.cvtColor(_to_uint8(work), cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (1.0 + 0.12 * mask_f[:, :, 0]), 0, 255)
    hsv[:, :, 2] = np.clip(hsv[:, :, 2] * (1.0 + 0.03 * mask_f[:, :, 0]), 0, 255)
    work = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB).astype(np.float32) / 255.0
    work = _limit_masked_contrast(base, work, mask_f)
    return np.clip(work, 0.0, 1.0)


def _limit_masked_contrast(base, enhanced, mask_f):
    cv2, np = _cv2_np()
    mask = mask_f[:, :, 0] > 0.25
    if np.count_nonzero(mask) < 40:
        return enhanced
    base_gray = cv2.cvtColor(_to_uint8(base), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    enhanced_gray = cv2.cvtColor(_to_uint8(enhanced), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    before_std = float(np.std(base_gray[mask]))
    after_std = float(np.std(enhanced_gray[mask]))
    if before_std <= 1e-4 or after_std <= before_std:
        return enhanced
    target_std = min(0.16, max(before_std * 2.8, before_std + 0.055))
    if after_std <= target_std:
        return enhanced
    blend = np.clip((target_std - before_std) / max(after_std - before_std, 1e-6), 0.35, 0.95)
    local_backoff = mask_f * (1.0 - float(blend))
    return base * local_backoff + enhanced * (1.0 - local_backoff)


def write_tiff(path: str | Path, image) -> None:
    _, np = _cv2_np()
    import imageio.v3 as iio

    arr = np.clip(image * 65535.0 + 0.5, 0, 65535).astype(np.uint16)
    iio.imwrite(Path(path), arr)


def write_jpg(path: str | Path, image, quality: int = 95) -> None:
    from PIL import Image

    Image.fromarray(_to_uint8(image)).save(Path(path), quality=quality, subsampling=0)


def write_mask(path: str | Path, mask) -> None:
    from PIL import Image

    Image.fromarray(mask).save(Path(path))


def create_contact_sheet(
    path: str | Path,
    frames: list[Any],
    stacked,
    enhanced,
    mask,
    kept_indices: Iterable[int],
) -> None:
    cv2, np = _cv2_np()
    from PIL import Image, ImageDraw

    kept_set = set(kept_indices)
    items: list[tuple[str, Any]] = []
    if frames:
        items.append(("reference input", frames[min(kept_set) if kept_set else 0]))
    items.append(("stacked", stacked))
    items.append(("enhanced", enhanced))
    items.append(("iris mask", np.repeat((mask[:, :, None] / 255.0).astype(np.float32), 3, axis=2)))
    for index, frame in enumerate(frames[:8]):
        label = f"frame {index + 1}"
        if index not in kept_set:
            label += " rejected"
        items.append((label, frame))

    cell_w, cell_h = 320, 260
    label_h = 28
    cols = 3
    rows = math.ceil(len(items) / cols)
    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (28, 28, 28))
    draw = ImageDraw.Draw(canvas)
    for idx, (label, img) in enumerate(items):
        thumb = Image.fromarray(_to_uint8(img))
        thumb.thumbnail((cell_w, cell_h - label_h), Image.Resampling.LANCZOS)
        x = (idx % cols) * cell_w + (cell_w - thumb.width) // 2
        y = (idx // cols) * cell_h + label_h
        canvas.paste(thumb, (x, y))
        draw.text(((idx % cols) * cell_w + 8, (idx // cols) * cell_h + 7), label, fill=(235, 235, 235))
    canvas.save(Path(path), quality=92)


def _load_optional_calibration(path: str | Path | None, settings: ProcessingSettings):
    if path is None:
        return None
    image = load_image_float(path)
    return _resize_for_settings(image, settings)


def _auto_find(session_dir: Path, prefix: str) -> Path | None:
    matches = sorted(
        path
        for path in session_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and path.stem.lower().startswith(prefix)
    )
    return matches[0] if matches else None


def _resize_for_settings(image, settings: ProcessingSettings):
    cv2, np = _cv2_np()
    if not settings.max_working_edge:
        return image
    h, w = image.shape[:2]
    edge = max(h, w)
    if edge <= settings.max_working_edge:
        return image
    scale = settings.max_working_edge / float(edge)
    size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def _crop_to_common_size(frames: list[Any]) -> list[Any]:
    if not frames:
        return []
    heights = [frame.shape[0] for frame in frames]
    widths = [frame.shape[1] for frame in frames]
    target_h, target_w = min(heights), min(widths)
    cropped = []
    for frame in frames:
        y0 = max(0, (frame.shape[0] - target_h) // 2)
        x0 = max(0, (frame.shape[1] - target_w) // 2)
        cropped.append(frame[y0 : y0 + target_h, x0 : x0 + target_w])
    return cropped


def _match_shape(image, shape_hw: tuple[int, int]):
    cv2, np = _cv2_np()
    h, w = shape_hw
    if image.shape[:2] == (h, w):
        return image
    return cv2.resize(image, (w, h), interpolation=cv2.INTER_LINEAR)


def _find_pupil(gray):
    cv2, np = _cv2_np()
    h, w = gray.shape[:2]
    threshold = min(np.percentile(gray, 10), float(np.mean(gray) * 0.72))
    binary = (gray <= threshold).astype(np.uint8) * 255
    kernel = np.ones((5, 5), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_score = -1.0
    min_radius = min(h, w) * 0.045
    max_radius = min(h, w) * 0.30
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < (min(h, w) ** 2) * 0.0005 or area > (min(h, w) ** 2) * 0.14:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        circularity = 4.0 * math.pi * area / (perimeter * perimeter)
        if circularity < 0.52:
            continue
        (x, y), radius = cv2.minEnclosingCircle(contour)
        area_radius = math.sqrt(area / math.pi)
        if area_radius < min_radius or area_radius > max_radius:
            continue
        if radius > area_radius * 1.45:
            continue
        center_penalty = math.hypot((x - w / 2) / w, (y - h / 2) / h)
        if center_penalty > 0.42:
            continue
        contour_mask = np.zeros_like(binary)
        cv2.drawContours(contour_mask, [contour], -1, 255, thickness=-1)
        darkness = 255.0 - float(np.mean(gray[contour_mask > 0])) if np.any(contour_mask > 0) else 0.0
        score = (
            area
            * max(0.0, circularity) ** 2
            * (1.0 - min(center_penalty, 0.9))
            * max(1.0, darkness / 35.0)
        )
        if score > best_score:
            best_score = score
            robust_radius = max(area_radius, min(float(radius), area_radius * 1.12))
            best = (float(x), float(y), float(robust_radius))
    return best or _find_pupil_hough(gray)


def _find_pupil_hough(gray):
    cv2, np = _cv2_np()
    h, w = gray.shape[:2]
    min_dim = min(h, w)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(32, min_dim // 4),
        param1=80,
        param2=20,
        minRadius=max(8, int(min_dim * 0.07)),
        maxRadius=max(12, int(min_dim * 0.28)),
    )
    if circles is None:
        return None

    yy, xx = np.mgrid[0:h, 0:w]
    best = None
    best_score = -1.0
    for x, y, radius in circles[0]:
        x = float(x)
        y = float(y)
        radius = float(radius)
        if x - radius < 0 or y - radius < 0 or x + radius >= w or y + radius >= h:
            continue
        center_penalty = math.hypot((x - w / 2) / w, (y - h / 2) / h)
        if center_penalty > 0.34:
            continue
        dist = np.sqrt((xx - x) ** 2 + (yy - y) ** 2)
        inner = dist <= radius * 0.78
        ring = (dist >= radius * 1.18) & (dist <= radius * 1.65)
        if np.count_nonzero(inner) < 20 or np.count_nonzero(ring) < 40:
            continue
        contrast = float(np.mean(gray[ring]) - np.mean(gray[inner]))
        if contrast < 8.0:
            continue
        score = contrast * (1.0 - center_penalty) * math.sqrt(max(radius, 1.0))
        if score > best_score:
            best_score = score
            best = (x, y, radius)
    return best


def _find_outer_iris_radial(gray, pupil_center: tuple[float, float], pupil_radius: float):
    cv2, np = _cv2_np()
    h, w = gray.shape[:2]
    min_dim = min(h, w)
    cx, cy = pupil_center
    min_radius = max(pupil_radius * 1.65, min_dim * 0.16)
    max_radius = min(min_dim * 0.42, max(pupil_radius * 3.35, min_dim * 0.28))
    if min_radius >= max_radius:
        return None

    gray_f = cv2.GaussianBlur(gray.astype(np.float32) / 255.0, (0, 0), 1.4)
    radii = np.arange(int(min_radius), int(max_radius) + 1)
    if len(radii) < 8:
        return None
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    profile = []
    for radius in radii:
        ring = np.abs(dist - radius) <= 1.5
        valid = ring & (yy > cy - radius * 0.70) & (yy < cy + radius * 0.78)
        if np.count_nonzero(valid) < 20:
            profile.append(np.nan)
        else:
            profile.append(float(np.mean(gray_f[valid])))
    profile_arr = np.asarray(profile, dtype=np.float32)
    if np.count_nonzero(~np.isnan(profile_arr)) < 8:
        return None
    profile_arr = np.interp(
        np.arange(len(profile_arr)),
        np.flatnonzero(~np.isnan(profile_arr)),
        profile_arr[~np.isnan(profile_arr)],
    )
    profile_arr = cv2.GaussianBlur(profile_arr.reshape(1, -1), (1, 7), 0).reshape(-1)
    gradient = np.gradient(profile_arr)
    idx = int(np.argmax(gradient))
    radius = float(radii[idx])
    if gradient[idx] < 0.003:
        return None
    return (float(cx), float(cy), radius)


def _find_outer_iris(gray, pupil_center: tuple[float, float], pupil_radius: float):
    cv2, np = _cv2_np()
    h, w = gray.shape[:2]
    min_dim = min(h, w)
    min_radius = max(int(pupil_radius * 2.0), int(min_dim * 0.18))
    max_radius = int(min_dim * 0.52)
    if min_radius >= max_radius:
        return None
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.4,
        minDist=max(20, min_dim // 8),
        param1=90,
        param2=24,
        minRadius=min_radius,
        maxRadius=max_radius,
    )
    if circles is None:
        return None
    candidates = circles[0]
    px, py = pupil_center
    best = None
    best_score = float("inf")
    for x, y, radius in candidates:
        distance = math.hypot(float(x) - px, float(y) - py)
        score = distance + abs(float(radius) - pupil_radius * 3.0) * 0.2
        if score < best_score:
            best_score = score
            best = (float(x), float(y), float(radius))
    return best


def _percentile_stretch(image, mask):
    _, np = _cv2_np()
    stretched = image.copy()
    if not np.any(mask):
        return stretched
    pixels = image[mask]
    for channel in range(3):
        low, high = np.percentile(pixels[:, channel], [0.5, 99.5])
        if high - low > 1e-4:
            stretched[:, :, channel] = (image[:, :, channel] - low) / (high - low)
    return np.clip(stretched, 0.0, 1.0)


def _to_uint8(image):
    _, np = _cv2_np()
    return np.clip(image * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _gray_float(image):
    cv2, np = _cv2_np()
    return cv2.cvtColor(_to_uint8(image), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _cv2_np():
    try:
        import cv2
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("Processing requires numpy and opencv-contrib-python.") from exc
    return cv2, np
