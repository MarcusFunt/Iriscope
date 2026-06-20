from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import CaptureSettings, PiConfig, PreviewSettings


VALID_EYES = {"left", "right"}


@dataclass(frozen=True)
class CaptureResult:
    session_name: str
    remote_dir: str
    local_dir: Path | None
    frame_count: int


def sanitize_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        raise ValueError("Value becomes empty after sanitization.")
    return cleaned


def make_session_name(subject: str, eye: str, timestamp: datetime | None = None) -> str:
    eye_value = eye.lower()
    if eye_value not in VALID_EYES:
        raise ValueError("Eye must be 'left' or 'right'.")
    ts = timestamp or datetime.now()
    return f"{sanitize_token(subject)}_{eye_value}_{ts:%Y%m%d_%H%M%S}"


def build_rpicam_command(
    output_file: str,
    settings: CaptureSettings,
    metadata_file: str | None = None,
) -> list[str]:
    args = ["rpicam-still"]
    if settings.raw:
        args.append("--raw")
    if settings.immediate:
        args.append("--immediate")
    if settings.nopreview:
        args.append("--nopreview")
    if settings.width:
        args += ["--width", str(settings.width)]
    if settings.height:
        args += ["--height", str(settings.height)]
    _extend_camera_options(args, settings)
    args += ["--denoise", settings.denoise]
    args += ["--quality", str(settings.quality)]
    if metadata_file:
        args += ["--metadata", metadata_file, "--metadata-format", "json"]
    args += ["-o", output_file]
    return args


def build_rpicam_mjpeg_command(
    settings: CaptureSettings,
    preview: PreviewSettings,
    output_file: str = "-",
) -> list[str]:
    args = [
        "rpicam-vid",
        "-t",
        str(max(0, preview.stream_timeout_s * 1000)),
        "-n",
        "--codec",
        "mjpeg",
        "--width",
        str(preview.width),
        "--height",
        str(preview.height),
        "--framerate",
        str(preview.framerate),
        "--quality",
        str(preview.quality),
    ]
    _extend_camera_options(args, settings)
    args += ["--denoise", settings.denoise, "-o", output_file]
    return args


def verify_remote_camera(pi: PiConfig) -> str:
    result = run_ssh(pi, "rpicam-hello --list-cameras")
    return result.stdout.strip()


def capture_remote_session(
    pi: PiConfig,
    subject: str,
    eye: str,
    settings: CaptureSettings,
    local_parent: str | Path = "captures",
    pull: bool = True,
) -> CaptureResult:
    session = make_session_name(subject, eye)
    remote_dir = posix_join(pi.remote_root, session)
    run_ssh(pi, f"mkdir -p {shlex.quote(remote_dir)}")

    for index in range(1, settings.count + 1):
        stem = f"frame_{index:04d}"
        command = build_rpicam_command(
            output_file=f"{stem}.jpg",
            metadata_file=f"{stem}.json",
            settings=settings,
        )
        remote_command = f"cd {shlex.quote(remote_dir)} && {shell_join(command)}"
        run_ssh(pi, remote_command)

    local_dir: Path | None = None
    if pull:
        local_dir = pull_remote_session(pi, remote_dir, Path(local_parent))
    return CaptureResult(session, remote_dir, local_dir, settings.count)


def capture_remote_calibration(pi: PiConfig, settings: CaptureSettings) -> str:
    remote_dir = posix_join(pi.remote_root, "calibration")
    run_ssh(pi, f"mkdir -p {shlex.quote(remote_dir)}")
    command = build_rpicam_command(
        output_file="test.jpg",
        metadata_file="test.json",
        settings=settings,
    )
    run_ssh(pi, f"cd {shlex.quote(remote_dir)} && {shell_join(command)}")
    return remote_dir


def pull_remote_session(pi: PiConfig, remote_dir: str, local_parent: Path) -> Path:
    local_parent.mkdir(parents=True, exist_ok=True)
    destination = local_parent / Path(remote_dir).name
    args = ["scp", "-P", str(pi.port), "-o", "StrictHostKeyChecking=accept-new"]
    if pi.ssh_key:
        args += ["-i", pi.ssh_key]
    args += ["-r", f"{pi.target}:{remote_dir}", str(local_parent)]
    subprocess.run(args, check=True, capture_output=True, text=True)
    return destination


def run_ssh(pi: PiConfig, remote_command: str) -> subprocess.CompletedProcess[str]:
    args = [
        "ssh",
        "-p",
        str(pi.port),
        "-o",
        f"ConnectTimeout={pi.connect_timeout}",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if pi.ssh_key:
        args += ["-i", pi.ssh_key]
    args += [pi.target, remote_command]
    return subprocess.run(args, check=True, capture_output=True, text=True)


def shell_join(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def posix_join(*parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part]
    if not cleaned:
        return "/"
    prefix = "/" if parts[0].startswith("/") else ""
    return prefix + "/".join(cleaned)


def _format_float(value: float) -> str:
    return f"{float(value):g}"


def _extend_camera_options(args: list[str], settings: CaptureSettings) -> None:
    if settings.tuning_file:
        args += ["--tuning-file", settings.tuning_file]
    if settings.mode:
        args += ["--mode", settings.mode]
    if settings.shutter_us > 0:
        args += ["--shutter", str(settings.shutter_us)]
    if settings.gain > 0:
        args += ["--gain", _format_float(settings.gain)]

    awb = settings.awb.strip().lower()
    if awb == "manual":
        if settings.awb_gains is not None:
            args += ["--awbgains", f"{_format_float(settings.awb_gains[0])},{_format_float(settings.awb_gains[1])}"]
    elif awb:
        args += ["--awb", awb]

    if settings.metering:
        args += ["--metering", settings.metering]
    if settings.exposure:
        args += ["--exposure", settings.exposure]
    if settings.ev:
        args += ["--ev", _format_float(settings.ev)]
    args += ["--brightness", _format_float(settings.brightness)]
    args += ["--contrast", _format_float(settings.contrast)]
    args += ["--saturation", _format_float(settings.saturation)]
    args += ["--sharpness", _format_float(settings.sharpness)]
    if settings.hdr and settings.hdr != "off":
        args += ["--hdr", settings.hdr]
