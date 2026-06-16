from pathlib import Path

import numpy as np
from PIL import Image

from iriscope.evaluation import discover_dataset_sessions
from iriscope.processing import select_aligned_frames


def test_discover_dataset_sessions_finds_leaf_image_folders(tmp_path: Path):
    _write_tiny_images(tmp_path / "001" / "L", 3)
    _write_tiny_images(tmp_path / "001" / "R", 2)

    sessions = discover_dataset_sessions(tmp_path, min_images=3)

    assert sessions == [tmp_path / "001" / "L"]


def test_select_aligned_frames_rejects_weak_alignment_when_enough_frames_remain():
    frames = [np.zeros((4, 4, 3), dtype=np.float32) + index for index in range(4)]
    report = [
        {"index": 0, "method": "ecc_euclidean", "score": 0.61},
        {"index": 1, "method": "ecc_euclidean", "score": 0.94},
        {"index": 2, "method": "ecc_euclidean", "score": 0.88},
        {"index": 3, "method": "reference", "score": 1.0},
    ]

    selected, updated_report, local_indices = select_aligned_frames(frames, report, min_frames=3)

    assert local_indices == [1, 2, 3]
    assert len(selected) == 3
    assert updated_report[0]["used_for_stack"] is False
    assert updated_report[0]["rejection_reason"] == "weak alignment score"
    assert updated_report[3]["used_for_stack"] is True


def _write_tiny_images(root: Path, count: int) -> None:
    root.mkdir(parents=True)
    for index in range(count):
        image = np.full((12, 12, 3), 90 + index, dtype=np.uint8)
        Image.fromarray(image).save(root / f"frame_{index}.png")
