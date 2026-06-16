from pathlib import Path

from PIL import Image

from iriscope.labeling import LABEL_FILE, load_label, save_label


def test_save_and_load_label(tmp_path: Path):
    saved = save_label(
        tmp_path,
        {
            "subject_code": "S001",
            "eye": "left",
            "consent_recorded": True,
            "tags": ["macro", "test"],
        },
    )

    loaded = load_label(tmp_path)

    assert (tmp_path / LABEL_FILE).exists()
    assert saved["subject_code"] == "S001"
    assert loaded["eye"] == "left"
    assert loaded["exclude_from_training"] is True


def test_default_label_for_unlabeled_session(tmp_path: Path):
    label = load_label(tmp_path)

    assert label["allowed_use"] == "local_enhancement_only"
    assert label["consent_recorded"] is False
