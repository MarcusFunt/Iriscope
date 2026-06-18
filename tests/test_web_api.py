from iriscope.web_api import _capture_dict, _preview_dict, _serial_ports
from iriscope.config import CaptureSettings, PreviewSettings


def test_capture_dict_includes_command_preview():
    data = _capture_dict(CaptureSettings())

    assert data["awb"] == "auto"
    assert data["awb_gains"] == [3.2, 1.4]
    assert data["iso_equivalent"] == 0
    assert "rpicam-still" in data["command_preview"]
    assert "--raw" in data["command_preview"]
    assert "--awb auto" in data["command_preview"]
    assert "--awbgains" not in data["command_preview"]


def test_preview_dict_includes_stream_command():
    data = _preview_dict(CaptureSettings(), PreviewSettings(width=800, height=600, framerate=10, quality=65))

    assert data["width"] == 800
    assert data["media_type"].startswith("multipart/x-mixed-replace")
    assert "rpicam-vid" in data["command_preview"]
    assert "--codec mjpeg" in data["command_preview"]
    assert "--width 800" in data["command_preview"]


def test_serial_ports_returns_list():
    assert isinstance(_serial_ports(), list)
