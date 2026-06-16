from __future__ import annotations

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
    shutter_us: int = 8000
    gain: float = 1.0
    awb_gains: tuple[float, float] = (1.8, 1.4)
    denoise: str = "off"
    quality: int = 95
    width: int | None = None
    height: int | None = None
    nopreview: bool = True
    immediate: bool = True
    raw: bool = True


@dataclass(frozen=True)
class ProcessingSettings:
    stack_method: str = "sigma"
    sigma: float = 2.5
    min_frames: int = 3
    save_intermediates: bool = True
    max_working_edge: int | None = None


@dataclass(frozen=True)
class ProjectConfig:
    pi: PiConfig = PiConfig()
    capture: CaptureSettings = CaptureSettings()
    processing: ProcessingSettings = ProcessingSettings()


def load_config(path: str | Path | None = None) -> ProjectConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return ProjectConfig()
    if tomllib is None:
        raise RuntimeError("TOML config files require Python 3.11+ or the `tomli` package.")
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return ProjectConfig(
        pi=_parse_pi(data.get("pi", {})),
        capture=_parse_capture(data.get("capture", {})),
        processing=_parse_processing(data.get("processing", {})),
    )


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


def _parse_capture(data: dict[str, Any]) -> CaptureSettings:
    awb = data.get("awb_gains", [1.8, 1.4])
    return CaptureSettings(
        count=int(data.get("count", 12)),
        shutter_us=int(data.get("shutter_us", 8000)),
        gain=float(data.get("gain", 1.0)),
        awb_gains=parse_awb_gains(awb),
        denoise=str(data.get("denoise", "off")),
        quality=int(data.get("quality", 95)),
        width=_optional_int(data.get("width")),
        height=_optional_int(data.get("height")),
        nopreview=bool(data.get("nopreview", True)),
        immediate=bool(data.get("immediate", True)),
        raw=bool(data.get("raw", True)),
    )


def _parse_processing(data: dict[str, Any]) -> ProcessingSettings:
    return ProcessingSettings(
        stack_method=str(data.get("stack_method", "sigma")),
        sigma=float(data.get("sigma", 2.5)),
        min_frames=int(data.get("min_frames", 3)),
        save_intermediates=bool(data.get("save_intermediates", True)),
        max_working_edge=_optional_int(data.get("max_working_edge")),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
