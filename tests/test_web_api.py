from iriscope.web_api import _capture_dict, _serial_ports
from iriscope.config import CaptureSettings


def test_capture_dict_includes_command_preview():
    data = _capture_dict(CaptureSettings())

    assert data["awb_gains"] == [1.8, 1.4]
    assert "rpicam-still" in data["command_preview"]
    assert "--raw" in data["command_preview"]


def test_serial_ports_returns_list():
    assert isinstance(_serial_ports(), list)
