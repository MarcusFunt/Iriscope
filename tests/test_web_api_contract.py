import json
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from iriscope import web_api
from iriscope.processing import ProcessResult


def test_status_reads_config_from_anchored_project_root(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["config"]["exists"] is True
    assert payload["config"]["path"] == str(tmp_path / ".iriscope.toml")
    assert payload["config"]["pi_host"] == "iriscope-pi.local"
    assert payload["config"]["ssh_key_configured"] is True
    assert "ssh_key" not in payload["config"]
    assert payload["config"]["capture"]["count"] == 16
    assert payload["config"]["capture"]["awb"] == "manual"
    assert payload["config"]["capture"]["awb_gains"] == [2.0, 1.2]
    assert payload["config"]["preview"]["width"] == 640
    assert "rpicam-vid" in payload["config"]["preview"]["command_preview"]
    assert payload["config"]["preview"]["webrtc_available"] is True
    assert payload["health"]["ssh"]["status"] == "test"
    assert payload["capture_root"] == str(tmp_path / "captures")

    config_response = client.get("/api/config")
    assert config_response.status_code == 200
    config_payload = config_response.json()
    assert config_payload["config"]["pi"]["ssh_key"] == "C:/keys/test_rsa"
    assert config_payload["config"]["processing"]["quality"]["max_clip_fraction"] == 0.2


def test_serves_built_web_dist_when_available(tmp_path: Path, monkeypatch):
    web_dist = tmp_path / "web-dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<!doctype html><title>Iriscope Docker</title>", encoding="utf-8")
    monkeypatch.setattr(web_api, "WEB_DIST", web_dist)

    client = TestClient(web_api.create_app())
    response = client.get("/")

    assert response.status_code == 200
    assert "Iriscope Docker" in response.text


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


def test_pi_webrtc_offer_endpoint_returns_answer(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    async def fake_answer(config, sdp, type_):
        return {"sdp": "answer-sdp", "type": "answer"}

    monkeypatch.setattr(web_api, "_create_webrtc_answer", fake_answer)

    response = client.post("/api/pi/webrtc/offer", json={"sdp": "offer-sdp", "type": "offer"})

    assert response.status_code == 200
    assert response.json()["sdp"] == "answer-sdp"
    assert response.json()["type"] == "answer"


def test_pi_webrtc_offer_endpoint_rejects_when_webrtc_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IRISCOPE_WEBRTC_ENABLED", "false")
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post("/api/pi/webrtc/offer", json={"sdp": "offer-sdp", "type": "offer"})
    status_response = client.get("/api/status")

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]
    assert status_response.json()["config"]["preview"]["webrtc_available"] is False


def test_remote_preview_cleanup_targets_only_mjpeg_preview(monkeypatch):
    commands: list[tuple[str, int]] = []

    def fake_remote_health(pi, remote_command, timeout):
        commands.append((remote_command, timeout))

    monkeypatch.setattr(web_api, "_run_remote_health", fake_remote_health)

    web_api._stop_remote_mjpeg_preview(web_api.PiConfig(host="iriscope-pi.local", user="camera"))

    assert commands == [
        ("pkill -f 'rpicam-vid .*--codec mjpeg .* -o -' || true", 5),
    ]


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


def test_session_endpoints_reject_paths_outside_capture_root(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    outside = tmp_path / "outside-session"
    outside.mkdir()

    preprocess = client.post("/api/preprocess", json={"session_dir": str(outside)})
    label = client.get("/api/label", params={"session_dir": str(outside)})
    review = client.get("/api/review", params={"session_dir": str(outside)})

    assert preprocess.status_code == 422
    assert label.status_code == 422
    assert review.status_code == 422


def test_process_uses_configured_save_intermediates_when_omitted(tmp_path: Path, monkeypatch):
    client, captures = _client(tmp_path, monkeypatch)
    session = captures / "S003_left_20260616_153000"
    session.mkdir()
    _write_image(session / "frame_0001.png")
    captured_settings = {}

    def fake_process_session(session_dir, output_dir, settings, dark_path, flat_path):
        captured_settings["save_intermediates"] = settings.save_intermediates
        processed = Path(session_dir) / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        _write_image(processed / "enhanced.jpg")
        _write_image(processed / "enhanced.tif")
        _write_image(processed / "contact_sheet.jpg")
        report = processed / "report.json"
        report.write_text(
            json.dumps({"quality_status": "pass", "requires_recapture": False, "quality_flags": []}),
            encoding="utf-8",
        )
        return ProcessResult(
            processed,
            processed / "enhanced.jpg",
            processed / "enhanced.tif",
            report,
            processed / "contact_sheet.jpg",
        )

    monkeypatch.setattr(web_api, "process_session", fake_process_session)

    response = client.post("/api/process", json={"session_dir": str(session)})

    assert response.status_code == 200
    assert captured_settings["save_intermediates"] is False


def test_config_endpoint_persists_settings(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/config",
        json={
            "pi": {
                "host": "10.42.0.2",
                "user": "iriscope",
                "port": 2222,
                "remote_root": "/home/iriscope/captures",
                "ssh_key": "C:/keys/iriscope_rsa",
                "connect_timeout": 9,
            },
            "capture": {
                "count": 8,
                "shutter_us": 7000,
                "gain": 1.2,
                "awb": "manual",
                "awb_gains": [1.7, 1.3],
                "denoise": "off",
                "quality": 92,
                "width": None,
                "height": None,
                "metering": "spot",
                "exposure": "normal",
                "ev": 0.3,
                "brightness": 0.1,
                "contrast": 1.1,
                "saturation": 0.9,
                "sharpness": 1.2,
                "tuning_file": "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json",
                "mode": None,
                "hdr": "off",
                "nopreview": True,
                "immediate": True,
                "raw": True,
            },
            "preview": {
                "width": 640,
                "height": 480,
                "framerate": 10,
                "quality": 70,
                "stream_timeout_s": 0,
            },
            "processing": {
                "stack_method": "median",
                "sigma": 2.0,
                "min_frames": 4,
                "save_intermediates": True,
                "max_working_edge": 640,
                "quality": {
                    "max_clip_fraction": 0.18,
                    "min_relative_focus": 0.4,
                    "min_median_focus": 11.0,
                    "min_mean_luma": 0.03,
                    "max_mean_luma": 0.97,
                    "min_alignment_score": 0.6,
                    "max_eval_clip_fraction": 0.32,
                    "min_mask_coverage": 0.07,
                    "max_mask_coverage": 0.45,
                    "min_pupil_iris_ratio": 0.2,
                    "max_pupil_iris_ratio": 0.65,
                    "min_iris_radius_fraction": 0.18,
                    "max_iris_radius_fraction": 0.52,
                    "max_center_offset_fraction": 0.25,
                    "max_edge_gain": 6.5,
                    "max_edge_gain_with_contrast": 5.0,
                    "max_contrast_gain_for_edge": 2.8,
                },
            },
            "calibration": {
                "target_luma_min": 0.4,
                "target_luma_max": 0.6,
                "max_clip_fraction": 0.025,
                "sample_budget": 8,
                "retain_artifacts": True,
                "thumbnail_edge": 320,
                "min_shutter_us": 900,
                "max_shutter_us": 25000,
                "min_gain": 1.0,
                "max_gain": 6.0,
                "command_timeout_s": 45,
                "scp_timeout_s": 45,
                "weights": {
                    "luma": 0.3,
                    "clipping": 0.2,
                    "focus": 0.2,
                    "mask": 0.12,
                    "color": 0.07,
                    "gain": 0.06,
                    "metadata": 0.05,
                },
            },
        },
    )

    assert response.status_code == 200
    text = (tmp_path / ".iriscope.toml").read_text(encoding="utf-8")
    assert 'host = "10.42.0.2"' in text
    assert 'awb = "manual"' in text
    assert 'tuning_file = "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json"' in text
    assert 'stack_method = "median"' in text
    assert "[processing.quality]" in text
    assert "max_clip_fraction = 0.18" in text
    assert "[calibration]" in text
    assert "sample_budget = 8" in text
    assert "[calibration.weights]" in text


def test_calibration_run_rejects_missing_pi_host(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    config_path = tmp_path / ".iriscope.toml"
    config_path.write_text(
        """
[pi]
user = "camera"
""",
        encoding="utf-8",
    )

    response = client.post("/api/calibration/run")

    assert response.status_code == 400
    assert "No Pi host" in response.json()["detail"]


def test_calibration_run_enforces_single_active_job(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    web_api._CALIBRATION_JOB = {"active": True, "job_id": "running"}

    response = client.post("/api/calibration/run")

    assert response.status_code == 409
    web_api._CALIBRATION_JOB = None


def test_calibration_run_status_apply_and_revert(tmp_path: Path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    recommendation = _fake_recommendation()

    def fake_run_auto_calibration(config, local_root, progress=None):
        report = Path(local_root) / "cal_test" / "calibration_report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}", encoding="utf-8")
        if progress:
            progress({"phase": "exposure_sweep", "progress": 0.5, "message": "halfway"})
        return SimpleNamespace(
            candidates=[{"candidate_id": "candidate_00", "score": 0.88}],
            warnings=[],
            recommendation=recommendation,
            report_path=report,
            remote_dir="/home/camera/iriscope/calibration-runs/cal_test",
            local_dir=report.parent,
        )

    monkeypatch.setattr(web_api, "run_auto_calibration", fake_run_auto_calibration)

    start = client.post("/api/calibration/run")
    assert start.status_code == 200

    status = _poll_calibration_status(client)
    assert status["status"] == "complete"
    assert status["recommendation"]["capture"]["shutter_us"] == 11000

    applied = client.post("/api/calibration/apply")
    assert applied.status_code == 200
    applied_payload = applied.json()
    assert applied_payload["status"] == "applied"
    assert "ssh_key" not in applied_payload["config"]
    assert 'shutter_us = 11000' in (tmp_path / ".iriscope.toml").read_text(encoding="utf-8")

    reverted = client.post("/api/calibration/revert")
    assert reverted.status_code == 200
    assert reverted.json()["status"] == "reverted"
    assert 'shutter_us = 6000' in (tmp_path / ".iriscope.toml").read_text(encoding="utf-8")


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path]:
    captures = tmp_path / "captures"
    captures.mkdir()
    config_path = tmp_path / ".iriscope.toml"
    config_path.write_text(
        """
[pi]
host = "iriscope-pi.local"
user = "camera"
ssh_key = "C:/keys/test_rsa"

[capture]
count = 16
shutter_us = 6000
gain = 1.5
awb_gains = [2.0, 1.2]

[processing]
save_intermediates = false
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_api, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(web_api, "CONFIG_PATH", config_path.resolve())
    monkeypatch.setattr(web_api, "CAPTURES_ROOT", captures.resolve())
    monkeypatch.setattr(web_api, "_health_status", lambda config: _fake_health())
    monkeypatch.setattr(web_api, "_stop_remote_mjpeg_preview", lambda pi: None)
    web_api._CALIBRATION_JOB = None
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


def _fake_health() -> dict:
    check = {"ok": True, "status": "test", "message": "test health"}
    return {
        "ssh": check,
        "rpicam": check,
        "preview": check,
        "disk": check,
        "windows_pnp": check,
    }


def _fake_recommendation() -> dict:
    return {
        "candidate_id": "candidate_00",
        "label": "best",
        "score": 0.88,
        "confidence": "high",
        "capture": {
            "count": 16,
            "shutter_us": 11000,
            "gain": 1.4,
            "awb": "manual",
            "awb_gains": [2.1, 1.3],
            "denoise": "cdn_fast",
            "quality": 95,
            "width": None,
            "height": None,
            "metering": "centre",
            "exposure": "normal",
            "ev": 0.0,
            "brightness": 0.0,
            "contrast": 1.0,
            "saturation": 1.0,
            "sharpness": 1.0,
            "tuning_file": None,
            "mode": None,
            "hdr": "off",
            "nopreview": True,
            "immediate": True,
            "raw": True,
        },
        "settings_diff": [{"field": "shutter_us", "before": 6000, "after": 11000}],
        "quality": {
            "mean_luma": 0.48,
            "clip_fraction": 0.005,
            "focus_score": 55.0,
            "mask_coverage": 0.22,
            "geometry_confidence": "high",
        },
        "artifacts": {"best_thumbnail": None, "best_frame": None, "report": None},
        "reasons": ["luma score 1.00"],
    }


def _poll_calibration_status(client: TestClient) -> dict:
    for _ in range(40):
        payload = client.get("/api/calibration/status").json()
        if payload["status"] in {"complete", "failed"}:
            return payload
        time.sleep(0.05)
    return payload
