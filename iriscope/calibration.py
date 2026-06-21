from __future__ import annotations

import csv
import json
import math
import re
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .capture import build_rpicam_command, posix_join, run_ssh, shell_join
from .config import CalibrationSettings, CaptureSettings, ProjectConfig, QualityThresholds
from .processing import detect_iris_mask, load_image_float, quality_metrics


ProgressCallback = Callable[[dict[str, Any]], None]


CAPTURE_PROFILE_FIELDS = {
    "count",
    "shutter_us",
    "gain",
    "awb",
    "awb_gains",
    "denoise",
    "quality",
    "width",
    "height",
    "metering",
    "exposure",
    "ev",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "tuning_file",
    "mode",
    "hdr",
    "nopreview",
    "immediate",
    "raw",
}


@dataclass(frozen=True)
class RpicamMetadata:
    exposure_us: int | None = None
    analogue_gain: float | None = None
    awb_gains: tuple[float, float] | None = None
    focus_fom: float | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class CalibrationCandidateSpec:
    candidate_id: str
    label: str
    phase: str
    settings: CaptureSettings


@dataclass(frozen=True)
class CalibrationRunResult:
    run_id: str
    local_dir: Path
    remote_dir: str
    report_path: Path
    metrics_json: Path
    metrics_csv: Path
    recommendation: dict[str, Any]
    candidates: list[dict[str, Any]]
    warnings: list[str]
    precheck: dict[str, Any]


def run_auto_calibration(
    config: ProjectConfig,
    local_root: str | Path,
    progress: ProgressCallback | None = None,
) -> CalibrationRunResult:
    if not config.pi.host:
        raise ValueError("No Pi host configured in .iriscope.toml.")
    local_root_path = Path(local_root).expanduser().resolve()
    local_root_path.mkdir(parents=True, exist_ok=True)
    run_id = f"cal_{datetime.now():%Y%m%d_%H%M%S}"
    local_dir = bounded_run_dir(local_root_path, run_id)
    remote_dir = posix_join(config.pi.remote_root, "calibration-runs", run_id)
    settings = config.calibration
    warnings: list[str] = []

    _progress(progress, "precheck", 0.04, "Checking Pi camera tools and disk space.")
    precheck = _precheck(config, settings, remote_dir)
    if not precheck["camera_ok"]:
        raise RuntimeError(precheck["camera_message"])

    run_ssh(
        config.pi,
        f"mkdir -p {shlex.quote(remote_dir)}",
        timeout=settings.command_timeout_s,
    )

    _progress(progress, "auto_baseline", 0.12, "Capturing auto-exposure baseline.")
    baseline = build_calibration_candidates(config.capture, settings)[0]
    _capture_candidate(config, baseline, remote_dir, settings.command_timeout_s)
    baseline_metadata = _read_remote_metadata(config, remote_dir, baseline.candidate_id, settings.command_timeout_s)
    candidate_specs = build_calibration_candidates(config.capture, settings, baseline_metadata)

    for index, candidate in enumerate(candidate_specs[1:], start=1):
        phase = candidate.phase
        completed = 0.12 + (index / max(1, len(candidate_specs) - 1)) * 0.52
        _progress(progress, phase, completed, f"Capturing {candidate.label}.")
        _capture_candidate(config, candidate, remote_dir, settings.command_timeout_s)

    _progress(progress, "focus_geometry_check", 0.72, "Pulling frames and scoring candidate quality.")
    local_dir = _pull_remote_dir(config, remote_dir, local_root_path, settings.scp_timeout_s)
    if not _is_relative_to(local_dir, local_root_path):
        raise RuntimeError(f"Calibration artifacts escaped calibration root: {local_dir}")

    candidate_metrics = _analyze_candidates(local_dir, candidate_specs, config.processing.quality, settings)
    if not candidate_metrics:
        raise RuntimeError("Calibration did not produce any analyzable frames.")
    scored = score_candidate_metrics(candidate_metrics, settings, config.processing.quality)
    best = _best_candidate(scored)
    recommendation = build_recommendation(config.capture, best, scored, settings)
    warnings.extend(_run_warnings(scored, recommendation, settings))

    _progress(progress, "recommendation", 0.9, "Writing calibration report.")
    metrics_json = local_dir / "candidate_metrics.json"
    metrics_csv = local_dir / "candidate_metrics.csv"
    report_path = local_dir / "calibration_report.json"
    _write_metrics(metrics_json, metrics_csv, scored)
    artifacts = _copy_selected_artifacts(local_dir, best, scored)
    recommendation["artifacts"].update(artifacts)
    report = {
        "version": 1,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "remote_dir": remote_dir,
        "local_dir": str(local_dir),
        "precheck": precheck,
        "settings": asdict(settings),
        "previous_capture": capture_settings_to_profile(config.capture),
        "candidates": scored,
        "recommendation": recommendation,
        "warnings": warnings,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    _progress(
        progress,
        "complete",
        1.0,
        f"Recommendation ready: {recommendation['confidence']} confidence, score {recommendation['score']:.2f}.",
        candidates=scored,
        recommendation=recommendation,
        warnings=warnings,
        report_path=str(report_path),
    )
    return CalibrationRunResult(
        run_id=run_id,
        local_dir=local_dir,
        remote_dir=remote_dir,
        report_path=report_path,
        metrics_json=metrics_json,
        metrics_csv=metrics_csv,
        recommendation=recommendation,
        candidates=scored,
        warnings=warnings,
        precheck=precheck,
    )


def build_calibration_candidates(
    base: CaptureSettings,
    settings: CalibrationSettings,
    metadata: RpicamMetadata | dict[str, Any] | None = None,
) -> list[CalibrationCandidateSpec]:
    parsed = metadata if isinstance(metadata, RpicamMetadata) else parse_rpicam_metadata(metadata or {})
    awb_gains = parsed.awb_gains or base.awb_gains
    base_exposure = parsed.exposure_us if parsed.exposure_us and parsed.exposure_us > 0 else base.shutter_us
    if not base_exposure or base_exposure <= 0:
        base_exposure = 8000
    base_gain = parsed.analogue_gain if parsed.analogue_gain and parsed.analogue_gain > 0 else base.gain
    if not base_gain or base_gain <= 0:
        base_gain = 1.5

    candidates: list[CalibrationCandidateSpec] = []

    def add(label: str, phase: str, candidate_settings: CaptureSettings) -> None:
        if len(candidates) >= settings.sample_budget:
            return
        signature = _candidate_signature(candidate_settings)
        if any(_candidate_signature(item.settings) == signature for item in candidates):
            return
        candidate_id = f"candidate_{len(candidates):02d}_{_slug(label)}"
        candidates.append(CalibrationCandidateSpec(candidate_id, label, phase, candidate_settings))

    add(
        "auto baseline",
        "auto_baseline",
        _calibration_settings(base, shutter_us=0, gain=0.0, awb="auto", awb_gains=None),
    )

    for factor in (0.55, 0.75, 1.0, 1.25, 1.6, 2.1):
        shutter, gain = _split_exposure_product(float(base_exposure) * float(base_gain) * factor, settings)
        add(
            f"exposure {factor:g}x",
            "exposure_sweep",
            _calibration_settings(
                base,
                shutter_us=shutter,
                gain=gain,
                awb="manual" if awb_gains else "auto",
                awb_gains=awb_gains,
                metering="centre",
                exposure="normal",
                ev=0.0,
            ),
        )

    if awb_gains:
        shutter, gain = _split_exposure_product(float(base_exposure) * float(base_gain), settings)
        add(
            "awb lock",
            "awb_lock_test",
            _calibration_settings(base, shutter_us=shutter, gain=gain, awb="manual", awb_gains=awb_gains),
        )
    shutter, gain = _split_exposure_product(float(base_exposure) * float(base_gain), settings)
    add(
        "denoise off",
        "focus_geometry_check",
        _calibration_settings(base, shutter_us=shutter, gain=gain, awb="manual" if awb_gains else "auto", awb_gains=awb_gains, denoise="off"),
    )
    add(
        "denoise hq",
        "focus_geometry_check",
        _calibration_settings(base, shutter_us=shutter, gain=gain, awb="manual" if awb_gains else "auto", awb_gains=awb_gains, denoise="cdn_hq"),
    )
    return candidates


def parse_rpicam_metadata(data: dict[str, Any] | None) -> RpicamMetadata:
    if not data:
        return RpicamMetadata()
    exposure = _metadata_number(data, "ExposureTime", "exposure_time", "exposure_us", "SensorExposureTime")
    gain = _metadata_number(data, "AnalogueGain", "analog_gain", "analogue_gain", "SensorAnalogueGain")
    focus = _metadata_number(data, "FocusFoM", "FocusFOM", "focus_fom", "focus")
    colour = _metadata_value(data, "ColourGains", "ColorGains", "awb_gains", "AwbGains")
    exposure_us = _normalise_exposure_us(exposure)
    awb_gains = _normalise_awb_gains(colour)
    present = sum(value is not None for value in (exposure_us, gain, awb_gains, focus))
    return RpicamMetadata(
        exposure_us=exposure_us,
        analogue_gain=float(gain) if gain is not None else None,
        awb_gains=awb_gains,
        focus_fom=float(focus) if focus is not None else None,
        confidence=present / 4.0,
    )


def score_candidate_metrics(
    metrics: list[dict[str, Any]],
    settings: CalibrationSettings,
    thresholds: QualityThresholds | None = None,
) -> list[dict[str, Any]]:
    thresholds = thresholds or QualityThresholds()
    max_focus = max((float(item.get("focus_score") or 0.0) for item in metrics), default=0.0)
    weights = asdict(settings.weights)
    total_weight = max(sum(float(value) for value in weights.values()), 1e-9)
    scored: list[dict[str, Any]] = []
    for item in metrics:
        gain = float(item.get("analogue_gain") or item["settings"].get("gain") or settings.min_gain)
        components = {
            "luma": _luma_score(float(item.get("mean_luma") or 0.0), settings),
            "clipping": _clipping_score(float(item.get("clip_fraction") or 0.0), settings),
            "focus": _focus_score(float(item.get("focus_score") or 0.0), max_focus),
            "mask": _mask_score(item, thresholds),
            "color": _color_score(float(item.get("channel_balance") or 0.0)),
            "gain": _gain_score(gain, settings),
            "metadata": _clamp(float(item.get("metadata_confidence") or 0.0), 0.0, 1.0),
        }
        score = sum(components[name] * float(weights[name]) for name in components) / total_weight
        clean = dict(item)
        clean["component_scores"] = {name: round(value, 4) for name, value in components.items()}
        clean["score"] = round(float(score), 4)
        scored.append(clean)
    return scored


def build_recommendation(
    current: CaptureSettings,
    best: dict[str, Any],
    candidates: list[dict[str, Any]],
    settings: CalibrationSettings,
) -> dict[str, Any]:
    best_settings = capture_settings_from_profile(best["settings"])
    exposure_us = int(best.get("exposure_us") or best_settings.shutter_us or 0)
    gain = float(best.get("analogue_gain") or best_settings.gain or 0.0)
    if exposure_us > 0:
        exposure_us = int(_clamp(exposure_us, settings.min_shutter_us, settings.max_shutter_us))
    if gain > 0:
        gain = _clamp(gain, settings.min_gain, settings.max_gain)
    awb_gains = _normalise_awb_gains(best.get("awb_gains")) or best_settings.awb_gains
    awb = "manual" if awb_gains else best_settings.awb
    recommended = replace(
        current,
        shutter_us=exposure_us,
        gain=round(gain, 3) if gain > 0 else gain,
        awb=awb,
        awb_gains=awb_gains,
        denoise=best_settings.denoise,
        metering=best_settings.metering,
        exposure=best_settings.exposure,
        ev=0.0,
    )
    confidence = _recommendation_confidence(best, settings)
    current_profile = capture_settings_to_profile(current)
    recommended_profile = capture_settings_to_profile(recommended)
    return {
        "candidate_id": best["candidate_id"],
        "label": best["label"],
        "score": float(best["score"]),
        "confidence": confidence,
        "capture": recommended_profile,
        "settings_diff": _settings_diff(current_profile, recommended_profile),
        "quality": {
            "mean_luma": best.get("mean_luma"),
            "clip_fraction": best.get("clip_fraction"),
            "focus_score": best.get("focus_score"),
            "mask_coverage": best.get("mask_coverage"),
            "geometry_confidence": best.get("geometry_confidence"),
            "rank": 1,
            "candidate_count": len(candidates),
        },
        "artifacts": {
            "best_frame": best.get("file"),
            "best_thumbnail": best.get("thumbnail"),
            "report": None,
        },
        "reasons": _recommendation_reasons(best),
    }


def capture_settings_to_profile(settings: CaptureSettings) -> dict[str, Any]:
    data = asdict(settings)
    data["awb_gains"] = list(settings.awb_gains) if settings.awb_gains is not None else None
    return data


def capture_settings_from_profile(profile: dict[str, Any]) -> CaptureSettings:
    clean = {key: profile[key] for key in CAPTURE_PROFILE_FIELDS if key in profile}
    awb_gains = clean.get("awb_gains")
    if awb_gains is not None:
        clean["awb_gains"] = (float(awb_gains[0]), float(awb_gains[1]))
    return CaptureSettings(**clean)


def bounded_run_dir(root: Path, run_id: str) -> Path:
    clean = _slug(run_id)
    if not clean:
        raise ValueError("Calibration run id is empty.")
    candidate = (root / clean).resolve()
    if not _is_relative_to(candidate, root):
        raise ValueError(f"Calibration run path escapes root: {candidate}")
    return candidate


def _precheck(config: ProjectConfig, settings: CalibrationSettings, remote_dir: str) -> dict[str, Any]:
    camera = run_ssh(config.pi, "rpicam-hello --list-cameras", timeout=min(settings.command_timeout_s, 15))
    camera_text = (camera.stdout + camera.stderr).strip()
    version_text = ""
    try:
        version = run_ssh(config.pi, "rpicam-still --version", timeout=min(settings.command_timeout_s, 10))
        version_text = (version.stdout + version.stderr).strip()
    except Exception as exc:
        version_text = str(exc)
    disk_text = ""
    try:
        remote_parent = shlex.quote(posix_join(remote_dir, ".."))
        disk = run_ssh(config.pi, f"df -Pk {remote_parent} 2>/dev/null || df -Pk \"$HOME\"", timeout=10)
        disk_text = disk.stdout.strip()
    except Exception as exc:
        disk_text = str(exc)
    camera_ok = "no cameras" not in camera_text.lower() and ("imx" in camera_text.lower() or "available cameras" in camera_text.lower())
    return {
        "camera_ok": camera_ok,
        "camera_message": camera_text[:4000] or "No camera list output returned.",
        "rpicam_still_version": version_text[:1000],
        "disk": disk_text[:1000],
    }


def _capture_candidate(
    config: ProjectConfig,
    candidate: CalibrationCandidateSpec,
    remote_dir: str,
    timeout: int,
) -> None:
    command = build_rpicam_command(
        output_file=f"{candidate.candidate_id}.jpg",
        metadata_file=f"{candidate.candidate_id}.json",
        settings=candidate.settings,
    )
    remote_command = f"cd {shlex.quote(remote_dir)} && {shell_join(command)}"
    run_ssh(config.pi, remote_command, timeout=timeout)


def _read_remote_metadata(
    config: ProjectConfig,
    remote_dir: str,
    candidate_id: str,
    timeout: int,
) -> RpicamMetadata:
    try:
        completed = run_ssh(
            config.pi,
            f"cat {shlex.quote(posix_join(remote_dir, f'{candidate_id}.json'))}",
            timeout=min(timeout, 10),
        )
        return parse_rpicam_metadata(json.loads(completed.stdout or "{}"))
    except Exception:
        return RpicamMetadata()


def _pull_remote_dir(config: ProjectConfig, remote_dir: str, local_root: Path, timeout: int) -> Path:
    local_root.mkdir(parents=True, exist_ok=True)
    destination = (local_root / Path(remote_dir).name).resolve()
    if destination.exists():
        shutil.rmtree(destination)
    args = [
        "scp",
        "-P",
        str(config.pi.port),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-r",
    ]
    if config.pi.ssh_key:
        args += ["-i", config.pi.ssh_key]
    args += [f"{config.pi.target}:{remote_dir}", str(local_root)]
    try:
        subprocess.run(args, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(stderr or f"SCP failed with exit code {exc.returncode}") from exc
    return destination


def _analyze_candidates(
    local_dir: Path,
    candidates: list[CalibrationCandidateSpec],
    thresholds: QualityThresholds,
    settings: CalibrationSettings,
) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for candidate in candidates:
        image_path = local_dir / f"{candidate.candidate_id}.jpg"
        metadata_path = local_dir / f"{candidate.candidate_id}.json"
        if not image_path.exists():
            continue
        metadata = _load_metadata(metadata_path)
        parsed = parse_rpicam_metadata(metadata)
        image = load_image_float(image_path)
        analysis_image = _resize_for_analysis(image)
        frame_quality = quality_metrics(analysis_image)
        _, mask_report = detect_iris_mask(analysis_image)
        thumbnail = _write_thumbnail(image_path, settings.thumbnail_edge) if settings.retain_artifacts else None
        channel_balance = _channel_balance(analysis_image)
        coverage = float(mask_report.get("coverage") or 0.0)
        geometry = _geometry_metrics(analysis_image, mask_report, thresholds, channel_balance)
        metrics.append(
            {
                "candidate_id": candidate.candidate_id,
                "label": candidate.label,
                "phase": candidate.phase,
                "settings": capture_settings_to_profile(candidate.settings),
                "file": str(image_path),
                "metadata_file": str(metadata_path) if metadata_path.exists() else None,
                "thumbnail": str(thumbnail) if thumbnail else None,
                "focus_score": round(float(frame_quality["focus_score"]), 4),
                "mean_luma": round(float(frame_quality["mean_luma"]), 4),
                "clip_fraction": round(float(frame_quality["clip_fraction"]), 6),
                "channel_balance": round(channel_balance, 4),
                "mask_method": mask_report.get("method"),
                "mask_coverage": round(coverage, 4),
                "pupil_to_iris_ratio": round(geometry["pupil_to_iris_ratio"], 4),
                "iris_radius_fraction": round(geometry["iris_radius_fraction"], 4),
                "center_offset_fraction": round(geometry["center_offset_fraction"], 4),
                "geometry_confidence": "high" if geometry["ready"] else "low",
                "exposure_us": parsed.exposure_us,
                "analogue_gain": parsed.analogue_gain,
                "awb_gains": list(parsed.awb_gains) if parsed.awb_gains is not None else None,
                "focus_fom": parsed.focus_fom,
                "metadata_confidence": parsed.confidence,
                "warnings": _candidate_warnings(frame_quality, mask_report, settings, thresholds, parsed, channel_balance),
            }
        )
    return metrics


def _write_metrics(metrics_json: Path, metrics_csv: Path, candidates: list[dict[str, Any]]) -> None:
    metrics_json.write_text(json.dumps(candidates, indent=2, sort_keys=True), encoding="utf-8")
    fields = [
        "candidate_id",
        "label",
        "phase",
        "score",
        "mean_luma",
        "clip_fraction",
        "focus_score",
        "mask_coverage",
        "geometry_confidence",
        "exposure_us",
        "analogue_gain",
        "metadata_confidence",
    ]
    with metrics_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in candidates:
            writer.writerow({field: item.get(field) for field in fields})


def _copy_selected_artifacts(local_dir: Path, best: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    best_file = Path(str(best.get("file") or ""))
    if best_file.exists() and _is_relative_to(best_file, local_dir):
        target = local_dir / "selected_best.jpg"
        shutil.copy2(best_file, target)
        selected["selected_best_frame"] = str(target)
    baseline = next((item for item in candidates if item["phase"] == "auto_baseline"), None)
    if baseline:
        baseline_file = Path(str(baseline.get("file") or ""))
        if baseline_file.exists() and _is_relative_to(baseline_file, local_dir):
            target = local_dir / "selected_baseline.jpg"
            shutil.copy2(baseline_file, target)
            selected["baseline_frame"] = str(target)
            selected["baseline_thumbnail"] = baseline.get("thumbnail")
    selected["report"] = str(local_dir / "calibration_report.json")
    selected["metrics_json"] = str(local_dir / "candidate_metrics.json")
    selected["metrics_csv"] = str(local_dir / "candidate_metrics.csv")
    return selected


def _best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        candidates,
        key=lambda item: (
            float(item.get("score") or 0.0),
            float(item.get("focus_score") or 0.0),
            -float(item.get("clip_fraction") or 0.0),
            -float(item.get("analogue_gain") or item["settings"].get("gain") or 0.0),
        ),
        reverse=True,
    )[0]


def _run_warnings(
    candidates: list[dict[str, Any]],
    recommendation: dict[str, Any],
    settings: CalibrationSettings,
) -> list[str]:
    warnings: list[str] = []
    if recommendation["confidence"] == "low":
        warnings.append("Calibration confidence is low; review framing and lighting before applying.")
    if recommendation["quality"].get("geometry_confidence") == "low":
        warnings.append("Iris geometry confidence is low. Exposure and focus recommendations were produced, but reframe the eye before stack capture.")
    if float(recommendation["quality"].get("clip_fraction") or 0.0) > settings.max_clip_fraction:
        warnings.append("Best candidate still exceeds the configured clipping limit.")
    if all(float(item.get("metadata_confidence") or 0.0) < 0.5 for item in candidates):
        warnings.append("rpicam metadata was incomplete; recommendation relies mainly on image analysis.")
    return warnings


def _recommendation_reasons(best: dict[str, Any]) -> list[str]:
    components = best.get("component_scores") or {}
    ranked = sorted(components.items(), key=lambda item: item[1], reverse=True)
    reasons = [f"{name} score {value:.2f}" for name, value in ranked[:3]]
    if best.get("geometry_confidence") == "low":
        reasons.append("geometry confidence low")
    return reasons


def _recommendation_confidence(best: dict[str, Any], settings: CalibrationSettings) -> str:
    score = float(best.get("score") or 0.0)
    clip = float(best.get("clip_fraction") or 0.0)
    geometry = best.get("geometry_confidence")
    luma = float(best.get("mean_luma") or 0.0)
    exposure_limited = (
        float(best.get("analogue_gain") or best["settings"].get("gain") or 0.0) >= settings.max_gain
        and int(best.get("exposure_us") or best["settings"].get("shutter_us") or 0) >= settings.max_shutter_us
    )
    if exposure_limited and luma < settings.target_luma_min:
        return "low"
    if geometry == "low":
        return "low" if score < 0.7 else "medium"
    if score >= 0.76 and clip <= settings.max_clip_fraction and geometry != "low":
        return "high"
    if score >= 0.56:
        return "medium"
    return "low"


def _settings_diff(current: dict[str, Any], recommended: dict[str, Any]) -> list[dict[str, Any]]:
    fields = ["shutter_us", "gain", "awb", "awb_gains", "denoise", "metering", "exposure", "ev"]
    diff = []
    for field in fields:
        if current.get(field) != recommended.get(field):
            diff.append({"field": field, "before": current.get(field), "after": recommended.get(field)})
    return diff


def _calibration_settings(base: CaptureSettings, **patch: Any) -> CaptureSettings:
    values = {
        "count": 1,
        "raw": False,
        "nopreview": True,
        "immediate": True,
        "quality": max(92, int(base.quality)),
    }
    values.update(patch)
    return replace(base, **values)


def _split_exposure_product(product: float, settings: CalibrationSettings) -> tuple[int, float]:
    product = max(product, settings.min_shutter_us * settings.min_gain)
    shutter = int(round(_clamp(product / settings.min_gain, settings.min_shutter_us, settings.max_shutter_us)))
    gain = product / max(shutter, 1)
    if gain > settings.max_gain:
        gain = settings.max_gain
        shutter = int(round(_clamp(product / gain, settings.min_shutter_us, settings.max_shutter_us)))
    return shutter, round(float(_clamp(gain, settings.min_gain, settings.max_gain)), 3)


def _candidate_signature(settings: CaptureSettings) -> tuple[Any, ...]:
    return (
        settings.shutter_us,
        round(settings.gain, 3),
        settings.awb,
        tuple(round(value, 3) for value in settings.awb_gains) if settings.awb_gains else None,
        settings.denoise,
        settings.metering,
        settings.exposure,
        round(settings.ev, 3),
    )


def _load_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_thumbnail(path: Path, edge: int) -> Path:
    from PIL import Image

    thumbnail_dir = path.parent / "thumbnails"
    thumbnail_dir.mkdir(exist_ok=True)
    target = thumbnail_dir / path.name
    with Image.open(path) as image:
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        image.convert("RGB").save(target, quality=88)
    return target


def _resize_for_analysis(image: Any, max_edge: int = 1200) -> Any:
    height, width = image.shape[:2]
    edge = max(height, width)
    if edge <= max_edge:
        return image
    try:
        import cv2

        scale = max_edge / float(edge)
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        return cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    except Exception:
        return image


def _channel_balance(image: Any) -> float:
    try:
        import numpy as np

        means = np.mean(image.reshape(-1, image.shape[-1]), axis=0)
        mean = float(np.mean(means))
        if mean <= 1e-9:
            return 0.0
        return float(_clamp(1.0 - (float(np.std(means)) / mean), 0.0, 1.0))
    except Exception:
        return 0.0


def _geometry_metrics(
    image: Any,
    mask_report: dict[str, Any],
    thresholds: QualityThresholds,
    channel_balance: float,
) -> dict[str, Any]:
    height, width = image.shape[:2]
    min_dim = max(1.0, float(min(height, width)))
    center = mask_report.get("center") or [width / 2.0, height / 2.0]
    center_offset = math.hypot((float(center[0]) - width / 2.0) / width, (float(center[1]) - height / 2.0) / height)
    radius = float(mask_report.get("radius") or 0.0)
    pupil_radius = float(mask_report.get("pupil_radius") or 0.0)
    return _geometry_metrics_from_values(
        method=str(mask_report.get("method") or ""),
        coverage=float(mask_report.get("coverage") or 0.0),
        pupil_to_iris_ratio=pupil_radius / radius if radius > 1e-12 else 0.0,
        iris_radius_fraction=radius / min_dim,
        center_offset_fraction=center_offset,
        thresholds=thresholds,
        channel_balance=channel_balance,
    )


def _geometry_metrics_from_report(
    mask_report: dict[str, Any],
    thresholds: QualityThresholds,
    channel_balance: float,
) -> dict[str, Any]:
    radius = float(mask_report.get("radius") or 0.0)
    pupil_radius = float(mask_report.get("pupil_radius") or 0.0)
    return _geometry_metrics_from_values(
        method=str(mask_report.get("method") or ""),
        coverage=float(mask_report.get("coverage") or 0.0),
        pupil_to_iris_ratio=pupil_radius / radius if radius > 1e-12 else 0.0,
        iris_radius_fraction=0.3,
        center_offset_fraction=0.0,
        thresholds=thresholds,
        channel_balance=channel_balance,
    )


def _geometry_metrics_from_values(
    method: str,
    coverage: float,
    pupil_to_iris_ratio: float,
    iris_radius_fraction: float,
    center_offset_fraction: float,
    thresholds: QualityThresholds,
    channel_balance: float,
) -> dict[str, Any]:
    ready = (
        method != "fallback_circle"
        and thresholds.min_mask_coverage <= coverage <= thresholds.max_mask_coverage
        and thresholds.min_pupil_iris_ratio <= pupil_to_iris_ratio <= thresholds.max_pupil_iris_ratio
        and thresholds.min_iris_radius_fraction <= iris_radius_fraction <= thresholds.max_iris_radius_fraction
        and center_offset_fraction <= thresholds.max_center_offset_fraction
        and channel_balance >= 0.45
    )
    return {
        "ready": ready,
        "pupil_to_iris_ratio": float(pupil_to_iris_ratio),
        "iris_radius_fraction": float(iris_radius_fraction),
        "center_offset_fraction": float(center_offset_fraction),
    }


def _candidate_warnings(
    frame_quality: dict[str, float],
    mask_report: dict[str, Any],
    settings: CalibrationSettings,
    thresholds: QualityThresholds,
    metadata: RpicamMetadata,
    channel_balance: float,
) -> list[str]:
    warnings: list[str] = []
    if frame_quality["clip_fraction"] > settings.max_clip_fraction:
        warnings.append("clipping_above_target")
    if not settings.target_luma_min <= frame_quality["mean_luma"] <= settings.target_luma_max:
        warnings.append("luma_outside_target")
    geometry = _geometry_metrics_from_report(mask_report, thresholds, channel_balance)
    if not geometry["ready"]:
        warnings.append("geometry_low_confidence")
    if metadata.confidence < 0.5:
        warnings.append("metadata_incomplete")
    return warnings


def _luma_score(value: float, settings: CalibrationSettings) -> float:
    if settings.target_luma_min <= value <= settings.target_luma_max:
        return 1.0
    center = (settings.target_luma_min + settings.target_luma_max) / 2.0
    span = max(settings.target_luma_max - settings.target_luma_min, 1e-6)
    return _clamp(1.0 - abs(value - center) / (span * 1.8), 0.0, 1.0)


def _clipping_score(value: float, settings: CalibrationSettings) -> float:
    if value <= settings.max_clip_fraction:
        return 1.0
    return _clamp(1.0 - (value - settings.max_clip_fraction) / max(settings.max_clip_fraction * 4.0, 1e-6), 0.0, 1.0)


def _focus_score(value: float, max_focus: float) -> float:
    if max_focus <= 1e-9:
        return 0.0
    return _clamp(value / max_focus, 0.0, 1.0)


def _mask_score(item: dict[str, Any], thresholds: QualityThresholds) -> float:
    if item.get("geometry_confidence") == "low":
        return 0.35 if float(item.get("mask_coverage") or 0.0) > 0 else 0.0
    coverage = float(item.get("mask_coverage") or 0.0)
    if item.get("mask_method") == "fallback_circle":
        return 0.45 if coverage > 0 else 0.0
    if thresholds.min_mask_coverage <= coverage <= thresholds.max_mask_coverage:
        return 1.0
    target = (thresholds.min_mask_coverage + thresholds.max_mask_coverage) / 2.0
    span = max(thresholds.max_mask_coverage - thresholds.min_mask_coverage, 1e-6)
    return _clamp(1.0 - abs(coverage - target) / span, 0.0, 0.8)


def _color_score(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


def _gain_score(value: float, settings: CalibrationSettings) -> float:
    if value <= max(settings.min_gain, 2.0):
        return 1.0
    return _clamp(1.0 - (value - 2.0) / max(settings.max_gain - 2.0, 1e-6), 0.0, 1.0)


def _metadata_number(data: dict[str, Any], *names: str) -> float | None:
    value = _metadata_value(data, *names)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metadata_value(data: Any, *names: str) -> Any:
    targets = {_normalise_key(name) for name in names}
    if isinstance(data, dict):
        for key, value in data.items():
            if _normalise_key(str(key)) in targets:
                return value
        for value in data.values():
            nested = _metadata_value(value, *names)
            if nested is not None:
                return nested
    elif isinstance(data, list):
        for value in data:
            nested = _metadata_value(value, *names)
            if nested is not None:
                return nested
    return None


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _normalise_exposure_us(value: float | None) -> int | None:
    if value is None or value <= 0:
        return None
    if value < 1.0:
        return int(round(value * 1_000_000))
    return int(round(value))


def _normalise_awb_gains(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return None
    if len(parts) != 2:
        return None
    try:
        return (round(float(parts[0]), 4), round(float(parts[1]), 4))
    except (TypeError, ValueError):
        return None


def _progress(
    callback: ProgressCallback | None,
    phase: str,
    progress: float,
    message: str,
    **extra: Any,
) -> None:
    if callback is None:
        return
    callback({"phase": phase, "progress": round(progress, 4), "message": message, **extra})


def _slug(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip().lower()).strip("._-")
    return clean or "run"


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
