from datetime import datetime

from iriscope.capture import build_rpicam_command, make_session_name
from iriscope.config import CaptureSettings, parse_awb_gains


def test_build_rpicam_command_matches_capture_contract():
    settings = CaptureSettings(
        count=12,
        shutter_us=8000,
        gain=1,
        awb="manual",
        awb_gains=(1.8, 1.4),
        denoise="off",
        quality=95,
    )

    command = build_rpicam_command("frame_0001.jpg", settings, "frame_0001.json")

    assert command[:4] == ["rpicam-still", "--raw", "--immediate", "--nopreview"]
    assert "--shutter" in command
    assert command[command.index("--shutter") + 1] == "8000"
    assert command[command.index("--gain") + 1] == "1"
    assert command[command.index("--awbgains") + 1] == "1.8,1.4"
    assert command[command.index("--denoise") + 1] == "off"
    assert command[command.index("--metadata") + 1] == "frame_0001.json"
    assert command[-2:] == ["-o", "frame_0001.jpg"]


def test_auto_exposure_and_awb_are_not_forced():
    command = build_rpicam_command("frame_0001.jpg", CaptureSettings(shutter_us=0, gain=0, awb="auto"))

    assert "--shutter" not in command
    assert "--gain" not in command
    assert "--awbgains" not in command
    assert command[command.index("--awb") + 1] == "auto"


def test_camera_tuning_and_colour_controls_are_passed_to_rpicam():
    settings = CaptureSettings(
        tuning_file="/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json",
        metering="spot",
        ev=0.7,
        saturation=0.8,
    )

    command = build_rpicam_command("frame_0001.jpg", settings)

    assert command[command.index("--tuning-file") + 1].endswith("imx477_scientific.json")
    assert command[command.index("--metering") + 1] == "spot"
    assert command[command.index("--ev") + 1] == "0.7"
    assert command[command.index("--saturation") + 1] == "0.8"


def test_session_name_is_stable_and_safe():
    name = make_session_name("Subject 001 / trial", "left", datetime(2026, 6, 16, 15, 30, 5))

    assert name == "Subject_001_trial_left_20260616_153005"


def test_parse_awb_gains_accepts_config_and_cli_shapes():
    assert parse_awb_gains([1.8, 1.4]) == (1.8, 1.4)
    assert parse_awb_gains("1.9,1.3") == (1.9, 1.3)
