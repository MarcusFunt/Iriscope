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
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.pi.host == "pi.local"
    assert config.pi.user == "camera"
    assert config.capture.count == 16
    assert config.capture.awb == "manual"
    assert config.capture.awb_gains == (2.0, 1.2)
    assert config.preview.width == 800
    assert config.preview.height == 480
    assert config.preview.framerate == 10
    assert config.processing.stack_method == "median"
    assert config.processing.max_working_edge == 640
