import { expect, test } from "@playwright/test";

const sessionDir = "C:/Iriscope/captures/S042_left_20260616_153000";
const outputPaths = {
  enhanced_jpg: `${sessionDir}/processed/enhanced.jpg`,
  enhanced_tif: `${sessionDir}/processed/enhanced.tif`,
  report_json: `${sessionDir}/processed/report.json`,
  contact_sheet: `${sessionDir}/processed/contact_sheet.jpg`,
  iris_mask: `${sessionDir}/processed/iris_mask.png`,
};

const pngPixel = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGNMmXaBAQAGJQJAdawK3AAAAABJRU5ErkJggg==",
  "base64",
);

const qualityThresholds = {
  max_clip_fraction: 0.2,
  min_relative_focus: 0.35,
  min_median_focus: 10,
  min_mean_luma: 0.02,
  max_mean_luma: 0.98,
  min_alignment_score: 0.55,
  max_eval_clip_fraction: 0.35,
  min_mask_coverage: 0.06,
  max_mask_coverage: 0.48,
  min_pupil_iris_ratio: 0.18,
  max_pupil_iris_ratio: 0.68,
  min_iris_radius_fraction: 0.16,
  max_iris_radius_fraction: 0.55,
  max_center_offset_fraction: 0.28,
  max_edge_gain: 7,
  max_edge_gain_with_contrast: 5.5,
  max_contrast_gain_for_edge: 3,
};

const editableConfig = {
  pi: {
    host: "iriscope-pi.local",
    user: "camera",
    port: 22,
    remote_root: "/home/camera/iriscope",
    ssh_key: "C:/Iriscope/id_rsa",
    connect_timeout: 15,
  },
  capture: {
    count: 16,
    shutter_us: 0,
    gain: 0,
    iso_equivalent: 0,
    awb: "auto",
    awb_gains: [3.2, 1.4],
    denoise: "cdn_fast",
    quality: 95,
    width: null,
    height: null,
    metering: "centre",
    exposure: "normal",
    ev: 0,
    brightness: 0,
    contrast: 1,
    saturation: 1,
    sharpness: 1,
    tuning_file: "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json",
    mode: null,
    hdr: "off",
    nopreview: true,
    immediate: true,
    raw: true,
    command_preview: "rpicam-still --raw --awb auto --tuning-file /usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json -o frame_0001.jpg",
  },
  preview: {
    width: 640,
    height: 480,
    framerate: 12,
    quality: 70,
    stream_timeout_s: 0,
    command_preview: "rpicam-vid -t 0 -n --codec mjpeg -o -",
    media_type: "multipart/x-mixed-replace; boundary=iriscope-frame",
  },
  processing: {
    stack_method: "sigma",
    sigma: 2.5,
    min_frames: 3,
    save_intermediates: true,
    max_working_edge: null,
    quality: qualityThresholds,
  },
};

test("main workstation flow loads config, labels, preprocesses, processes, and saves", async ({ page }) => {
  let processed = false;
  let savedLabel: Record<string, unknown> | null = null;
  let savedConfig: typeof editableConfig | null = null;
  let processPayload: Record<string, unknown> | null = null;
  let piWebrtcRequested = false;
  let piStreamRequested = false;

  await page.route("**/api/status", async (route) => {
    await route.fulfill({
      json: {
        platform: { system: "Windows", release: "test", python: "3.13.0" },
        config: {
          exists: true,
          path: "C:/Iriscope/.iriscope.toml",
          pi_host: editableConfig.pi.host,
          pi_user: editableConfig.pi.user,
          pi_port: editableConfig.pi.port,
          remote_root: editableConfig.pi.remote_root,
          ssh_key_configured: true,
          connect_timeout: editableConfig.pi.connect_timeout,
          capture: editableConfig.capture,
          preview: editableConfig.preview,
          processing: editableConfig.processing,
        },
        health: {
          ssh: { ok: true, status: "ok", message: "SSH key access verified." },
          rpicam: { ok: true, status: "ok", message: "0 : imx477 [4056x3040 12-bit RGGB]" },
          preview: { ok: true, status: "ok", message: "Preview frame received.", frame_bytes: 1234 },
          disk: { ok: true, status: "ok", message: "4.2 GB free", free_gb: 4.2 },
          windows_pnp: { ok: true, status: "ok", message: "1 camera device reports OK." },
        },
        tools: {
          python_modules: { cv2: true, rawpy: true, numpy: true, PIL: true, serial: true },
          executables: { ssh: true, scp: true, ffmpeg: true },
        },
        serial_ports: ["COM22"],
        camera_devices: [{ name: "UVC Camera", instance_id: "USB\\VID_1D6B&PID_0104", source: "pnp", status: "OK" }],
        capture_root: "C:/Iriscope/captures",
      },
    });
  });

  await page.route("**/api/config", async (route) => {
    if (route.request().method() === "POST") {
      savedConfig = route.request().postDataJSON() as typeof editableConfig;
      await route.fulfill({ json: { ok: true, path: "C:/Iriscope/.iriscope.toml", config: savedConfig } });
      return;
    }
    await route.fulfill({ json: { ok: true, path: "C:/Iriscope/.iriscope.toml", config: editableConfig } });
  });

  await page.route("**/api/sessions", async (route) => {
    await route.fulfill({
      json: [
        {
          name: "S042_left_20260616_153000",
          path: sessionDir,
          modified: 1_781_632_800,
          frame_count: 8,
          processed,
          labeled: true,
          preprocessed: processed,
          outputs: processed ? outputPaths : {},
        },
      ],
    });
  });

  await page.route("**/api/label**", async (route) => {
    if (route.request().method() === "POST") {
      savedLabel = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ json: { ok: true, label: { ...savedLabel, updated_at: "2026-06-16T20:00:00Z" } } });
      return;
    }
    await route.fulfill({
      json: {
        ok: true,
        label: {
          subject_code: "S042",
          eye: "left",
          consent_recorded: true,
          biometric_category: "iris_visible_light",
          allowed_use: "local_enhancement_only",
          exclude_from_training: true,
          operator: "test",
          lighting: "diffuse white LED",
          lens: "macro lens",
          capture_distance_mm: 120,
          quality_label: "unreviewed",
          tags: ["macro"],
          notes: "existing note",
          updated_at: "2026-06-16T19:00:00Z",
        },
      },
    });
  });

  await page.route("**/api/preprocess", async (route) => {
    await route.fulfill({
      json: {
        ok: true,
        report: {
          frames_total: 8,
          frames_inspected: 8,
          summary: {
            focus_score_median: 42.5,
            mean_luma_median: 0.46,
            clip_fraction_max: 0.01,
            ready_for_stack: true,
            mask_ready: true,
            mask_method: "radial_or_hough_circle",
            mask_coverage: 0.24,
            pupil_to_iris_ratio: 0.31,
          },
          mask: { method: "radial_or_hough_circle", coverage: 0.24, radius: 90, pupil_radius: 28 },
          recommendations: ["Frames are ready for alignment and stacking."],
        },
      },
    });
  });

  await page.route("**/api/process", async (route) => {
    processed = true;
    processPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      json: {
        ok: true,
        output_dir: `${sessionDir}/processed`,
        ...outputPaths,
        quality_status: "pass",
        requires_recapture: false,
        quality_flags: [],
      },
    });
  });

  await page.route("**/api/pi/webrtc/offer", async (route) => {
    piWebrtcRequested = true;
    await route.fulfill({ status: 503, json: { detail: "WebRTC unavailable in smoke test" } });
  });

  await page.route("**/api/pi/stream.mjpeg**", async (route) => {
    piStreamRequested = true;
    await route.fulfill({ contentType: "image/png", body: pngPixel });
  });
  await page.route("**/api/pi/snapshot**", async (route) => {
    await route.fulfill({ contentType: "image/png", body: pngPixel });
  });
  await page.route("**/api/uvc/snapshot**", async (route) => {
    await route.fulfill({ status: 500, json: { detail: "UVC preview should not be used when Pi is configured." } });
  });
  await page.route("**/api/artifact**", async (route) => {
    await route.fulfill({ contentType: "image/png", body: pngPixel });
  });

  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Capture workstation" })).toBeVisible();
  await expect(page.getByText("Pi HQ camera")).toBeVisible();
  await expect.poll(() => piWebrtcRequested).toBe(true);
  await expect.poll(() => piStreamRequested).toBe(true);
  await expect(page.getByText("Sharpness")).toBeVisible();
  await expect(page.locator(".sharpness-indicator")).toHaveAttribute("aria-label", /Sharpness 0\.0/);
  await expect(page.locator(".quality-strip .metric").filter({ hasText: "Focus" }).locator("strong")).toHaveText("0.0");
  await expect(page.locator(".quality-strip .metric").filter({ hasText: "Luma" }).locator("strong")).toHaveText(/\d+\.\d{2}/);
  await expect(page.locator(".quality-strip .metric").filter({ hasText: "Ready" }).locator("strong")).toHaveText("no");
  await expect(page.getByText("Next action")).toBeVisible();
  await expect(page.getByText("Camera tuning")).toBeVisible();
  await expect(page.getByLabel("Frames")).toHaveValue("16");
  await page.getByText("Camera tuning").click();
  await expect(page.getByLabel("Shutter us")).toHaveValue("0");
  await expect(page.getByRole("spinbutton", { name: "Gain" })).toHaveValue("0");
  await expect(page.getByLabel("AWB mode")).toHaveValue("auto");
  await expect(page.getByLabel("AWB red")).toHaveValue("3.2");

  await page.locator("nav").getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByLabel("Save stacked image and iris mask").uncheck();
  await page.getByRole("button", { name: "Save Settings" }).click();
  await expect.poll(() => savedConfig?.processing.save_intermediates).toBe(false);

  await page.locator("nav").getByRole("button", { name: "Label", exact: true }).click();
  await expect(page.getByLabel("Subject code")).toHaveValue("S042");
  await expect(page.getByLabel("Notes")).toHaveValue("existing note");

  await page.locator("nav").getByRole("button", { name: "Preprocess", exact: true }).click();
  await page.getByRole("button", { name: "Inspect Frames", exact: true }).click();
  await expect(page.getByText("Frames are ready for alignment and stacking.")).toBeVisible();
  await expect(page.getByText("42.5").first()).toBeVisible();

  await page.getByRole("button", { name: "Process Session", exact: true }).click();
  await expect.poll(() => processPayload?.save_intermediates).toBe(false);
  await expect(page.getByRole("img", { name: "Enhanced" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Report JSON" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open Review" })).toBeEnabled();

  await page.locator("nav").getByRole("button", { name: "Label", exact: true }).click();
  await page.getByLabel("Notes").fill("approved for local review");
  await page.getByRole("button", { name: "Save Label" }).click();
  await expect.poll(() => savedLabel?.notes).toBe("approved for local review");
  await expect.poll(() => savedLabel?.session_dir).toBe(sessionDir);
});
