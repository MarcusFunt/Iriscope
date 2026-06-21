export type ToolStatus = {
  python_modules: Record<string, boolean>;
  executables: Record<string, boolean>;
};

export type AwbMode = "auto" | "incandescent" | "tungsten" | "fluorescent" | "indoor" | "daylight" | "cloudy" | "custom" | "manual";
export type MeteringMode = "centre" | "spot" | "average" | "custom";
export type ExposureMode = "normal" | "sport";
export type HdrMode = "off" | "auto" | "sensor" | "single-exp";
export type DenoiseMode = "auto" | "off" | "cdn_off" | "cdn_fast" | "cdn_hq";

export type CaptureSettings = {
  count: number;
  shutter_us: number;
  gain: number;
  iso_equivalent: number;
  awb: AwbMode;
  awb_gains: [number, number] | number[] | null;
  denoise: DenoiseMode;
  quality: number;
  width?: number | null;
  height?: number | null;
  metering: MeteringMode;
  exposure: ExposureMode;
  ev: number;
  brightness: number;
  contrast: number;
  saturation: number;
  sharpness: number;
  tuning_file?: string | null;
  mode?: string | null;
  hdr: HdrMode;
  nopreview?: boolean;
  immediate?: boolean;
  raw?: boolean;
  command_preview: string;
};

export type PreviewSettings = {
  width: number;
  height: number;
  framerate: number;
  quality: number;
  stream_timeout_s: number;
  command_preview: string;
  media_type: string;
  webrtc_available?: boolean;
  webrtc_reason?: string;
};

export type ProcessingSettings = {
  stack_method: "sigma" | "median" | "mean";
  sigma: number;
  min_frames: number;
  save_intermediates: boolean;
  max_working_edge?: number | null;
  quality: QualityThresholds;
};

export type QualityThresholds = {
  max_clip_fraction: number;
  min_relative_focus: number;
  min_median_focus: number;
  min_mean_luma: number;
  max_mean_luma: number;
  min_alignment_score: number;
  max_eval_clip_fraction: number;
  min_mask_coverage: number;
  max_mask_coverage: number;
  min_pupil_iris_ratio: number;
  max_pupil_iris_ratio: number;
  min_iris_radius_fraction: number;
  max_iris_radius_fraction: number;
  max_center_offset_fraction: number;
  max_edge_gain: number;
  max_edge_gain_with_contrast: number;
  max_contrast_gain_for_edge: number;
};

export type HealthCheck = {
  ok: boolean;
  status: string;
  message: string;
  elapsed_ms?: number;
  frame_bytes?: number;
  free_gb?: number;
  used_percent?: string;
  devices?: CameraDevice[];
  problem_devices?: CameraDevice[];
  [key: string]: unknown;
};

export type HealthStatus = {
  ssh: HealthCheck;
  rpicam: HealthCheck;
  preview: HealthCheck;
  disk: HealthCheck;
  windows_pnp: HealthCheck;
};

export type CameraDevice = {
  name: string;
  instance_id?: string;
  source?: string;
  status?: string;
};

export type StatusResponse = {
  platform: {
    system: string;
    release: string;
    python: string;
  };
  config: {
    exists: boolean;
    path: string;
    pi_host: string | null;
    pi_user: string;
    pi_port: number;
    remote_root: string;
    ssh_key_configured: boolean;
    connect_timeout: number;
    capture: CaptureSettings;
    preview: PreviewSettings;
    processing: ProcessingSettings;
  };
  tools: ToolStatus;
  serial_ports: string[];
  camera_devices: CameraDevice[];
  health: HealthStatus;
  capture_root: string;
};

export type ConfigPayload = {
  pi: {
    host: string | null;
    user: string;
    port: number;
    remote_root: string;
    ssh_key: string | null;
    connect_timeout: number;
  };
  capture: {
    count: number;
    shutter_us: number;
    gain: number;
    awb: AwbMode;
    awb_gains: [number, number] | null;
    denoise: DenoiseMode;
    quality: number;
    width: number | null;
    height: number | null;
    metering: MeteringMode;
    exposure: ExposureMode;
    ev: number;
    brightness: number;
    contrast: number;
    saturation: number;
    sharpness: number;
    tuning_file: string | null;
    mode: string | null;
    hdr: HdrMode;
    nopreview: boolean;
    immediate: boolean;
    raw: boolean;
  };
  preview: {
    width: number;
    height: number;
    framerate: number;
    quality: number;
    stream_timeout_s: number;
  };
  processing: ProcessingSettings;
};

export type ConfigResponse = {
  ok: boolean;
  path: string;
  config: ConfigPayload;
};

export type SessionRecord = {
  name: string;
  path: string;
  modified: number;
  frame_count: number;
  processed: boolean;
  labeled: boolean;
  preprocessed: boolean;
  outputs: Record<string, string | null>;
};

export type PreprocessReport = {
  frames_total: number;
  frames_inspected: number;
  summary: {
    focus_score_median: number;
    mean_luma_median: number;
    clip_fraction_max: number;
    ready_for_stack: boolean;
    mask_ready?: boolean;
    mask_method?: string;
    mask_coverage?: number;
    pupil_to_iris_ratio?: number;
  };
  mask?: {
    method: string;
    coverage: number;
    radius: number;
    pupil_radius: number;
    source_file?: string;
  } | null;
  recommendations: string[];
};

export type LabelRecord = {
  subject_code: string;
  eye: string;
  consent_recorded: boolean;
  biometric_category: string;
  allowed_use: string;
  exclude_from_training: boolean;
  operator: string;
  lighting: string;
  lens: string;
  capture_distance_mm: number | null;
  quality_label: string;
  tags: string[];
  notes: string;
  updated_at: string | null;
};

export type ProcessResponse = {
  ok: boolean;
  output_dir: string;
  enhanced_jpg: string;
  enhanced_tif: string;
  report_json: string;
  contact_sheet: string;
  quality_status: "pass" | "review" | "requires_recapture" | "unknown";
  requires_recapture: boolean;
  quality_flags: string[];
};

export type ProcessOptions = {
  stack_method: "sigma" | "median" | "mean";
  sigma: number;
  min_frames: number;
  save_intermediates: boolean;
  max_working_edge: number | null;
  dark_path: string;
  flat_path: string;
};

export type WebRTCOfferPayload = {
  sdp: string;
  type: "offer";
};

export type WebRTCAnswerPayload = {
  ok: boolean;
  sdp: string;
  type: "answer";
};
