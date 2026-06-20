import json
from pathlib import Path

import numpy as np
from PIL import Image

from iriscope.config import ProcessingSettings
from iriscope.processing import process_session


def test_process_session_with_synthetic_iris_stack(tmp_path: Path):
    _write_synthetic_stack(tmp_path, count=6)

    result = process_session(
        tmp_path,
        settings=ProcessingSettings(min_frames=3, max_working_edge=180),
    )

    assert result.enhanced_jpg.exists()
    assert result.enhanced_tif.exists()
    assert result.contact_sheet.exists()
    assert result.report_json.exists()

    report = json.loads(result.report_json.read_text(encoding="utf-8"))
    assert len(report["kept_indices"]) >= 3
    assert report["mask"]["coverage"] > 0.05
    assert report["quality_status"] in {"pass", "review", "requires_recapture"}
    assert isinstance(report["requires_recapture"], bool)
    assert "quality_reasons" in report
    assert "forced_keep_indices" in report
    assert report["settings"]["quality"]["max_clip_fraction"] == 0.2
    assert report["settings"]["quality"]["min_alignment_score"] == 0.55
    assert report["outputs"]["enhanced_jpg"].endswith("enhanced.jpg")


def _write_synthetic_stack(root: Path, count: int) -> None:
    import cv2

    base = _synthetic_iris(220)
    rng = np.random.default_rng(42)
    shifts = [(-1, 0), (0, 0), (1, 0), (0, -1), (0, 1), (1, 1)]
    for index in range(count):
        dx, dy = shifts[index % len(shifts)]
        matrix = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float32)
        shifted = cv2.warpAffine(base, matrix, (base.shape[1], base.shape[0]), borderMode=cv2.BORDER_REFLECT)
        noise = rng.normal(0, 3.0, shifted.shape)
        frame = np.clip(shifted.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        Image.fromarray(frame).save(root / f"frame_{index + 1:04d}.png")


def _synthetic_iris(size: int) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size]
    cx = cy = size / 2
    radius = size * 0.39
    pupil = size * 0.105
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    angle = np.arctan2(yy - cy, xx - cx)

    image = np.zeros((size, size, 3), dtype=np.float32)
    image[:] = np.array([170, 145, 130], dtype=np.float32)

    sclera = dist < size * 0.47
    image[sclera] = np.array([214, 209, 198], dtype=np.float32)

    iris = dist < radius
    radial = 0.5 + 0.5 * np.sin(angle * 18 + dist * 0.22)
    rings = 0.5 + 0.5 * np.sin(dist * 0.34)
    texture = 0.65 * radial + 0.35 * rings
    image[:, :, 0][iris] = 62 + 42 * texture[iris]
    image[:, :, 1][iris] = 100 + 85 * texture[iris]
    image[:, :, 2][iris] = 88 + 48 * texture[iris]

    pupil_mask = dist < pupil
    image[pupil_mask] = np.array([8, 7, 6], dtype=np.float32)

    highlight = ((xx - size * 0.62) ** 2 + (yy - size * 0.38) ** 2) < (size * 0.035) ** 2
    image[highlight] = np.array([245, 245, 238], dtype=np.float32)
    return np.clip(image, 0, 255).astype(np.uint8)
