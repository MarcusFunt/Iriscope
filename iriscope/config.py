from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


DEFAULT_CONFIG_PATH = Path(".iriscope.toml")


@dataclass(frozen=True)
class PiConfig:
    host: str | None = None
    user: str = "pi"
    port: int = 22
    remote_root: str = "/home/pi/iriscope"
    ssh_key: str | None = None
    connect_timeout: int = 15

    @property
    def target(self) -> str:
        if not self.host:
            raise ValueError("Pi host is not configured. Set [pi].host or pass --host.")
        return f"{self.user}@{self.host}"


@dataclass(frozen=True)
class CaptureSettings:
    count: int = 12
    shutter_us: int = 0
    gain: float = 0.0
    awb: str = "auto"
    awb_gains: tuple[float, float] | None = (3.2, 1.4)
    denoise: str = "cdn_fast"
    quality: int = 95
    width: int | None = None
    height: int | None = None
    metering: str = "centre"
    exposure: str = "normal"
    ev: float = 0.0
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    sharpness: float = 1.0
    tuning_file: str | None = None
    mode: str | None = None
    hdr: str = "off"
    nopreview: bool = True
    immediate: bool = True
    raw: bool = True


@dataclass(frozen=True)
class PreviewSettings:
    width: int = 640
    height: int = 480
    framerate: int = 12
    quality: int = 70
    stream_timeout_s: int = 0


@dataclass(frozen=True)
class QualityThresholds:
    max_clip_fraction: float = 0.20
    min_relative_focus: float = 0.35
    min_median_focus: float = 10.0
    min_mean_luma: float = 0.02
    max_mean_luma: float = 0.98
    min_alignment_score: float = 0.55
    max_eval_clip_fraction: float = 0.35
    min_mask_coverage: float = 0.06
    max_mask_coverage: float = 0.48
    min_pupil_iris_ratio: float = 0.18
    max_pupil_iris_ratio: float = 0.68
    min_iris_radius_fraction: float = 0.16
    max_iris_radius_fraction: float = 0.55
    max_center_offset_fraction: float = 0.28
    max_edge_gain: float = 7.0
    max_edge_gain_with_contrast: float = 5.5
    max_contrast_gain_for_edge: float = 3.0


@dataclass(frozen=True)
class ProcessingSettings:
    stack_method: str = "sigma"
    sigma: float = 2.5
    min_frames: int = 3
    save_intermediates: bool = True
    max_working_edge: int | None = None
    quality: QualityThresholds = QualityThresholds()


@dataclass(frozen=True)
class ProjectConfig:
    pi: PiConfig = PiConfig()
    capture: CaptureSettings = CaptureSettings()
    preview: PreviewSettings = PreviewSettings()
    processing: ProcessingSettings = ProcessingSettings()


def load_config(path: str | Path | None = None) -> ProjectConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return _apply_env_overrides(ProjectConfig())
    if tomllib is None:
        raise RuntimeError("TOML config files require Python 3.11+ or the `tomli` package.")
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    config = ProjectConfig(
        pi=_parse_pi(data.get("pi", {})),
        capture=_parse_capture(data.get("capture", {})),
        preview=_parse_preview(data.get("preview", {})),
        processing=_parse_processing(data.get("processing", {})),
    )
    return _apply_env_overrides(config)


def merge_pi(config: PiConfig, **overrides: Any) -> PiConfig:
    clean = {key: value for key, value in overrides.items() if value is not None}
    return replace(config, **clean)


def merge_capture(config: CaptureSettings, **overrides: Any) -> CaptureSettings:
    clean = {key: value for key, value in overrides.items() if value is not None}
    if "awb_gains" in clean and isinstance(clean["awb_gains"], str):
        clean["awb_gains"] = parse_awb_gains(clean["awb_gains"])
    return replace(config, **clean)


def merge_processing(config: ProcessingSettings, **overrides: Any) -> ProcessingSettings:
    clean = {key: value for key, value in overrides.items() if value is not None}
    return replace(config, **clean)


def parse_awb_gains(value: str | tuple[float, float] | list[float]) -> tuple[float, float]:
    if isinstance(value, tuple) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    if isinstance(value, list) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) == 2:
            return (float(parts[0]), float(parts[1]))
    raise ValueError("AWB gains must be two numbers, for example '1.8,1.4'.")


def _parse_pi(data: dict[str, Any]) -> PiConfig:
    return PiConfig(
        host=data.get("host"),
        user=data.get("user", "pi"),
        port=int(data.get("port", 22)),
        remote_root=data.get("remote_root", "/home/pi/iriscope"),
        ssh_key=data.get("ssh_key"),
        connect_timeout=int(data.get("connect_timeout", 15)),
    )


def _apply_env_overrides(config: ProjectConfig) -> ProjectConfig:
    pi_overrides: dict[str, Any] = {}
    env_map = {
        "IRISCOPE_PI_HOST": ("host", str),
        "IRISCOPE_PI_USER": ("user", str),
        "IRISCOPE_PI_REMOTE_ROOT": ("remote_root", str),
        "IRISCOPE_PI_SSH_KEY": ("ssh_key", str),
        "IRISCOPE_PI_PORT": ("port", int),
        "IRISCOPE_PI_CONNECT_TIMEOUT": ("connect_timeout", int),
    }
    for env_name, (field_name, parser) in env_map.items():
        if env_name not in os.environ:
            continue
        raw_value = os.environ[env_name].strip()
        if not raw_value:
            if field_name in {"host", "ssh_key"}:
                pi_overrides[field_name] = None
            continue
        value = parser(raw_value)
        if value is not None:
            pi_overrides[field_name] = value
        elif field_name in {"host", "ssh_key"}:
            pi_overrides[field_name] = None
    if not pi_overrides:
        return config
    return replace(config, pi=replace(config.pi, **pi_overrides))


def _parse_capture(data: dict[str, Any]) -> CaptureSettings:
    awb_mode = str(data.get("awb", "manual" if "awb_gains" in data else "auto"))
    awb_gains_value = data.get("awb_gains", [3.2, 1.4])
    awb_gains = None if awb_gains_value in (None, "") else parse_awb_gains(awb_gains_value)
    return CaptureSettings(
        count=int(data.get("count", 12)),
        shutter_us=int(data.get("shutter_us", 0)),
        gain=float(data.get("gain", 0.0)),
        awb=awb_mode,
        awb_gains=awb_gains,
        denoise=str(data.get("denoise", "cdn_fast")),
        quality=int(data.get("quality", 95)),
        width=_optional_int(data.get("width")),
        height=_optional_int(data.get("height")),
        metering=str(data.get("metering", "centre")),
        exposure=str(data.get("exposure", "normal")),
        ev=float(data.get("ev", 0.0)),
        brightness=float(data.get("brightness", 0.0)),
        contrast=float(data.get("contrast", 1.0)),
        saturation=float(data.get("saturation", 1.0)),
        sharpness=float(data.get("sharpness", 1.0)),
        tuning_file=_optional_str(data.get("tuning_file")),
        mode=_optional_str(data.get("mode")),
        hdr=str(data.get("hdr", "off")),
        nopreview=bool(data.get("nopreview", True)),
        immediate=bool(data.get("immediate", True)),
        raw=bool(data.get("raw", True)),
    )


def _parse_preview(data: dict[str, Any]) -> PreviewSettings:
    return PreviewSettings(
        width=int(data.get("width", 640)),
        height=int(data.get("height", 480)),
        framerate=int(data.get("framerate", 12)),
        quality=int(data.get("quality", 70)),
        stream_timeout_s=int(data.get("stream_timeout_s", 0)),
    )


def _parse_processing(data: dict[str, Any]) -> ProcessingSettings:
    return ProcessingSettings(
        stack_method=str(data.get("stack_method", "sigma")),
        sigma=float(data.get("sigma", 2.5)),
        min_frames=int(data.get("min_frames", 3)),
        save_intermediates=bool(data.get("save_intermediates", True)),
        max_working_edge=_optional_int(data.get("max_working_edge")),
        quality=parse_quality_thresholds(data.get("quality", {})),
    )


def parse_quality_thresholds(data: dict[str, Any]) -> QualityThresholds:
    defaults = QualityThresholds()
    if not data:
        return defaults
    return QualityThresholds(
        max_clip_fraction=float(data.get("max_clip_fraction", defaults.max_clip_fraction)),
        min_relative_focus=float(data.get("min_relative_focus", defaults.min_relative_focus)),
        min_median_focus=float(data.get("min_median_focus", defaults.min_median_focus)),
        min_mean_luma=float(data.get("min_mean_luma", defaults.min_mean_luma)),
        max_mean_luma=float(data.get("max_mean_luma", defaults.max_mean_luma)),
        min_alignment_score=float(data.get("min_alignment_score", defaults.min_alignment_score)),
        max_eval_clip_fraction=float(data.get("max_eval_clip_fraction", defaults.max_eval_clip_fraction)),
        min_mask_coverage=float(data.get("min_mask_coverage", defaults.min_mask_coverage)),
        max_mask_coverage=float(data.get("max_mask_coverage", defaults.max_mask_coverage)),
        min_pupil_iris_ratio=float(data.get("min_pupil_iris_ratio", defaults.min_pupil_iris_ratio)),
        max_pupil_iris_ratio=float(data.get("max_pupil_iris_ratio", defaults.max_pupil_iris_ratio)),
        min_iris_radius_fraction=float(data.get("min_iris_radius_fraction", defaults.min_iris_radius_fraction)),
        max_iris_radius_fraction=float(data.get("max_iris_radius_fraction", defaults.max_iris_radius_fraction)),
        max_center_offset_fraction=float(data.get("max_center_offset_fraction", defaults.max_center_offset_fraction)),
        max_edge_gain=float(data.get("max_edge_gain", defaults.max_edge_gain)),
        max_edge_gain_with_contrast=float(
            data.get("max_edge_gain_with_contrast", defaults.max_edge_gain_with_contrast)
        ),
        max_contrast_gain_for_edge=float(
            data.get("max_contrast_gain_for_edge", defaults.max_contrast_gain_for_edge)
        ),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None
