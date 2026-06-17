export type ToolStatus = {
  python_modules: Record<string, boolean>;
  executables: Record<string, boolean>;
};

export type CaptureSettings = {
  count: number;
  shutter_us: number;
  gain: number;
  awb_gains: [number, number] | number[];
  denoise: string;
  quality: number;
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
    remote_root: string;
    capture: CaptureSettings;
    preview: PreviewSettings;
    processing: Record<string, unknown>;
  };
  tools: ToolStatus;
  serial_ports: string[];
  camera_devices: Array<{ name: string; instance_id?: string; source?: string }>;
  capture_root: string;
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
};
