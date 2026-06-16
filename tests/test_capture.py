from datetime import datetime

from iriscope.capture import build_rpicam_command, make_session_name
from iriscope.config import CaptureSettings, parse_awb_gains


def test_build_rpicam_command_matches_capture_contract():
    settings = CaptureSettings(
        count=12,
        shutter_us=8000,
        gain=1,
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


def test_session_name_is_stable_and_safe():
    name = make_session_name("Subject 001 / trial", "left", datetime(2026, 6, 16, 15, 30, 5))

    assert name == "Subject_001_trial_left_20260616_153005"


def test_parse_awb_gains_accepts_config_and_cli_shapes():
    assert parse_awb_gains([1.8, 1.4]) == (1.8, 1.4)
    assert parse_awb_gains("1.9,1.3") == (1.9, 1.3)
