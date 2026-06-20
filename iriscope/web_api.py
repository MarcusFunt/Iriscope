from __future__ import annotations

import asyncio
import importlib.util
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import quote

from .capture import (
    build_rpicam_mjpeg_command,
    build_rpicam_command,
    capture_remote_calibration,
    capture_remote_session,
    posix_join,
    shell_join,
    verify_remote_camera,
)
from .config import (
    CaptureSettings,
    PiConfig,
    PreviewSettings,
    ProcessingSettings,
    ProjectConfig,
    QualityThresholds,
    load_config,
    merge_capture,
)
from .labeling import inspect_preprocessing, load_label, save_label
from .processing import IMAGE_SUFFIXES, find_input_images, process_session

try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
    from pydantic import BaseModel, Field
except ModuleNotFoundError as exc:  # pragma: no cover - import-time guidance
    raise RuntimeError("The web API requires `pip install -e .[web]`.") from exc


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(os.environ.get("IRISCOPE_PROJECT_ROOT", DEFAULT_PROJECT_ROOT)).expanduser().resolve()
CONFIG_PATH = PROJECT_ROOT / ".iriscope.toml"
CAPTURES_ROOT = Path(os.environ.get("IRISCOPE_CAPTURES_ROOT", PROJECT_ROOT / "captures")).expanduser().resolve()
ARTIFACT_SUFFIXES = IMAGE_SUFFIXES | {".json", ".html", ".csv", ".txt"}
MJPEG_BOUNDARY = "iriscope-frame"
_PREVIEW_LOCK = threading.Lock()
_ACTIVE_PREVIEW_PROCESS: subprocess.Popen[bytes] | None = None
_ACTIVE_PREVIEW_PI: PiConfig | None = None
_LAST_PREVIEW_FRAME: dict[str, Any] | None = None
_WEBRTC_LOCK = threading.Lock()
_ACTIVE_WEBRTC_PEERS: set[Any] = set()
_ACTIVE_WEBRTC_TRACKS: set[Any] = set()
HEALTH_STALE_AFTER_S = 15.0
REMOTE_MJPEG_PREVIEW_PATTERN = "rpicam-vid .*--codec mjpeg .* -o -"

AwbMode = Literal["auto", "incandescent", "tungsten", "fluorescent", "indoor", "daylight", "cloudy", "custom", "manual"]
MeteringMode = Literal["centre", "spot", "average", "custom"]
ExposureMode = Literal["normal", "sport"]
HdrMode = Literal["off", "auto", "sensor", "single-exp"]


class CaptureRequest(BaseModel):
    subject: str = Field(min_length=1)
    eye: Literal["left", "right"]
    count: int | None = Field(default=None, ge=1, le=60)
    shutter_us: int | None = Field(default=None, ge=0)
    gain: float | None = Field(default=None, ge=0.0)
    awb: AwbMode | None = None
    awb_red: float | None = Field(default=None, ge=0.1)
    awb_blue: float | None = Field(default=None, ge=0.1)
    pull: bool = True


class ProcessRequest(BaseModel):
    session_dir: str = Field(min_length=1)
    stack_method: Literal["sigma", "median", "mean"] = "sigma"
    sigma: float = Field(default=2.5, ge=0.1)
    min_frames: int = Field(default=3, ge=1)
    max_working_edge: int | None = Field(default=None, ge=64)
    dark_path: str | None = None
    flat_path: str | None = None
    save_intermediates: bool | None = None


class LabelRequest(BaseModel):
    session_dir: str = Field(min_length=1)
    subject_code: str = ""
    eye: str = ""
    consent_recorded: bool = False
    biometric_category: str = "iris_visible_light"
    allowed_use: str = "local_enhancement_only"
    exclude_from_training: bool = True
    operator: str = ""
    lighting: str = ""
    lens: str = ""
    capture_distance_mm: int | None = Field(default=None, ge=1)
    quality_label: str = "unreviewed"
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class PreprocessRequest(BaseModel):
    session_dir: str = Field(min_length=1)
    max_frames: int = Field(default=16, ge=1, le=60)


class WebRTCOfferRequest(BaseModel):
    sdp: str = Field(min_length=1)
    type: Literal["offer"] = "offer"


class PiConfigRequest(BaseModel):
    host: str | None = None
    user: str = Field(default="pi", min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    remote_root: str = Field(default="/home/pi/iriscope", min_length=1)
    ssh_key: str | None = None
    connect_timeout: int = Field(default=15, ge=1, le=60)


class CaptureConfigRequest(BaseModel):
    count: int = Field(default=12, ge=1, le=60)
    shutter_us: int = Field(default=0, ge=0)
    gain: float = Field(default=0.0, ge=0.0)
    awb: AwbMode = "auto"
    awb_gains: list[float] | None = Field(default_factory=lambda: [3.2, 1.4], min_length=2, max_length=2)
    denoise: Literal["auto", "off", "cdn_off", "cdn_fast", "cdn_hq"] = "cdn_fast"
    quality: int = Field(default=95, ge=1, le=100)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    metering: MeteringMode = "centre"
    exposure: ExposureMode = "normal"
    ev: float = Field(default=0.0, ge=-10.0, le=10.0)
    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)
    contrast: float = Field(default=1.0, ge=0.0)
    saturation: float = Field(default=1.0, ge=0.0)
    sharpness: float = Field(default=1.0, ge=0.0)
    tuning_file: str | None = None
    mode: str | None = None
    hdr: HdrMode = "off"
    nopreview: bool = True
    immediate: bool = True
    raw: bool = True


class PreviewConfigRequest(BaseModel):
    width: int = Field(default=640, ge=64)
    height: int = Field(default=480, ge=64)
    framerate: int = Field(default=12, ge=1, le=120)
    quality: int = Field(default=70, ge=1, le=100)
    stream_timeout_s: int = Field(default=0, ge=0, le=3600)


class QualityThresholdsRequest(BaseModel):
    max_clip_fraction: float = Field(default=0.20, ge=0.0, le=1.0)
    min_relative_focus: float = Field(default=0.35, ge=0.0)
    min_median_focus: float = Field(default=10.0, ge=0.0)
    min_mean_luma: float = Field(default=0.02, ge=0.0, le=1.0)
    max_mean_luma: float = Field(default=0.98, ge=0.0, le=1.0)
    min_alignment_score: float = Field(default=0.55, ge=0.0)
    max_eval_clip_fraction: float = Field(default=0.35, ge=0.0, le=1.0)
    min_mask_coverage: float = Field(default=0.06, ge=0.0, le=1.0)
    max_mask_coverage: float = Field(default=0.48, ge=0.0, le=1.0)
    min_pupil_iris_ratio: float = Field(default=0.18, ge=0.0)
    max_pupil_iris_ratio: float = Field(default=0.68, ge=0.0)
    min_iris_radius_fraction: float = Field(default=0.16, ge=0.0)
    max_iris_radius_fraction: float = Field(default=0.55, ge=0.0)
    max_center_offset_fraction: float = Field(default=0.28, ge=0.0)
    max_edge_gain: float = Field(default=7.0, ge=0.0)
    max_edge_gain_with_contrast: float = Field(default=5.5, ge=0.0)
    max_contrast_gain_for_edge: float = Field(default=3.0, ge=0.0)


class ProcessingConfigRequest(BaseModel):
    stack_method: Literal["sigma", "median", "mean"] = "sigma"
    sigma: float = Field(default=2.5, ge=0.1)
    min_frames: int = Field(default=3, ge=1)
    save_intermediates: bool = True
    max_working_edge: int | None = Field(default=None, ge=64)
    quality: QualityThresholdsRequest = Field(default_factory=QualityThresholdsRequest)


class ConfigUpdateRequest(BaseModel):
    pi: PiConfigRequest
    capture: CaptureConfigRequest
    preview: PreviewConfigRequest
    processing: ProcessingConfigRequest


def create_app() -> FastAPI:
    app = FastAPI(title="Iriscope Host API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def enforce_local_or_token(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        host = request.headers.get("host", "").split(":", 1)[0].strip("[]").lower()
        if _is_loopback_host(host):
            return await call_next(request)
        token = os.environ.get("IRISCOPE_ADMIN_TOKEN", "")
        if token and _request_token(request) == token:
            return await call_next(request)
        return JSONResponse(
            {"detail": "Remote Iriscope API access requires IRISCOPE_ADMIN_TOKEN."},
            status_code=403,
        )

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        config = load_config(CONFIG_PATH)
        health = await asyncio.to_thread(_health_status, config)
        return {
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
            },
            "config": _status_config_dict(config),
            "tools": _tool_status(),
            "serial_ports": _serial_ports(),
            "camera_devices": _camera_devices(),
            "health": health,
            "capture_root": str(CAPTURES_ROOT),
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        config = load_config(CONFIG_PATH)
        return {"ok": True, "path": str(CONFIG_PATH), "config": _config_payload(config)}

    @app.post("/api/config")
    async def save_config(request: ConfigUpdateRequest) -> dict[str, Any]:
        config = _config_from_request(request)
        try:
            await asyncio.to_thread(_write_project_config, CONFIG_PATH, config)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "ok": True,
            "path": str(CONFIG_PATH),
            "config": _config_payload(config),
        }

    @app.get("/api/sessions")
    async def sessions() -> list[dict[str, Any]]:
        return await asyncio.to_thread(_list_sessions)

    @app.get("/api/uvc/snapshot")
    async def uvc_snapshot(
        device: str = Query(default="UVC Camera"),
        index: int | None = Query(default=None, ge=0, le=16),
    ) -> FileResponse:
        try:
            path = await asyncio.to_thread(_capture_snapshot, device, index)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg", filename="iriscope-uvc-snapshot.jpg")

    @app.get("/api/pi/snapshot")
    async def pi_snapshot() -> FileResponse:
        config = load_config(CONFIG_PATH)
        if not config.pi.host:
            raise HTTPException(status_code=400, detail="No Pi host configured in .iriscope.toml.")
        try:
            await _stop_pi_preview_transports(config)
            path = await asyncio.to_thread(_capture_pi_snapshot, config)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg", filename="iriscope-pi-snapshot.jpg")

    @app.get("/api/pi/stream.mjpeg")
    async def pi_stream() -> StreamingResponse:
        config = load_config(CONFIG_PATH)
        if not config.pi.host:
            raise HTTPException(status_code=400, detail="No Pi host configured in .iriscope.toml.")
        try:
            stream = await asyncio.to_thread(_open_pi_mjpeg_stream, config)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return StreamingResponse(
            stream,
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        )

    @app.post("/api/pi/webrtc/offer")
    async def pi_webrtc_offer(request: WebRTCOfferRequest) -> dict[str, Any]:
        config = load_config(CONFIG_PATH)
        if not config.pi.host:
            raise HTTPException(status_code=400, detail="No Pi host configured in .iriscope.toml.")
        try:
            await _stop_pi_preview_transports(config)
            answer = await _create_webrtc_answer(config, request.sdp, request.type)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, **answer}

    @app.post("/api/pi/preview/stop")
    async def stop_preview() -> dict[str, bool]:
        config = load_config(CONFIG_PATH)
        await _stop_pi_preview_transports(config)
        return {"ok": True}

    @app.post("/api/calibrate")
    async def calibrate() -> dict[str, Any]:
        config = load_config(CONFIG_PATH)
        example = " ".join(build_rpicam_command("test.jpg", config.capture, "test.json"))
        if not config.pi.host:
            return {
                "ok": False,
                "status": "no_host",
                "message": "No Pi host configured in .iriscope.toml.",
                "command": example,
            }
        try:
            await _stop_pi_preview_transports(config)
            camera_list, remote_dir = await asyncio.to_thread(_run_calibration, config)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "status": "captured", "camera_list": camera_list, "remote_dir": remote_dir}

    @app.post("/api/capture")
    async def capture(request: CaptureRequest) -> dict[str, Any]:
        config = load_config(CONFIG_PATH)
        if not config.pi.host:
            raise HTTPException(status_code=400, detail="No Pi host configured in .iriscope.toml.")
        awb_gains = None
        if request.awb_red is not None and request.awb_blue is not None:
            awb_gains = (request.awb_red, request.awb_blue)
        settings = merge_capture(
            config.capture,
            count=request.count,
            shutter_us=request.shutter_us,
            gain=request.gain,
            awb=request.awb,
            awb_gains=awb_gains,
        )
        try:
            await _stop_pi_preview_transports(config)
            result = await asyncio.to_thread(
                capture_remote_session,
                config.pi,
                request.subject,
                request.eye,
                settings,
                CAPTURES_ROOT,
                request.pull,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "ok": True,
            "session_name": result.session_name,
            "remote_dir": result.remote_dir,
            "local_dir": str(result.local_dir) if result.local_dir else None,
            "frame_count": result.frame_count,
        }

    @app.post("/api/process")
    async def process(request: ProcessRequest) -> dict[str, Any]:
        config = load_config(CONFIG_PATH)
        settings = ProcessingSettings(
            stack_method=request.stack_method,
            sigma=request.sigma,
            min_frames=request.min_frames,
            max_working_edge=request.max_working_edge,
            save_intermediates=(
                request.save_intermediates
                if request.save_intermediates is not None
                else config.processing.save_intermediates
            ),
            quality=config.processing.quality,
        )
        session_dir = _resolve_session_path(request.session_dir)
        try:
            dark_path = _resolve_optional_image_path(request.dark_path)
            flat_path = _resolve_optional_image_path(request.flat_path)
            result = await asyncio.to_thread(
                process_session,
                session_dir,
                None,
                settings,
                dark_path,
                flat_path,
            )
            report = _read_report(result.report_json) or {}
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "ok": True,
            "output_dir": str(result.output_dir),
            "enhanced_jpg": str(result.enhanced_jpg),
            "enhanced_tif": str(result.enhanced_tif),
            "report_json": str(result.report_json),
            "contact_sheet": str(result.contact_sheet),
            "quality_status": report.get("quality_status", "unknown"),
            "requires_recapture": bool(report.get("requires_recapture", False)),
            "quality_flags": report.get("quality_flags", []),
        }

    @app.post("/api/preprocess")
    async def preprocess(request: PreprocessRequest) -> dict[str, Any]:
        config = load_config(CONFIG_PATH)
        session_dir = _resolve_session_path(request.session_dir)
        try:
            report = await asyncio.to_thread(
                inspect_preprocessing,
                session_dir,
                request.max_frames,
                config.processing.quality,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "report": report}

    @app.get("/api/label")
    async def label(session_dir: str) -> dict[str, Any]:
        root = _resolve_session_path(session_dir)
        try:
            record = await asyncio.to_thread(load_label, root)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "label": record}

    @app.post("/api/label")
    async def save_session_label(request: LabelRequest) -> dict[str, Any]:
        root = _resolve_session_path(request.session_dir)
        data = _model_data(request)
        data.pop("session_dir", None)
        try:
            record = await asyncio.to_thread(save_label, root, data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "label": record}

    @app.get("/api/artifact")
    async def artifact(path: str) -> FileResponse:
        try:
            artifact_path = _resolve_artifact_path(path)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        media_type = mimetypes.guess_type(artifact_path.name)[0] or "application/octet-stream"
        return FileResponse(artifact_path, media_type=media_type, filename=artifact_path.name)

    @app.get("/api/review")
    async def review(session_dir: str) -> HTMLResponse:
        from .review import generate_review, resolve_processed_dir

        root = _resolve_session_path(session_dir)
        try:
            html_path = await asyncio.to_thread(generate_review, root, False)
            processed = resolve_processed_dir(root)
            text = html_path.read_text(encoding="utf-8")
            report = _read_report(processed / "report.json") or {}
            for output_path in (report.get("outputs") or {}).values():
                if not output_path:
                    continue
                output = Path(output_path)
                text = text.replace(
                    f'src="{output.name}"',
                    f'src="{_artifact_href(output)}"',
                )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return HTMLResponse(text)

    return app

def _run_calibration(config) -> tuple[str, str]:
    camera_list = verify_remote_camera(config.pi)
    remote_dir = capture_remote_calibration(config.pi, config.capture)
    return camera_list, remote_dir


def _health_status(config: ProjectConfig) -> dict[str, Any]:
    pnp_cameras = _camera_devices()
    health: dict[str, Any] = {
        "ssh": _check_skipped("No Pi host configured.") if not config.pi.host else _check_ssh(config.pi),
        "rpicam": _check_skipped("SSH is not available."),
        "preview": _check_skipped("SSH is not available."),
        "disk": _check_skipped("SSH is not available."),
        "windows_pnp": _check_windows_pnp(pnp_cameras),
    }
    if health["ssh"]["ok"]:
        health["rpicam"] = _check_rpicam(config.pi)
        health["disk"] = _check_remote_disk(config.pi)
        health["preview"] = _check_preview_frame(config)
    return health


def _check_ssh(pi: PiConfig) -> dict[str, Any]:
    started = time.monotonic()
    completed = _run_remote_health(pi, "printf iriscope-ssh-ok", timeout=6)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    stdout = completed.stdout.decode("utf-8", "replace").strip()
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    ok = completed.returncode == 0 and "iriscope-ssh-ok" in stdout
    return _check_result(
        ok,
        "SSH key access verified." if ok else _failure_message(stderr, completed.returncode),
        elapsed_ms=elapsed_ms,
        target=pi.target if pi.host else "",
    )


def _check_rpicam(pi: PiConfig) -> dict[str, Any]:
    started = time.monotonic()
    completed = _run_remote_health(pi, "rpicam-hello --list-cameras", timeout=12)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = (completed.stdout + completed.stderr).decode("utf-8", "replace")
    camera_lines = [
        line.strip()
        for line in text.splitlines()
        if " : " in line and ("imx" in line.lower() or "[" in line)
    ]
    ok = completed.returncode == 0 and bool(camera_lines) and "no cameras" not in text.lower()
    return _check_result(
        ok,
        camera_lines[0] if ok else _failure_message(text.strip(), completed.returncode),
        elapsed_ms=elapsed_ms,
        camera_list=text.strip()[:4000],
    )


def _check_remote_disk(pi: PiConfig) -> dict[str, Any]:
    remote_path = shlex_quote(pi.remote_root)
    command = f"df -Pk {remote_path} 2>/dev/null || df -Pk \"$HOME\""
    started = time.monotonic()
    completed = _run_remote_health(pi, command, timeout=8)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = completed.stdout.decode("utf-8", "replace").strip()
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        return _check_result(False, _failure_message(stderr, completed.returncode), elapsed_ms=elapsed_ms)
    parsed = _parse_df(text)
    if not parsed:
        return _check_result(False, "Could not parse remote disk output.", elapsed_ms=elapsed_ms, raw=text)
    ok = parsed["free_bytes"] >= 512 * 1024 * 1024
    message = f"{parsed['free_gb']:.2f} GB free on {parsed['mount']}"
    return _check_result(ok, message, elapsed_ms=elapsed_ms, **parsed)


def _check_preview_frame(config: ProjectConfig) -> dict[str, Any]:
    recent = _recent_preview_frame()
    if recent:
        return _check_result(True, "Preview frame received from active stream.", **recent)
    if _active_preview_running():
        return _check_result(False, "Preview stream is active but no frame has been received yet.", status="warming_up")

    preview = replace(config.preview, stream_timeout_s=3)
    command = build_rpicam_mjpeg_command(config.capture, preview)
    args = _ssh_args(config.pi, tty=False) + [config.pi.target, shell_join(command)]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return _check_result(False, "Preview probe timed out.", elapsed_ms=15000)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    frame_bytes = _first_jpeg_length(completed.stdout)
    if frame_bytes:
        _remember_preview_frame(frame_bytes)
        return _check_result(
            True,
            "Preview probe received a JPEG frame.",
            elapsed_ms=elapsed_ms,
            frame_bytes=frame_bytes,
            stream_bytes=len(completed.stdout),
        )
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    if "Device or resource busy" in stderr:
        return _check_result(
            False,
            _failure_message(stderr, completed.returncode),
            status="busy",
            elapsed_ms=elapsed_ms,
            stream_bytes=len(completed.stdout),
        )
    return _check_result(
        False,
        _failure_message(stderr or "No JPEG frame found in preview output.", completed.returncode),
        elapsed_ms=elapsed_ms,
        stream_bytes=len(completed.stdout),
    )


def _check_windows_pnp(cameras: list[dict[str, str]]) -> dict[str, Any]:
    if platform.system() != "Windows":
        return _check_result(True, "Windows PnP is not applicable on this host.", status="not_applicable")
    if not cameras:
        return _check_result(False, "No Windows PnP camera devices reported.", devices=[])
    ok_devices = [camera for camera in cameras if camera.get("status", "").lower() == "ok"]
    unknown_devices = [camera for camera in cameras if camera.get("status", "").lower() not in {"ok", ""}]
    if ok_devices:
        return _check_result(
            True,
            f"{len(ok_devices)} camera device(s) report PnP status OK.",
            devices=cameras,
            problem_devices=unknown_devices,
        )
    return _check_result(False, "No camera devices report PnP status OK.", devices=cameras)


def _check_result(ok: bool, message: str, **details: Any) -> dict[str, Any]:
    status = details.pop("status", None) or ("ok" if ok else "error")
    return {"ok": bool(ok), "status": status, "message": message, **details}


def _check_skipped(message: str) -> dict[str, Any]:
    return {"ok": False, "status": "skipped", "message": message}


def _run_remote_health(pi: PiConfig, remote_command: str, timeout: int) -> subprocess.CompletedProcess[bytes]:
    args = _ssh_args(pi, tty=False) + [pi.target, remote_command]
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, exc.stdout or b"", exc.stderr or b"Timed out")


def _ssh_args(pi: PiConfig, tty: bool = True) -> list[str]:
    args = [
        "ssh",
        "-p",
        str(pi.port),
        "-o",
        f"ConnectTimeout={min(int(pi.connect_timeout), 6)}",
        "-o",
        "BatchMode=yes",
    ]
    if not tty:
        args.insert(1, "-T")
    if pi.ssh_key:
        args += ["-i", pi.ssh_key]
    return args


def _failure_message(text: str, returncode: int) -> str:
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    clean = " ".join(clean.split())
    if clean:
        return clean[:280]
    return f"Command failed with exit code {returncode}."


def _parse_df(text: str) -> dict[str, Any] | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 6:
        return None
    try:
        total_bytes = int(parts[1]) * 1024
        used_bytes = int(parts[2]) * 1024
        free_bytes = int(parts[3]) * 1024
    except ValueError:
        return None
    return {
        "filesystem": parts[0],
        "mount": parts[-1],
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "free_gb": free_bytes / (1024**3),
        "used_percent": parts[4],
    }


def _recent_preview_frame() -> dict[str, Any] | None:
    with _PREVIEW_LOCK:
        frame = dict(_LAST_PREVIEW_FRAME) if _LAST_PREVIEW_FRAME else None
    if not frame:
        return None
    age_s = time.time() - float(frame["received_at"])
    if age_s > HEALTH_STALE_AFTER_S:
        return None
    frame["age_s"] = round(age_s, 2)
    return frame


def _active_preview_running() -> bool:
    with _PREVIEW_LOCK:
        process = _ACTIVE_PREVIEW_PROCESS
    return process is not None and process.poll() is None


def _remember_preview_frame(frame_bytes: int) -> None:
    with _PREVIEW_LOCK:
        global _LAST_PREVIEW_FRAME
        _LAST_PREVIEW_FRAME = {
            "received_at": time.time(),
            "frame_bytes": int(frame_bytes),
        }


def _first_jpeg_length(data: bytes) -> int:
    start = data.find(b"\xff\xd8")
    if start < 0:
        return 0
    end = data.find(b"\xff\xd9", start + 2)
    if end < 0:
        return 0
    return end + 2 - start


def _config_from_request(request: ConfigUpdateRequest) -> ProjectConfig:
    pi = request.pi
    capture = request.capture
    preview = request.preview
    processing = request.processing
    return ProjectConfig(
        pi=PiConfig(
            host=_blank_to_none(pi.host),
            user=pi.user,
            port=pi.port,
            remote_root=pi.remote_root,
            ssh_key=_blank_to_none(pi.ssh_key),
            connect_timeout=pi.connect_timeout,
        ),
        capture=CaptureSettings(
            count=capture.count,
            shutter_us=capture.shutter_us,
            gain=capture.gain,
            awb=capture.awb,
            awb_gains=None if capture.awb_gains is None else (float(capture.awb_gains[0]), float(capture.awb_gains[1])),
            denoise=capture.denoise,
            quality=capture.quality,
            width=capture.width,
            height=capture.height,
            metering=capture.metering,
            exposure=capture.exposure,
            ev=capture.ev,
            brightness=capture.brightness,
            contrast=capture.contrast,
            saturation=capture.saturation,
            sharpness=capture.sharpness,
            tuning_file=_blank_to_none(capture.tuning_file),
            mode=_blank_to_none(capture.mode),
            hdr=capture.hdr,
            nopreview=capture.nopreview,
            immediate=capture.immediate,
            raw=capture.raw,
        ),
        preview=PreviewSettings(
            width=preview.width,
            height=preview.height,
            framerate=preview.framerate,
            quality=preview.quality,
            stream_timeout_s=preview.stream_timeout_s,
        ),
        processing=ProcessingSettings(
            stack_method=processing.stack_method,
            sigma=processing.sigma,
            min_frames=processing.min_frames,
            save_intermediates=processing.save_intermediates,
            max_working_edge=processing.max_working_edge,
            quality=QualityThresholds(**_model_data(processing.quality)),
        ),
    )


def _status_config_dict(config: ProjectConfig) -> dict[str, Any]:
    return {
        "exists": CONFIG_PATH.exists(),
        "path": str(CONFIG_PATH),
        "pi_host": config.pi.host,
        "pi_user": config.pi.user,
        "pi_port": config.pi.port,
        "remote_root": config.pi.remote_root,
        "ssh_key_configured": bool(config.pi.ssh_key),
        "connect_timeout": config.pi.connect_timeout,
        "capture": _capture_dict(config.capture),
        "preview": _preview_dict(config.capture, config.preview),
        "processing": asdict(config.processing),
    }


def _config_payload(config: ProjectConfig) -> dict[str, Any]:
    return {
        "pi": asdict(config.pi),
        "capture": _capture_dict(config.capture),
        "preview": _preview_dict(config.capture, config.preview),
        "processing": asdict(config.processing),
    }


def _write_project_config(path: Path, config: ProjectConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[pi]",
    ]
    if config.pi.host:
        lines.append(f"host = {_toml(config.pi.host)}")
    lines += [
        f"user = {_toml(config.pi.user)}",
        f"port = {config.pi.port}",
        f"remote_root = {_toml(config.pi.remote_root)}",
    ]
    if config.pi.ssh_key:
        lines.append(f"ssh_key = {_toml(config.pi.ssh_key)}")
    lines += [
        f"connect_timeout = {config.pi.connect_timeout}",
        "",
        "[capture]",
        f"count = {config.capture.count}",
        f"shutter_us = {config.capture.shutter_us}",
        f"gain = {_float(config.capture.gain)}",
        f"awb = {_toml(config.capture.awb)}",
    ]
    if config.capture.awb_gains is not None:
        lines.append(f"awb_gains = [{_float(config.capture.awb_gains[0])}, {_float(config.capture.awb_gains[1])}]")
    lines += [
        f"denoise = {_toml(config.capture.denoise)}",
        f"quality = {config.capture.quality}",
        f"metering = {_toml(config.capture.metering)}",
        f"exposure = {_toml(config.capture.exposure)}",
        f"ev = {_float(config.capture.ev)}",
        f"brightness = {_float(config.capture.brightness)}",
        f"contrast = {_float(config.capture.contrast)}",
        f"saturation = {_float(config.capture.saturation)}",
        f"sharpness = {_float(config.capture.sharpness)}",
    ]
    if config.capture.width is not None:
        lines.append(f"width = {config.capture.width}")
    if config.capture.height is not None:
        lines.append(f"height = {config.capture.height}")
    if config.capture.tuning_file:
        lines.append(f"tuning_file = {_toml(config.capture.tuning_file)}")
    if config.capture.mode:
        lines.append(f"mode = {_toml(config.capture.mode)}")
    lines += [
        f"hdr = {_toml(config.capture.hdr)}",
        f"nopreview = {_toml(config.capture.nopreview)}",
        f"immediate = {_toml(config.capture.immediate)}",
        f"raw = {_toml(config.capture.raw)}",
        "",
        "[preview]",
        f"width = {config.preview.width}",
        f"height = {config.preview.height}",
        f"framerate = {config.preview.framerate}",
        f"quality = {config.preview.quality}",
        f"stream_timeout_s = {config.preview.stream_timeout_s}",
        "",
        "[processing]",
        f"stack_method = {_toml(config.processing.stack_method)}",
        f"sigma = {_float(config.processing.sigma)}",
        f"min_frames = {config.processing.min_frames}",
        f"save_intermediates = {_toml(config.processing.save_intermediates)}",
    ]
    if config.processing.max_working_edge is not None:
        lines.append(f"max_working_edge = {config.processing.max_working_edge}")
    quality = config.processing.quality
    lines += [
        "",
        "[processing.quality]",
        f"max_clip_fraction = {_float(quality.max_clip_fraction)}",
        f"min_relative_focus = {_float(quality.min_relative_focus)}",
        f"min_median_focus = {_float(quality.min_median_focus)}",
        f"min_mean_luma = {_float(quality.min_mean_luma)}",
        f"max_mean_luma = {_float(quality.max_mean_luma)}",
        f"min_alignment_score = {_float(quality.min_alignment_score)}",
        f"max_eval_clip_fraction = {_float(quality.max_eval_clip_fraction)}",
        f"min_mask_coverage = {_float(quality.min_mask_coverage)}",
        f"max_mask_coverage = {_float(quality.max_mask_coverage)}",
        f"min_pupil_iris_ratio = {_float(quality.min_pupil_iris_ratio)}",
        f"max_pupil_iris_ratio = {_float(quality.max_pupil_iris_ratio)}",
        f"min_iris_radius_fraction = {_float(quality.min_iris_radius_fraction)}",
        f"max_iris_radius_fraction = {_float(quality.max_iris_radius_fraction)}",
        f"max_center_offset_fraction = {_float(quality.max_center_offset_fraction)}",
        f"max_edge_gain = {_float(quality.max_edge_gain)}",
        f"max_edge_gain_with_contrast = {_float(quality.max_edge_gain_with_contrast)}",
        f"max_contrast_gain_for_edge = {_float(quality.max_contrast_gain_for_edge)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _toml(value: str | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def _float(value: float) -> str:
    return f"{float(value):g}"


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    return clean or None


def _capture_dict(settings: CaptureSettings) -> dict[str, Any]:
    data = asdict(settings)
    data["awb_gains"] = list(settings.awb_gains) if settings.awb_gains is not None else None
    data["iso_equivalent"] = int(round(settings.gain * 100)) if settings.gain > 0 else 0
    data["command_preview"] = " ".join(build_rpicam_command("frame_0001.jpg", settings, "frame_0001.json"))
    return data


def _preview_dict(capture_settings: CaptureSettings, preview_settings: PreviewSettings) -> dict[str, Any]:
    data = asdict(preview_settings)
    data["command_preview"] = " ".join(build_rpicam_mjpeg_command(capture_settings, preview_settings))
    data["media_type"] = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"
    return data


def _tool_status() -> dict[str, Any]:
    modules = ["cv2", "rawpy", "numpy", "PIL", "skimage", "scipy", "imageio", "serial", "aiortc"]
    return {
        "python_modules": {name: importlib.util.find_spec(name) is not None for name in modules},
        "executables": {
            "ssh": shutil.which("ssh") is not None,
            "scp": shutil.which("scp") is not None,
            "ffmpeg": shutil.which("ffmpeg") is not None,
        },
    }


def _serial_ports() -> list[str]:
    ports: set[str] = set()
    try:
        import serial.tools.list_ports

        ports.update(port.device for port in serial.tools.list_ports.comports())
    except ModuleNotFoundError:
        pass
    return sorted(ports)


def _camera_devices() -> list[dict[str, str]]:
    devices_by_name: dict[str, dict[str, str]] = {}
    if platform.system() == "Windows":
        for item in _windows_pnp_cameras():
            devices_by_name[item.get("instance_id") or item["name"]] = item
        if devices_by_name:
            return list(devices_by_name.values())
    if platform.system() != "Windows" or not shutil.which("ffmpeg"):
        return list(devices_by_name.values())
    try:
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return list(devices_by_name.values())
    text = completed.stderr + completed.stdout
    for line in text.splitlines():
        line = line.strip()
        if '" (video)' not in line:
            continue
        name = line.split('"')[1]
        devices_by_name.setdefault(name, {"name": name, "instance_id": "", "source": "dshow", "status": ""})
    return list(devices_by_name.values())


def _windows_pnp_cameras() -> list[dict[str, str]]:
    if platform.system() != "Windows":
        return []
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-PnpDevice -Class Camera -ErrorAction SilentlyContinue | "
                    "Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return []
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    devices = []
    for row in rows:
        name = str(row.get("FriendlyName") or "")
        instance_id = str(row.get("InstanceId") or "")
        status = str(row.get("Status") or "")
        if name:
            devices.append({"name": name, "instance_id": instance_id, "status": status, "source": "pnp"})
    return devices


def _powershell_lines(command: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


async def _create_webrtc_answer(config: ProjectConfig, sdp: str, type_: str) -> dict[str, str]:
    RTCPeerConnection, RTCSessionDescription = _import_aiortc()
    await stop_pi_webrtc_preview()

    pc = RTCPeerConnection()
    track = _new_pi_mjpeg_video_track(config)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange() -> None:
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await _close_webrtc_peer(pc, track)

    with _WEBRTC_LOCK:
        _ACTIVE_WEBRTC_PEERS.add(pc)
        _ACTIVE_WEBRTC_TRACKS.add(track)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        pc.addTrack(track)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await _wait_for_ice_gathering_complete(pc)
    except Exception:
        await _close_webrtc_peer(pc, track)
        raise

    if pc.localDescription is None:
        await _close_webrtc_peer(pc, track)
        raise RuntimeError("WebRTC answer creation did not produce a local description.")
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


async def _wait_for_ice_gathering_complete(pc, timeout_s: float = 5.0) -> None:
    if getattr(pc, "iceGatheringState", None) == "complete":
        return
    ready = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_icegatheringstatechange() -> None:
        if pc.iceGatheringState == "complete":
            ready.set()

    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout_s)
    except TimeoutError:
        pass


def _import_aiortc():
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription
    except ModuleNotFoundError as exc:
        raise RuntimeError("WebRTC preview requires `pip install -e .[webrtc]` or `pip install aiortc`.") from exc
    return RTCPeerConnection, RTCSessionDescription


def _new_pi_mjpeg_video_track(config: ProjectConfig):
    try:
        from aiortc import VideoStreamTrack
        from aiortc.mediastreams import MediaStreamError
        from av import VideoFrame
        import cv2
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("WebRTC preview requires aiortc, av, numpy, and OpenCV.") from exc

    class PiMjpegVideoTrack(VideoStreamTrack):
        kind = "video"

        def __init__(self, track_config: ProjectConfig) -> None:
            super().__init__()
            self.config = track_config
            self.process: subprocess.Popen[bytes] | None = None
            self.buffer = bytearray()
            self.frame_lock = threading.Lock()
            self.latest_image: Any | None = None
            self.last_error: str | None = None
            self.reader_thread: threading.Thread | None = None
            self.stopped = False

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            await asyncio.to_thread(self._ensure_reader)
            with self.frame_lock:
                image = None if self.latest_image is None else self.latest_image.copy()
                last_error = self.last_error
            if last_error is not None:
                raise MediaStreamError(last_error)
            if image is None:
                image = np.zeros((self.config.preview.height, self.config.preview.width, 3), dtype=np.uint8)
            frame = VideoFrame.from_ndarray(image, format="bgr24")
            frame.pts = pts
            frame.time_base = time_base
            return frame

        def stop(self) -> None:
            self.stopped = True
            super().stop()
            self._close_process()

        def _ensure_reader(self) -> None:
            if self.stopped:
                raise MediaStreamError("Pi WebRTC preview stopped.")
            if (
                self.process is not None
                and self.process.poll() is None
                and self.reader_thread is not None
                and self.reader_thread.is_alive()
            ):
                return
            self._close_process()
            stop_pi_preview()
            _stop_remote_mjpeg_preview(self.config.pi)
            command = build_rpicam_mjpeg_command(self.config.capture, self.config.preview)
            args = _preview_ssh_args(self.config.pi) + [self.config.pi.target, shell_join(command)]
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with self.frame_lock:
                self.last_error = None
            self.reader_thread = threading.Thread(target=self._reader_loop, args=(self.process,), daemon=True)
            self.reader_thread.start()

        def _reader_loop(self, process: subprocess.Popen[bytes]) -> None:
            if process.stdout is None:
                with self.frame_lock:
                    self.last_error = "Pi preview process did not expose stdout."
                return
            try:
                while not self.stopped:
                    frame = _pop_jpeg_frame(self.buffer)
                    if frame:
                        arr = np.frombuffer(frame, dtype=np.uint8)
                        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if image is not None:
                            with self.frame_lock:
                                self.latest_image = image
                                self.last_error = None
                            _remember_preview_frame(len(frame))
                        continue
                    chunk = process.stdout.read1(16384) if hasattr(process.stdout, "read1") else process.stdout.read(16384)
                    if not chunk:
                        break
                    self.buffer.extend(chunk)
                    if len(self.buffer) > 4 * 1024 * 1024:
                        del self.buffer[:-2]
            except Exception as exc:
                if not self.stopped:
                    with self.frame_lock:
                        self.last_error = str(exc)
                return
            if not self.stopped:
                stderr = _process_stderr(process)
                with self.frame_lock:
                    self.last_error = stderr or "Pi WebRTC preview stream ended."

        def _close_process(self) -> None:
            process = self.process
            self.process = None
            self.reader_thread = None
            self.buffer.clear()
            if process is not None:
                _terminate_process(process)
            _stop_remote_mjpeg_preview(self.config.pi)

    return PiMjpegVideoTrack(config)


async def _stop_pi_preview_transports(config: ProjectConfig | None = None) -> None:
    await stop_pi_webrtc_preview()
    await asyncio.to_thread(stop_pi_preview)
    if config is not None and config.pi.host:
        await asyncio.to_thread(_stop_remote_mjpeg_preview, config.pi)


async def stop_pi_webrtc_preview() -> None:
    with _WEBRTC_LOCK:
        peers = list(_ACTIVE_WEBRTC_PEERS)
        tracks = list(_ACTIVE_WEBRTC_TRACKS)
        _ACTIVE_WEBRTC_PEERS.clear()
        _ACTIVE_WEBRTC_TRACKS.clear()
    for track in tracks:
        try:
            await asyncio.to_thread(track.stop)
        except Exception:
            pass
    for pc in peers:
        try:
            await pc.close()
        except Exception:
            pass


async def _close_webrtc_peer(pc, track) -> None:
    with _WEBRTC_LOCK:
        _ACTIVE_WEBRTC_PEERS.discard(pc)
        _ACTIVE_WEBRTC_TRACKS.discard(track)
    try:
        await asyncio.to_thread(track.stop)
    except Exception:
        pass
    if getattr(pc, "connectionState", None) != "closed":
        try:
            await pc.close()
        except Exception:
            pass


def _pop_jpeg_frame(buffer: bytearray) -> bytes | None:
    start = buffer.find(b"\xff\xd8")
    if start < 0:
        if len(buffer) > 2:
            del buffer[:-2]
        return None
    if start > 0:
        del buffer[:start]
        start = 0
    end = buffer.find(b"\xff\xd9", start + 2)
    if end < 0:
        return None
    frame = bytes(buffer[start : end + 2])
    del buffer[: end + 2]
    return frame


def _process_stderr(process: subprocess.Popen[bytes]) -> str:
    stderr = b""
    if process.stderr is not None:
        try:
            stderr = process.stderr.read() or b""
        except Exception:
            stderr = b""
    return _failure_message(stderr.decode("utf-8", "replace"), process.returncode or 1)


def _open_pi_mjpeg_stream(config) -> Iterator[bytes]:
    process = _start_pi_preview_process(config)
    return _iter_pi_mjpeg(process)


def _start_pi_preview_process(config) -> subprocess.Popen[bytes]:
    stop_pi_preview()
    _stop_remote_mjpeg_preview(config.pi)
    command = build_rpicam_mjpeg_command(config.capture, config.preview)
    args = _preview_ssh_args(config.pi)
    args += [config.pi.target, shell_join(command)]
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        stop_pi_preview()
        raise
    with _PREVIEW_LOCK:
        global _ACTIVE_PREVIEW_PI, _ACTIVE_PREVIEW_PROCESS
        _ACTIVE_PREVIEW_PROCESS = process
        _ACTIVE_PREVIEW_PI = config.pi
    return process


def _preview_ssh_args(pi: PiConfig) -> list[str]:
    args = [
        "ssh",
        "-T",
        "-p",
        str(pi.port),
        "-o",
        f"ConnectTimeout={pi.connect_timeout}",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if pi.ssh_key:
        args += ["-i", pi.ssh_key]
    return args


def _iter_pi_mjpeg(process: subprocess.Popen[bytes]) -> Iterator[bytes]:
    if process.stdout is None:
        raise RuntimeError("Preview process did not expose stdout.")
    buffer = bytearray()
    try:
        while True:
            chunk = process.stdout.read1(16384) if hasattr(process.stdout, "read1") else process.stdout.read(16384)
            if not chunk:
                break
            buffer.extend(chunk)
            yield from _drain_jpeg_frames(buffer)
            if len(buffer) > 4 * 1024 * 1024:
                del buffer[:-2]
    finally:
        _stop_preview_process(process)


def _drain_jpeg_frames(buffer: bytearray) -> Iterator[bytes]:
    while True:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            if len(buffer) > 2:
                del buffer[:-2]
            return
        if start > 0:
            del buffer[:start]
            start = 0
        end = buffer.find(b"\xff\xd9", start + 2)
        if end < 0:
            return
        frame = bytes(buffer[start : end + 2])
        del buffer[: end + 2]
        _remember_preview_frame(len(frame))
        yield _mjpeg_part(frame)


def _mjpeg_part(frame: bytes) -> bytes:
    return (
        f"--{MJPEG_BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(frame)}\r\n\r\n"
    ).encode("ascii") + frame + b"\r\n"


def stop_pi_preview() -> None:
    with _PREVIEW_LOCK:
        global _ACTIVE_PREVIEW_PI, _ACTIVE_PREVIEW_PROCESS
        process = _ACTIVE_PREVIEW_PROCESS
        pi = _ACTIVE_PREVIEW_PI
        _ACTIVE_PREVIEW_PROCESS = None
        _ACTIVE_PREVIEW_PI = None
    if process is not None:
        _terminate_process(process)
    if pi is not None:
        _stop_remote_mjpeg_preview(pi)


def _stop_preview_process(process: subprocess.Popen[bytes]) -> None:
    with _PREVIEW_LOCK:
        global _ACTIVE_PREVIEW_PI, _ACTIVE_PREVIEW_PROCESS
        pi = None
        if _ACTIVE_PREVIEW_PROCESS is process:
            pi = _ACTIVE_PREVIEW_PI
            _ACTIVE_PREVIEW_PROCESS = None
            _ACTIVE_PREVIEW_PI = None
    _terminate_process(process)
    if pi is not None:
        _stop_remote_mjpeg_preview(pi)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _stop_remote_mjpeg_preview(pi: PiConfig) -> None:
    if not pi.host:
        return
    try:
        _run_remote_health(pi, f"pkill -f {shlex_quote(REMOTE_MJPEG_PREVIEW_PATTERN)} || true", timeout=5)
    except Exception:
        pass


def _capture_snapshot(device: str, index: int | None) -> Path:
    tmp = Path(tempfile.gettempdir()) / f"iriscope_uvc_{int(time.time() * 1000)}.jpg"
    if platform.system() == "Windows" and shutil.which("ffmpeg") and index is None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "dshow",
                "-video_size",
                "640x480",
                "-framerate",
                "30",
                "-i",
                f"video={device}",
                "-frames:v",
                "1",
                "-update",
                "1",
                str(tmp),
            ],
            check=True,
            timeout=10,
        )
        return tmp

    import cv2

    capture_index = 0 if index is None else index
    cap = cv2.VideoCapture(capture_index)
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"Could not read camera index {capture_index}.")
        cv2.imwrite(str(tmp), frame)
        return tmp
    finally:
        cap.release()


def _capture_pi_snapshot(config) -> Path:
    settings = CaptureSettings(
        count=1,
        shutter_us=config.capture.shutter_us,
        gain=config.capture.gain,
        awb=config.capture.awb,
        awb_gains=config.capture.awb_gains,
        denoise=config.capture.denoise,
        quality=min(config.capture.quality, 90),
        width=1024,
        height=768,
        metering=config.capture.metering,
        exposure=config.capture.exposure,
        ev=config.capture.ev,
        brightness=config.capture.brightness,
        contrast=config.capture.contrast,
        saturation=config.capture.saturation,
        sharpness=config.capture.sharpness,
        tuning_file=config.capture.tuning_file,
        mode=config.capture.mode,
        hdr=config.capture.hdr,
        nopreview=True,
        immediate=True,
        raw=False,
    )
    remote_dir = posix_join(config.pi.remote_root, "preview")
    stem = f"preview_{int(time.time() * 1000)}"
    remote_jpg = f"{stem}.jpg"
    command = build_rpicam_command(remote_jpg, settings)
    remote_command = f"mkdir -p {shlex_quote(remote_dir)} && cd {shlex_quote(remote_dir)} && {shell_join(command)}"
    _run_ssh_batch(config.pi, remote_command)

    tmp = Path(tempfile.gettempdir()) / f"iriscope_pi_{stem}.jpg"
    _scp_from_pi(config.pi, f"{remote_dir}/{remote_jpg}", tmp)
    try:
        _run_ssh_batch(config.pi, f"rm -f {shlex_quote(posix_join(remote_dir, remote_jpg))}")
    except Exception:
        pass
    return tmp


def _run_ssh_batch(pi, remote_command: str) -> subprocess.CompletedProcess[bytes]:
    args = [
        "ssh",
        "-p",
        str(pi.port),
        "-o",
        f"ConnectTimeout={pi.connect_timeout}",
        "-o",
        "BatchMode=yes",
    ]
    if pi.ssh_key:
        args += ["-i", pi.ssh_key]
    args += [pi.target, remote_command]
    try:
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace").strip()
        if "Permission denied" in stderr or "publickey,password" in stderr:
            raise RuntimeError(
                "Pi SSH key authentication is required for GUI preview. "
                "Add this PC's public key to the Pi or set [pi].ssh_key."
            ) from exc
        raise RuntimeError(stderr or f"SSH command failed with exit code {exc.returncode}") from exc


def _scp_from_pi(pi, remote_path: str, local_path: Path) -> None:
    args = ["scp", "-P", str(pi.port), "-o", "BatchMode=yes"]
    if pi.ssh_key:
        args += ["-i", pi.ssh_key]
    args += [f"{pi.target}:{remote_path}", str(local_path)]
    try:
        subprocess.run(args, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(stderr or f"SCP failed with exit code {exc.returncode}") from exc


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _list_sessions() -> list[dict[str, Any]]:
    if not CAPTURES_ROOT.exists():
        return []
    sessions = []
    for path in sorted(CAPTURES_ROOT.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        report_path = path / "processed" / "report.json"
        report = _read_report(report_path)
        outputs = dict(report.get("outputs", {}) if report else {})
        if report:
            outputs["report_json"] = str(report_path)
            review_path = path / "processed" / "review.html"
            if review_path.exists():
                outputs["review_html"] = str(review_path)
        inputs = find_input_images(path)
        sessions.append(
            {
                "name": path.name,
                "path": str(path),
                "modified": path.stat().st_mtime,
                "frame_count": len(inputs),
                "processed": report is not None,
                "labeled": (path / "iriscope_labels.json").exists(),
                "preprocessed": (path / "preprocess_report.json").exists(),
                "outputs": outputs,
            }
        )
    return sessions[:40]


def _read_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _allowed_origins() -> list[str]:
    configured = os.environ.get("IRISCOPE_ALLOWED_ORIGINS")
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ]


def _is_loopback_host(host: str) -> bool:
    return host in {"", "localhost", "testserver", "127.0.0.1", "::1"} or host.startswith("127.")


def _request_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-iriscope-token", "").strip()


def _calibration_root() -> Path:
    return Path(os.environ.get("IRISCOPE_CALIBRATION_ROOT", PROJECT_ROOT / "calibration")).expanduser().resolve()


def _resolve_session_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if not resolved.exists():
        raise HTTPException(status_code=422, detail=f"Path does not exist: {resolved}")
    if not resolved.is_dir():
        raise HTTPException(status_code=422, detail=f"Session path is not a directory: {resolved}")
    if not _is_relative_to(resolved, CAPTURES_ROOT):
        raise HTTPException(status_code=422, detail=f"Session path is outside the capture root: {resolved}")
    return resolved


def _resolve_optional_image_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Calibration image does not exist: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Calibration path is not a file: {resolved}")
    if resolved.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"Unsupported calibration image type: {resolved.suffix}")
    calibration_root = _calibration_root()
    if not (_is_relative_to(resolved, CAPTURES_ROOT) or _is_relative_to(resolved, calibration_root)):
        raise ValueError(f"Calibration image is outside allowed roots: {resolved}")
    return resolved


def _resolve_artifact_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Path does not exist: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Path is not a file: {resolved}")
    if resolved.suffix.lower() not in ARTIFACT_SUFFIXES:
        raise ValueError(f"Unsupported artifact type: {resolved.suffix}")
    if not _is_relative_to(resolved, CAPTURES_ROOT):
        raise ValueError(f"Artifact is outside the capture root: {resolved}")
    return resolved


def _artifact_href(path: str | Path) -> str:
    return f"/api/artifact?path={quote(str(path), safe='')}"


def _model_data(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


app = create_app()
