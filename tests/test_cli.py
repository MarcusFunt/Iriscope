from pathlib import Path

from iriscope.cli import main


def test_init_config_writes_template(tmp_path: Path):
    output = tmp_path / ".iriscope.toml"

    code = main(["init-config", "--output", str(output)])

    assert code == 0
    assert "[pi]" in output.read_text(encoding="utf-8")


def test_calibrate_without_host_prints_local_instructions(tmp_path: Path):
    missing_config = tmp_path / "missing.toml"

    code = main(["--config", str(missing_config), "calibrate"])

    assert code == 0
