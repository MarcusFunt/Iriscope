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

test("main workstation flow loads config, labels, preprocesses, processes, and saves", async ({ page }) => {
  let processed = false;
  let savedLabel: Record<string, unknown> | null = null;
  let piStreamRequested = false;

  await page.route("**/api/status", async (route) => {
    await route.fulfill({
      json: {
        platform: { system: "Windows", release: "test", python: "3.13.0" },
        config: {
          exists: true,
          path: "C:/Iriscope/.iriscope.toml",
          pi_host: "iriscope-pi.local",
          pi_user: "camera",
          remote_root: "/home/camera/iriscope",
          capture: {
            count: 16,
            shutter_us: 6000,
            gain: 1.5,
            awb_gains: [2.0, 1.2],
            denoise: "off",
            quality: 95,
            command_preview: "rpicam-still --raw -o frame_0001.jpg",
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
          processing: { stack_method: "sigma", sigma: 2.5, min_frames: 3 },
        },
        tools: {
          python_modules: { cv2: true, rawpy: true, numpy: true, PIL: true, serial: true },
          executables: { ssh: true, scp: true, ffmpeg: true },
        },
        serial_ports: ["COM22"],
        camera_devices: [{ name: "UVC Camera", instance_id: "USB\\VID_1D6B&PID_0104", source: "pnp" }],
        capture_root: "C:/Iriscope/captures",
      },
    });
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
    await route.fulfill({ json: { ok: true, output_dir: `${sessionDir}/processed`, ...outputPaths } });
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
  await expect.poll(() => piStreamRequested).toBe(true);
  await expect(page.getByLabel("Frames")).toHaveValue("16");
  await expect(page.getByLabel("Shutter us")).toHaveValue("6000");
  await expect(page.getByLabel("Gain")).toHaveValue("1.5");
  await expect(page.getByLabel("AWB red")).toHaveValue("2");
  await expect(page.getByLabel("Subject code")).toHaveValue("S042");
  await expect(page.getByLabel("Notes")).toHaveValue("existing note");

  await page.getByRole("button", { name: "Inspect Frames" }).click();
  await expect(page.getByText("Frames are ready for alignment and stacking.")).toBeVisible();
  await expect(page.getByText("42.5").first()).toBeVisible();

  await page.getByRole("button", { name: "Process Session" }).click();
  await expect(page.getByRole("img", { name: "Enhanced" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Report JSON" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open Review" })).toBeEnabled();

  await page.getByLabel("Notes").fill("approved for local review");
  await page.getByRole("button", { name: "Save Label" }).click();
  await expect.poll(() => savedLabel?.notes).toBe("approved for local review");
  await expect.poll(() => savedLabel?.session_dir).toBe(sessionDir);
});
