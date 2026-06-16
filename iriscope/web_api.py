from __future__ import annotations

import asyncio
import importlib.util
import json
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from .capture import (
    build_rpicam_command,
    capture_remote_calibration,
    capture_remote_session,
    verify_remote_camera,
)
from .config import CaptureSettings, ProcessingSettings, load_config, merge_capture
from .labeling import inspect_preprocessing, load_label, save_label
from .processing import IMAGE_SUFFIXES, find_input_images, process_session

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field
except ModuleNotFoundError as exc:  # pragma: no cover - import-time guidance
    raise RuntimeError("The web API requires `pip install -e .[web]`.") from exc


PROJECT_ROOT = Path.cwd()
CAPTURES_ROOT = PROJECT_ROOT / "captures"


class CaptureRequest(BaseModel):
    subject: str = Field(min_length=1)
    eye: Literal["left", "right"]
    count: int | None = Field(default=None, ge=1, le=60)
    shutter_us: int | None = Field(default=None, ge=1)
    gain: float | None = Field(default=None, ge=0.0)
    awb_red: float | None = Field(default=None, ge=0.1)
    awb_blue: float | None = Field(default=None, ge=0.1)
    pull: bool = True


class ProcessRequest(BaseModel):
    session_dir: str = Field(min_length=1)
    stack_method: Literal["sigma", "median", "mean"] = "sigma"
    sigma: float = Field(default=2.5, ge=0.1)
    min_frames: int = Field(default=3, ge=1)
    max_working_edge: int | None = Field(default=None, ge=64)


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


def create_app() -> FastAPI:
    app = FastAPI(title="Iriscope Host API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        config = load_config()
        return {
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
            },
            "config": {
                "exists": Path(".iriscope.toml").exists(),
                "pi_host": config.pi.host,
                "pi_user": config.pi.user,
                "remote_root": config.pi.remote_root,
                "capture": _capture_dict(config.capture),
                "processing": asdict(config.processing),
            },
            "tools": _tool_status(),
            "serial_ports": _serial_ports(),
            "camera_devices": _camera_devices(),
            "capture_root": str(CAPTURES_ROOT),
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

    @app.post("/api/calibrate")
    async def calibrate() -> dict[str, Any]:
        config = load_config()
        example = " ".join(build_rpicam_command("test.jpg", config.capture, "test.json"))
        if not config.pi.host:
            return {
                "ok": False,
                "status": "no_host",
                "message": "No Pi host configured in .iriscope.toml.",
                "command": example,
            }
        try:
            camera_list, remote_dir = await asyncio.to_thread(_run_calibration, config)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "status": "captured", "camera_list": camera_list, "remote_dir": remote_dir}

    @app.post("/api/capture")
    async def capture(request: CaptureRequest) -> dict[str, Any]:
        config = load_config()
        if not config.pi.host:
            raise HTTPException(status_code=400, detail="No Pi host configured in .iriscope.toml.")
        awb = None
        if request.awb_red is not None and request.awb_blue is not None:
            awb = (request.awb_red, request.awb_blue)
        settings = merge_capture(
            config.capture,
            count=request.count,
            shutter_us=request.shutter_us,
            gain=request.gain,
            awb_gains=awb,
        )
        try:
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
        settings = ProcessingSettings(
            stack_method=request.stack_method,
            sigma=request.sigma,
            min_frames=request.min_frames,
            max_working_edge=request.max_working_edge,
            save_intermediates=True,
        )
        session_dir = _resolve_local_path(request.session_dir)
        try:
            result = await asyncio.to_thread(process_session, session_dir, None, settings)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "ok": True,
            "output_dir": str(result.output_dir),
            "enhanced_jpg": str(result.enhanced_jpg),
            "enhanced_tif": str(result.enhanced_tif),
            "report_json": str(result.report_json),
            "contact_sheet": str(result.contact_sheet),
        }

    @app.post("/api/preprocess")
    async def preprocess(request: PreprocessRequest) -> dict[str, Any]:
        session_dir = _resolve_local_path(request.session_dir)
        try:
            report = await asyncio.to_thread(inspect_preprocessing, session_dir, request.max_frames)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "report": report}

    @app.get("/api/label")
    async def label(session_dir: str) -> dict[str, Any]:
        root = _resolve_local_path(session_dir)
        try:
            record = await asyncio.to_thread(load_label, root)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "label": record}

    @app.post("/api/label")
    async def save_session_label(request: LabelRequest) -> dict[str, Any]:
        root = _resolve_local_path(request.session_dir)
        data = request.model_dump()
        data.pop("session_dir", None)
        try:
            record = await asyncio.to_thread(save_label, root, data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "label": record}

    return app


app = create_app()


def _run_calibration(config) -> tuple[str, str]:
    camera_list = verify_remote_camera(config.pi)
    remote_dir = capture_remote_calibration(config.pi, config.capture)
    return camera_list, remote_dir


def _capture_dict(settings: CaptureSettings) -> dict[str, Any]:
    data = asdict(settings)
    data["awb_gains"] = list(settings.awb_gains)
    data["command_preview"] = " ".join(build_rpicam_command("frame_0001.jpg", settings, "frame_0001.json"))
    return data


def _tool_status() -> dict[str, Any]:
    modules = ["cv2", "rawpy", "numpy", "PIL", "skimage", "scipy", "imageio", "serial"]
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
    if platform.system() == "Windows":
        ports.update(_powershell_lines(_WINDOWS_SERIAL_COMMAND))
    return sorted(ports)


def _camera_devices() -> list[dict[str, str]]:
    devices_by_name: dict[str, dict[str, str]] = {}
    if platform.system() == "Windows":
        for item in _windows_pnp_cameras():
            devices_by_name[item["name"]] = item
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
        devices_by_name.setdefault(name, {"name": name, "instance_id": "", "source": "dshow"})
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
                    "Select-Object FriendlyName,InstanceId | ConvertTo-Json -Compress"
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
        if name:
            devices.append({"name": name, "instance_id": instance_id, "source": "pnp"})
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


_WINDOWS_SERIAL_COMMAND = (
    "$ports=@([System.IO.Ports.SerialPort]::GetPortNames()); "
    "$p='HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\USB\\VID_1D6B&PID_0104&MI_02\\6&B0C2142&1&0002\\Device Parameters'; "
    "if (Test-Path $p) { $ports += (Get-ItemProperty -Path $p -Name PortName).PortName }; "
    "$ports | Sort-Object -Unique"
)


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


def _list_sessions() -> list[dict[str, Any]]:
    if not CAPTURES_ROOT.exists():
        return []
    sessions = []
    for path in sorted(CAPTURES_ROOT.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        report = _read_report(path / "processed" / "report.json")
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
                "outputs": report.get("outputs", {}) if report else {},
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


def _resolve_local_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Path does not exist: {resolved}")
    if resolved.is_file() and resolved.suffix.lower() in IMAGE_SUFFIXES:
        return resolved.parent
    return resolved
