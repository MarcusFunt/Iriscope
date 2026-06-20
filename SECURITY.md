# Security Policy

Iriscope handles iris images and local governance labels. Treat all captures,
processed outputs, reports, and `iriscope_labels.json` files as sensitive
biometric data.

## Supported Use

- Run the API on loopback (`127.0.0.1`) for normal workstation use.
- Keep capture folders, calibration files, and labels on trusted local storage.
- Do not upload captures or labels to cloud services unless your own consent
  and data-handling process explicitly allows it.

## Remote Access

Unauthenticated API access is intended only for loopback. If you bind the API to
a non-loopback address, set `IRISCOPE_ADMIN_TOKEN` and send it as either:

```text
Authorization: Bearer <token>
```

or:

```text
X-Iriscope-Token: <token>
```

Use `IRISCOPE_ALLOWED_ORIGINS` as a comma-separated list when a remote browser
origin is required. Prefer a private network or SSH tunnel over exposing the API
directly.

## File Boundaries

Web API session paths must resolve under the configured capture root. Artifact
downloads are also bounded to the capture root. Dark and flat calibration files
may resolve under the capture root or the project `calibration/` directory.

## Reporting Issues

Do not include private captures, labels, SSH keys, tokens, or subject metadata in
public issue reports. Share only minimal reproduction steps and redacted logs.
