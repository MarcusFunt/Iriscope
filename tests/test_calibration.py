from pathlib import Path

from iriscope.calibration import (
    bounded_run_dir,
    build_calibration_candidates,
    capture_settings_to_profile,
    parse_rpicam_metadata,
    score_candidate_metrics,
)
from iriscope.capture import build_rpicam_command
from iriscope.config import CalibrationSettings, CaptureSettings, QualityThresholds


def test_parse_rpicam_metadata_accepts_variant_keys():
    metadata = {
        "camera": {
            "ExposureTime": 8123,
            "AnalogueGain": 1.7,
            "ColourGains": [2.1, 1.4],
            "FocusFoM": 96,
        }
    }

    parsed = parse_rpicam_metadata(metadata)

    assert parsed.exposure_us == 8123
    assert parsed.analogue_gain == 1.7
    assert parsed.awb_gains == (2.1, 1.4)
    assert parsed.focus_fom == 96
    assert parsed.confidence == 1.0


def test_parse_rpicam_metadata_handles_missing_values():
    parsed = parse_rpicam_metadata({"exposure_time": 0.008})

    assert parsed.exposure_us == 8000
    assert parsed.analogue_gain is None
    assert parsed.awb_gains is None
    assert parsed.confidence == 0.25


def test_calibration_candidates_generate_rpicam_metadata_commands():
    settings = CalibrationSettings(sample_budget=4)
    candidates = build_calibration_candidates(
        CaptureSettings(shutter_us=0, gain=0, awb="auto"),
        settings,
        parse_rpicam_metadata({"ExposureTime": 7000, "AnalogueGain": 1.5, "ColourGains": [2.2, 1.3]}),
    )

    command = build_rpicam_command("candidate.jpg", candidates[1].settings, "candidate.json")

    assert candidates[0].phase == "auto_baseline"
    assert len(candidates) == 4
    assert "--metadata" in command
    assert command[command.index("--metadata-format") + 1] == "json"
    assert "--raw" not in command


def test_candidate_scoring_prefers_balanced_low_clip_low_gain_frame():
    calibration = CalibrationSettings()
    thresholds = QualityThresholds()
    candidates = [
        {
            "candidate_id": "bad",
            "label": "bad",
            "settings": capture_settings_to_profile(CaptureSettings(gain=6, shutter_us=12000)),
            "mean_luma": 0.8,
            "clip_fraction": 0.2,
            "focus_score": 40,
            "mask_coverage": 0.2,
            "mask_method": "radial_or_hough_circle",
            "channel_balance": 0.7,
            "metadata_confidence": 1.0,
        },
        {
            "candidate_id": "good",
            "label": "good",
            "settings": capture_settings_to_profile(CaptureSettings(gain=1.4, shutter_us=10000)),
            "mean_luma": 0.48,
            "clip_fraction": 0.005,
            "focus_score": 42,
            "mask_coverage": 0.24,
            "mask_method": "radial_or_hough_circle",
            "channel_balance": 0.95,
            "metadata_confidence": 0.75,
        },
    ]

    scored = score_candidate_metrics(candidates, calibration, thresholds)

    assert scored[1]["candidate_id"] == "good"
    assert scored[1]["score"] > scored[0]["score"]
    assert scored[1]["component_scores"]["clipping"] == 1.0


def test_bounded_run_dir_rejects_path_escape(tmp_path: Path):
    root = tmp_path / "calibration"
    root.mkdir()

    run_dir = bounded_run_dir(root, "cal_20260621")

    assert run_dir == root / "cal_20260621"
