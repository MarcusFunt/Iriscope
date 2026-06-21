from pathlib import Path

from iriscope.config import load_config


def test_load_config_merges_expected_sections(tmp_path: Path):
    config_path = tmp_path / ".iriscope.toml"
    config_path.write_text(
        """
[pi]
host = "pi.local"
user = "camera"

[capture]
count = 16
awb_gains = [2.0, 1.2]

[preview]
width = 800
framerate = 10

[processing]
stack_method = "median"
max_working_edge = 640

[calibration]
target_luma_min = 0.40
target_luma_max = 0.62
sample_budget = 8

[calibration.weights]
focus = 0.22
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.pi.host == "pi.local"
    assert config.pi.user == "camera"
    assert config.capture.count == 16
    assert config.capture.awb == "manual"
    assert config.capture.awb_gains == (2.0, 1.2)
    assert config.capture.denoise == "cdn_fast"
    assert config.preview.width == 800
    assert config.preview.height == 480
    assert config.preview.framerate == 10
    assert config.processing.stack_method == "median"
    assert config.processing.max_working_edge == 640
    assert config.processing.quality.max_clip_fraction == 0.2
    assert config.calibration.target_luma_min == 0.40
    assert config.calibration.target_luma_max == 0.62
    assert config.calibration.sample_budget == 8
    assert config.calibration.weights.focus == 0.22


def test_load_config_parses_quality_threshold_overrides(tmp_path: Path):
    config_path = tmp_path / ".iriscope.toml"
    config_path.write_text(
        """
[processing.quality]
max_clip_fraction = 0.18
min_relative_focus = 0.4
min_mask_coverage = 0.07
max_eval_clip_fraction = 0.32
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.processing.quality.max_clip_fraction == 0.18
    assert config.processing.quality.min_relative_focus == 0.4
    assert config.processing.quality.min_mask_coverage == 0.07
    assert config.processing.quality.max_eval_clip_fraction == 0.32


def test_load_config_allows_pi_environment_overrides(tmp_path: Path, monkeypatch):
    config_path = tmp_path / ".iriscope.toml"
    config_path.write_text(
        """
[pi]
host = "pi.local"
user = "camera"
port = 22
remote_root = "/home/camera/iriscope"
ssh_key = "C:/keys/test_rsa"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("IRISCOPE_PI_HOST", "10.42.0.2")
    monkeypatch.setenv("IRISCOPE_PI_USER", "Iriscope")
    monkeypatch.setenv("IRISCOPE_PI_PORT", "2222")
    monkeypatch.setenv("IRISCOPE_PI_REMOTE_ROOT", "/home/Iriscope/iriscope")
    monkeypatch.setenv("IRISCOPE_PI_SSH_KEY", "/run/secrets/iriscope_ssh_key")
    monkeypatch.setenv("IRISCOPE_PI_CONNECT_TIMEOUT", "7")

    config = load_config(config_path)

    assert config.pi.host == "10.42.0.2"
    assert config.pi.user == "Iriscope"
    assert config.pi.port == 2222
    assert config.pi.remote_root == "/home/Iriscope/iriscope"
    assert config.pi.ssh_key == "/run/secrets/iriscope_ssh_key"
    assert config.pi.connect_timeout == 7


def test_blank_pi_environment_values_do_not_override_non_nullable_fields(tmp_path: Path, monkeypatch):
    config_path = tmp_path / ".iriscope.toml"
    config_path.write_text(
        """
[pi]
host = "pi.local"
user = "camera"
port = 22
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("IRISCOPE_PI_USER", "")
    monkeypatch.setenv("IRISCOPE_PI_PORT", "")
    monkeypatch.setenv("IRISCOPE_PI_HOST", "")

    config = load_config(config_path)

    assert config.pi.host is None
    assert config.pi.user == "camera"
    assert config.pi.port == 22
