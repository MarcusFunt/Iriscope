import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from iriscope import web_api


def test_status_reads_config_from_anchored_project_root(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["config"]["exists"] is True
    assert payload["config"]["path"] == str(tmp_path / ".iriscope.toml")
    assert payload["config"]["pi_host"] == "iriscope-pi.local"
    assert payload["config"]["capture"]["count"] == 16
    assert payload["config"]["capture"]["awb_gains"] == [2.0, 1.2]
    assert payload["config"]["preview"]["width"] == 640
    assert "rpicam-vid" in payload["config"]["preview"]["command_preview"]
    assert payload["capture_root"] == str(tmp_path / "captures")


def test_sessions_expose_outputs_and_artifact_endpoints_are_bounded(tmp_path: Path, monkeypatch):
    client, captures = _client(tmp_path, monkeypatch)
    session = _processed_session(captures)

    sessions_response = client.get("/api/sessions")

    assert sessions_response.status_code == 200
    sessions = sessions_response.json()
    assert sessions[0]["path"] == str(session)
    assert sessions[0]["processed"] is True
    assert sessions[0]["outputs"]["report_json"] == str(session / "processed" / "report.json")

    artifact_response = client.get(
        "/api/artifact",
        params={"path": str(session / "processed" / "enhanced.jpg")},
    )
    assert artifact_response.status_code == 200
    assert artifact_response.headers["content-type"].startswith("image/jpeg")

    review_response = client.get("/api/review", params={"session_dir": str(session)})
    assert review_response.status_code == 200
    assert "/api/artifact?path=" in review_response.text

    outside = tmp_path / "outside.jpg"
    _write_image(outside)
    rejected_response = client.get("/api/artifact", params={"path": str(outside)})
    assert rejected_response.status_code == 404


def test_pi_snapshot_endpoint_returns_remote_preview_file(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    snapshot = tmp_path / "snapshot.jpg"
    _write_image(snapshot)
    monkeypatch.setattr(web_api, "_capture_pi_snapshot", lambda config: snapshot)

    response = client.get("/api/pi/snapshot")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content


def test_pi_stream_endpoint_returns_mjpeg_stream(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    def fake_stream(config):
        yield b"--iriscope-frame\r\nContent-Type: image/jpeg\r\n\r\nframe\r\n"

    monkeypatch.setattr(web_api, "_open_pi_mjpeg_stream", fake_stream)

    response = client.get("/api/pi/stream.mjpeg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert b"--iriscope-frame" in response.content


def test_label_contract_loads_defaults_and_persists_updates(tmp_path: Path, monkeypatch):
    client, captures = _client(tmp_path, monkeypatch)
    session = captures / "S002_right_20260616_153000"
    session.mkdir(parents=True)

    initial = client.get("/api/label", params={"session_dir": str(session)})
    assert initial.status_code == 200
    assert initial.json()["label"]["exclude_from_training"] is True

    saved = client.post(
        "/api/label",
        json={
            "session_dir": str(session),
            "subject_code": "S002",
            "eye": "right",
            "consent_recorded": True,
            "notes": "reviewed",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["label"]["subject_code"] == "S002"

    loaded = client.get("/api/label", params={"session_dir": str(session)})
    assert loaded.status_code == 200
    assert loaded.json()["label"]["notes"] == "reviewed"


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path]:
    captures = tmp_path / "captures"
    captures.mkdir()
    config_path = tmp_path / ".iriscope.toml"
    config_path.write_text(
        """
[pi]
host = "iriscope-pi.local"
user = "camera"

[capture]
count = 16
shutter_us = 6000
gain = 1.5
awb_gains = [2.0, 1.2]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_api, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(web_api, "CONFIG_PATH", config_path.resolve())
    monkeypatch.setattr(web_api, "CAPTURES_ROOT", captures.resolve())
    return TestClient(web_api.create_app()), captures


def _processed_session(captures: Path) -> Path:
    session = captures / "S001_left_20260616_153000"
    processed = session / "processed"
    processed.mkdir(parents=True)
    _write_image(session / "frame_0001.png")
    _write_image(processed / "enhanced.jpg")
    _write_image(processed / "contact_sheet.jpg")
    _write_image(processed / "iris_mask.png")
    _write_image(processed / "enhanced.tif")
    report = {
        "session": str(session),
        "frames": [],
        "kept_indices": [0],
        "mask": {"method": "test", "coverage": 0.2},
        "outputs": {
            "enhanced_jpg": str(processed / "enhanced.jpg"),
            "enhanced_tif": str(processed / "enhanced.tif"),
            "contact_sheet": str(processed / "contact_sheet.jpg"),
            "iris_mask": str(processed / "iris_mask.png"),
        },
    }
    (processed / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return session


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (8, 8), (80, 130, 120))
    image.save(path)
