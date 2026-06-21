from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .capture import (
    build_rpicam_command,
    capture_remote_calibration,
    capture_remote_session,
    shell_join,
    verify_remote_camera,
)
from .config import (
    CaptureSettings,
    ProcessingSettings,
    load_config,
    merge_capture,
    merge_pi,
    merge_processing,
)
from .evaluation import evaluate_dataset
from .processing import process_session
from .review import generate_review


CONFIG_TEMPLATE = """[pi]
host = "raspberrypi.local"
user = "pi"
port = 22
remote_root = "/home/pi/iriscope"
# ssh_key = "C:/Users/you/.ssh/id_ed25519"

[capture]
count = 12
shutter_us = 0
gain = 0
awb = "auto"
awb_gains = [3.2, 1.4]
denoise = "cdn_fast"
quality = 95
metering = "centre"
exposure = "normal"
ev = 0
brightness = 0
contrast = 1
saturation = 1
sharpness = 1
# tuning_file = "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json"
hdr = "off"
nopreview = true

[preview]
width = 640
height = 480
framerate = 12
quality = 70
stream_timeout_s = 0

[calibration]
target_luma_min = 0.38
target_luma_max = 0.58
max_clip_fraction = 0.03
sample_budget = 10
retain_artifacts = true
thumbnail_edge = 360
min_shutter_us = 800
max_shutter_us = 30000
min_gain = 1
max_gain = 8
command_timeout_s = 60
scp_timeout_s = 60

[calibration.weights]
luma = 0.28
clipping = 0.20
focus = 0.18
mask = 0.14
color = 0.08
gain = 0.07
metadata = 0.05

[processing]
stack_method = "sigma"
sigma = 2.5
min_frames = 3
save_intermediates = true

[processing.quality]
max_clip_fraction = 0.20
min_relative_focus = 0.35
min_median_focus = 10.0
min_mean_luma = 0.02
max_mean_luma = 0.98
min_alignment_score = 0.55
max_eval_clip_fraction = 0.35
min_mask_coverage = 0.06
max_mask_coverage = 0.48
min_pupil_iris_ratio = 0.18
max_pupil_iris_ratio = 0.68
min_iris_radius_fraction = 0.16
max_iris_radius_fraction = 0.55
max_center_offset_fraction = 0.28
max_edge_gain = 7.0
max_edge_gain_with_contrast = 5.5
max_contrast_gain_for_edge = 3.0
"""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        _error(f"Command failed with exit code {exc.returncode}: {' '.join(map(str, exc.cmd))}")
        if exc.stdout:
            _print(exc.stdout.strip())
        if exc.stderr:
            _error(exc.stderr.strip())
        return exc.returncode or 1
    except Exception as exc:
        _error(str(exc))
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iriscope",
        description="Capture and locally enhance Raspberry Pi HQ camera iris image stacks.",
    )
    parser.add_argument("--config", default=".iriscope.toml", help="Path to TOML config file.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_config = sub.add_parser("init-config", help="Write a starter .iriscope.toml.")
    init_config.add_argument("--output", default=".iriscope.toml", help="Destination config path.")
    init_config.add_argument("--force", action="store_true", help="Overwrite an existing config file.")
    init_config.set_defaults(func=_cmd_init_config)

    calibrate = sub.add_parser("calibrate", help="Print or run Pi camera calibration checks.")
    _add_pi_options(calibrate)
    _add_capture_options(calibrate, include_count=False)
    calibrate.set_defaults(func=_cmd_calibrate)

    capture = sub.add_parser("capture", help="Capture a DNG/JPEG stack on the Pi and optionally pull it.")
    capture.add_argument("--subject", required=True, help="Subject/session identifier, e.g. S001.")
    capture.add_argument("--eye", required=True, choices=["left", "right"], help="Eye to capture.")
    capture.add_argument("--local-dir", default="captures", help="Local parent directory for pulled sessions.")
    capture.add_argument("--pull", action=argparse.BooleanOptionalAction, default=True, help="Pull session after capture.")
    _add_pi_options(capture)
    _add_capture_options(capture, include_count=True)
    capture.set_defaults(func=_cmd_capture)

    process = sub.add_parser("process", help="Process a local session folder.")
    process.add_argument("session_dir", help="Session folder containing DNG/JPEG/TIFF/PNG frames.")
    process.add_argument("--output-dir", help="Output directory. Defaults to SESSION/processed.")
    process.add_argument("--dark", help="Optional dark-frame image.")
    process.add_argument("--flat", help="Optional flat-field image.")
    _add_processing_options(process)
    process.set_defaults(func=_cmd_process)

    review = sub.add_parser("review", help="Generate a local HTML review page for a processed session.")
    review.add_argument("session_dir", help="Session folder or processed folder.")
    review.add_argument("--open", action="store_true", help="Open review.html in the default browser.")
    review.set_defaults(func=_cmd_review)

    eval_dataset = sub.add_parser("eval-dataset", help="Evaluate processing on a local iris image dataset.")
    eval_dataset.add_argument("dataset_dir", help="Dataset root containing subject/eye image folders.")
    eval_dataset.add_argument("--output-dir", help="Evaluation output directory. Defaults to DATASET_PARENT/eval-runs/...")
    eval_dataset.add_argument("--limit", type=int, default=40, help="Maximum subject/eye folders to process. Use 0 for all.")
    eval_dataset.add_argument("--offset", type=int, default=0, help="Skip this many discovered folders before evaluating.")
    eval_dataset.add_argument("--min-images", type=int, default=3, help="Minimum images required for a folder to be evaluated.")
    _add_processing_options(eval_dataset)
    eval_dataset.set_defaults(func=_cmd_eval_dataset)

    web = sub.add_parser("web", help="Run the local Iriscope API for the Vite web GUI.")
    web.add_argument("--host", default="127.0.0.1", help="API bind host.")
    web.add_argument("--port", default=8765, type=int, help="API bind port.")
    web.add_argument("--reload", action="store_true", help="Reload the API server on code changes.")
    web.set_defaults(func=_cmd_web)
    return parser


def _cmd_init_config(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and not args.force:
        raise FileExistsError(f"{output} already exists. Use --force to overwrite it.")
    output.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    _print(f"Wrote {output}")
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pi = _pi_from_args(args, config.pi)
    capture = _capture_from_args(args, config.capture)
    example = shell_join(build_rpicam_command("test.jpg", capture, "test.json"))

    if not pi.host:
        _print("No Pi host configured. Add [pi].host to .iriscope.toml or pass --host.")
        _print("")
        _print("Run this on the Pi to verify the camera:")
        _print("  rpicam-hello --list-cameras")
        _print("")
        _print("Then test capture with:")
        _print(f"  {example}")
        return 0

    _print(f"Checking camera on {pi.target}...")
    camera_list = verify_remote_camera(pi)
    _print(camera_list or "No camera list output returned.")
    remote_dir = capture_remote_calibration(pi, capture)
    _print(f"Captured test files on Pi under {remote_dir}")
    return 0


def _cmd_capture(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pi = _pi_from_args(args, config.pi)
    capture = _capture_from_args(args, config.capture)
    if not pi.host:
        raise ValueError("Pi host is required for capture. Set [pi].host or pass --host.")
    _print(f"Capturing {capture.count} frames on {pi.target}...")
    result = capture_remote_session(
        pi=pi,
        subject=args.subject,
        eye=args.eye,
        settings=capture,
        local_parent=args.local_dir,
        pull=bool(args.pull),
    )
    _print(f"Remote session: {result.remote_dir}")
    if result.local_dir:
        _print(f"Pulled session: {result.local_dir}")
    return 0


def _cmd_process(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    settings = _processing_from_args(args, config.processing)
    _print(f"Processing {args.session_dir}...")
    result = process_session(
        args.session_dir,
        output_dir=args.output_dir,
        settings=settings,
        dark_path=args.dark,
        flat_path=args.flat,
    )
    _print(f"Enhanced JPEG: {result.enhanced_jpg}")
    _print(f"Enhanced TIFF: {result.enhanced_tif}")
    _print(f"Report: {result.report_json}")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    path = generate_review(args.session_dir, open_browser=bool(args.open))
    _print(f"Review page: {path}")
    return 0


def _cmd_eval_dataset(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    settings = _processing_from_args(args, config.processing)
    limit = None if int(args.limit) == 0 else int(args.limit)
    _print(f"Evaluating dataset under {args.dataset_dir}...")
    result = evaluate_dataset(
        args.dataset_dir,
        output_dir=args.output_dir,
        settings=settings,
        limit=limit,
        min_images=int(args.min_images),
        offset=max(0, int(args.offset)),
    )
    _print(f"Processed sessions: {result.processed_count}")
    _print(f"Passed heuristic checks: {result.passed_count}")
    _print(f"Summary JSON: {result.summary_json}")
    _print(f"Summary CSV: {result.summary_csv}")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise RuntimeError("The web API requires `pip install -e .[web]`.") from exc
    if not _is_loopback_bind(args.host) and not os.environ.get("IRISCOPE_ADMIN_TOKEN"):
        raise RuntimeError(
            "Binding the Iriscope API to a non-loopback host requires IRISCOPE_ADMIN_TOKEN. "
            "Use --host 127.0.0.1 for local-only access."
        )
    _print(f"Starting Iriscope API at http://{args.host}:{args.port}")
    uvicorn.run(
        "iriscope.web_api:app",
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
        log_level="info",
    )
    return 0


def _is_loopback_bind(host: str) -> bool:
    clean = host.strip().lower()
    return clean in {"localhost", "127.0.0.1", "::1"}


def _add_pi_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="Pi hostname or IP address.")
    parser.add_argument("--user", help="SSH username.")
    parser.add_argument("--port", type=int, help="SSH port.")
    parser.add_argument("--remote-root", help="Remote capture root on the Pi.")
    parser.add_argument("--ssh-key", help="SSH private key path.")


def _add_capture_options(parser: argparse.ArgumentParser, include_count: bool) -> None:
    if include_count:
        parser.add_argument("--count", type=int, help="Number of frames to capture.")
    parser.add_argument("--shutter", type=int, dest="shutter_us", help="Manual shutter time in microseconds. Use 0 for auto exposure.")
    parser.add_argument("--gain", type=float, help="Manual analogue gain. Use 0 for auto gain.")
    parser.add_argument("--iso", type=float, dest="iso_equivalent", help="ISO-equivalent analogue gain. ISO 400 maps to gain 4.")
    parser.add_argument(
        "--awb",
        choices=["auto", "incandescent", "tungsten", "fluorescent", "indoor", "daylight", "cloudy", "custom", "manual"],
        help="rpicam AWB mode. Use manual with --awb-gains.",
    )
    parser.add_argument("--awb-gains", help="Fixed AWB gains as red,blue, for example 3.2,1.4.")
    parser.add_argument("--denoise", choices=["auto", "off", "cdn_off", "cdn_fast", "cdn_hq"], help="rpicam denoise mode.")
    parser.add_argument("--quality", type=int, help="JPEG quality for preview files.")
    parser.add_argument("--width", type=int, help="Optional output width.")
    parser.add_argument("--height", type=int, help="Optional output height.")
    parser.add_argument("--metering", choices=["centre", "spot", "average", "custom"], help="rpicam metering mode.")
    parser.add_argument("--exposure", choices=["normal", "sport"], help="rpicam exposure profile.")
    parser.add_argument("--ev", type=float, help="Exposure value compensation.")
    parser.add_argument("--brightness", type=float, help="Brightness adjustment, usually -1 to 1.")
    parser.add_argument("--contrast", type=float, help="Contrast adjustment.")
    parser.add_argument("--saturation", type=float, help="Saturation adjustment.")
    parser.add_argument("--sharpness", type=float, help="Sharpness adjustment.")
    parser.add_argument("--tuning-file", help="Path to a libcamera tuning file on the Pi.")
    parser.add_argument("--mode", help="Sensor mode string such as 2028:1080:12:P.")
    parser.add_argument("--hdr", choices=["off", "auto", "sensor", "single-exp"], help="rpicam HDR mode.")


def _add_processing_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stack-method", choices=["sigma", "median", "mean"], help="Frame stacking method.")
    parser.add_argument("--sigma", type=float, help="Sigma threshold for sigma-clipped stacking.")
    parser.add_argument("--min-frames", type=int, help="Minimum frames to keep after quality filtering.")
    parser.add_argument("--max-working-edge", type=int, help="Resize long edge before processing; useful for quick tests.")
    parser.add_argument(
        "--save-intermediates",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Save stacked.tif and iris_mask.png.",
    )


def _pi_from_args(args: argparse.Namespace, base):
    return merge_pi(
        base,
        host=getattr(args, "host", None),
        user=getattr(args, "user", None),
        port=getattr(args, "port", None),
        remote_root=getattr(args, "remote_root", None),
        ssh_key=getattr(args, "ssh_key", None),
    )


def _capture_from_args(args: argparse.Namespace, base: CaptureSettings) -> CaptureSettings:
    gain = getattr(args, "gain", None)
    iso_equivalent = getattr(args, "iso_equivalent", None)
    if gain is None and iso_equivalent is not None:
        gain = iso_equivalent / 100
    return merge_capture(
        base,
        count=getattr(args, "count", None),
        shutter_us=getattr(args, "shutter_us", None),
        gain=gain,
        awb=getattr(args, "awb", None),
        awb_gains=getattr(args, "awb_gains", None),
        denoise=getattr(args, "denoise", None),
        quality=getattr(args, "quality", None),
        width=getattr(args, "width", None),
        height=getattr(args, "height", None),
        metering=getattr(args, "metering", None),
        exposure=getattr(args, "exposure", None),
        ev=getattr(args, "ev", None),
        brightness=getattr(args, "brightness", None),
        contrast=getattr(args, "contrast", None),
        saturation=getattr(args, "saturation", None),
        sharpness=getattr(args, "sharpness", None),
        tuning_file=getattr(args, "tuning_file", None),
        mode=getattr(args, "mode", None),
        hdr=getattr(args, "hdr", None),
    )


def _processing_from_args(args: argparse.Namespace, base: ProcessingSettings) -> ProcessingSettings:
    return merge_processing(
        base,
        stack_method=getattr(args, "stack_method", None),
        sigma=getattr(args, "sigma", None),
        min_frames=getattr(args, "min_frames", None),
        max_working_edge=getattr(args, "max_working_edge", None),
        save_intermediates=getattr(args, "save_intermediates", None),
    )


def _print(message: str) -> None:
    try:
        from rich.console import Console

        Console().print(message, markup=False)
    except Exception:
        print(message)


def _error(message: str) -> None:
    try:
        from rich.console import Console

        Console(stderr=True).print(message, style="red", markup=False)
    except Exception:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
