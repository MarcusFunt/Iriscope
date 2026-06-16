from __future__ import annotations

import html
import json
import webbrowser
from pathlib import Path
from typing import Any


def generate_review(session_dir: str | Path, open_browser: bool = False) -> Path:
    processed = resolve_processed_dir(session_dir)
    report_path = processed / "report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"No report.json found under {processed}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    html_path = processed / "review.html"
    html_path.write_text(_render_review_html(report, processed), encoding="utf-8")
    if open_browser:
        webbrowser.open(html_path.resolve().as_uri())
    return html_path


def resolve_processed_dir(path: str | Path) -> Path:
    root = Path(path)
    if (root / "report.json").exists():
        return root
    if (root / "processed" / "report.json").exists():
        return root / "processed"
    return root / "processed"


def _render_review_html(report: dict[str, Any], processed: Path) -> str:
    outputs = report.get("outputs", {})
    frame_rows = "\n".join(_frame_row(index, frame) for index, frame in enumerate(report.get("frames", [])))
    contact = _relative_img(outputs.get("contact_sheet"), processed)
    enhanced = _relative_img(outputs.get("enhanced_jpg"), processed)
    mask = _relative_img(outputs.get("iris_mask"), processed)
    kept = ", ".join(str(index) for index in report.get("kept_indices", []))
    mask_report = report.get("mask", {})
    alignment = report.get("alignment", [])
    failed_alignments = [item for item in alignment if item.get("method") == "none"]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Iriscope Review</title>
  <style>
    body {{
      margin: 0;
      font-family: Segoe UI, system-ui, sans-serif;
      background: #151719;
      color: #eceff3;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px;
    }}
    h1, h2 {{
      font-weight: 650;
      letter-spacing: 0;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 18px;
    }}
    .panel {{
      border: 1px solid #353a40;
      border-radius: 8px;
      padding: 16px;
      background: #1d2024;
    }}
    img {{
      max-width: 100%;
      height: auto;
      border-radius: 4px;
      background: #0b0c0d;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid #353a40;
      padding: 8px;
      text-align: left;
    }}
    code {{
      color: #a7d8ff;
    }}
    .muted {{
      color: #aab1ba;
    }}
  </style>
</head>
<body>
<main>
  <h1>Iriscope Review</h1>
  <p class="muted">Session: <code>{html.escape(str(report.get("session", "")))}</code></p>

  <section class="grid">
    <div class="panel">
      <h2>Enhanced</h2>
      {_img_tag(enhanced, "Enhanced iris image")}
    </div>
    <div class="panel">
      <h2>Contact Sheet</h2>
      {_img_tag(contact, "Contact sheet")}
    </div>
    <div class="panel">
      <h2>Iris Mask</h2>
      {_img_tag(mask, "Iris mask")}
    </div>
  </section>

  <section class="panel">
    <h2>Summary</h2>
    <p>Kept frames: <code>{html.escape(kept)}</code></p>
    <p>Mask method: <code>{html.escape(str(mask_report.get("method", "")))}</code>,
       coverage: <code>{float(mask_report.get("coverage", 0.0)):.3f}</code></p>
    <p>Failed alignments: <code>{len(failed_alignments)}</code></p>
  </section>

  <section class="panel">
    <h2>Frame Metrics</h2>
    <table>
      <thead><tr><th>#</th><th>File</th><th>Focus</th><th>Mean Luma</th><th>Clipping</th></tr></thead>
      <tbody>
        {frame_rows}
      </tbody>
    </table>
  </section>
</main>
</body>
</html>
"""


def _frame_row(index: int, frame: dict[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{index}</td>"
        f"<td>{html.escape(str(frame.get('file', '')))}</td>"
        f"<td>{float(frame.get('focus_score', 0.0)):.2f}</td>"
        f"<td>{float(frame.get('mean_luma', 0.0)):.3f}</td>"
        f"<td>{float(frame.get('clip_fraction', 0.0)):.3f}</td>"
        "</tr>"
    )


def _relative_img(path_value: str | None, processed: Path) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        return path.resolve().relative_to(processed.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_uri()


def _img_tag(src: str | None, alt: str) -> str:
    if not src:
        return '<p class="muted">Not generated.</p>'
    return f'<img src="{html.escape(src)}" alt="{html.escape(alt)}">'
