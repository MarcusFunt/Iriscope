import type {
  ConfigPayload,
  ConfigResponse,
  LabelRecord,
  PreprocessReport,
  ProcessOptions,
  ProcessResponse,
  SessionRecord,
  StatusResponse,
  WebRTCAnswerPayload,
  WebRTCOfferPayload,
} from "./types";

type ApiErrorPayload = {
  detail?: string;
};

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path);
  return parseResponse<T>(response);
}

export async function apiPost<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseResponse<T>(response);
}

export function getStatus() {
  return apiGet<StatusResponse>("/api/status");
}

export function getSessions() {
  return apiGet<SessionRecord[]>("/api/sessions");
}

export function getConfig() {
  return apiGet<ConfigResponse>("/api/config");
}

export function preprocessSession(sessionDir: string, maxFrames = 16) {
  return apiPost<{ ok: boolean; report: PreprocessReport }>("/api/preprocess", {
    session_dir: sessionDir,
    max_frames: maxFrames,
  });
}

export function processSession(sessionDir: string, options: ProcessOptions) {
  return apiPost<ProcessResponse>("/api/process", {
    session_dir: sessionDir,
    ...options,
    max_working_edge: options.max_working_edge || null,
    dark_path: options.dark_path || null,
    flat_path: options.flat_path || null,
  });
}

export function saveConfig(config: ConfigPayload) {
  return apiPost<ConfigResponse>("/api/config", config);
}

export function getLabel(sessionDir: string) {
  const params = new URLSearchParams({ session_dir: sessionDir });
  return apiGet<{ ok: boolean; label: LabelRecord }>(`/api/label?${params.toString()}`);
}

export function saveLabel(sessionDir: string, label: LabelRecord) {
  return apiPost<{ ok: boolean; label: LabelRecord }>("/api/label", {
    ...label,
    session_dir: sessionDir,
  });
}

export function snapshotUrl(deviceName: string, nonce: number) {
  const params = new URLSearchParams({ device: deviceName, t: String(nonce) });
  return `/api/uvc/snapshot?${params.toString()}`;
}

export function piSnapshotUrl(nonce: number) {
  const params = new URLSearchParams({ t: String(nonce) });
  return `/api/pi/snapshot?${params.toString()}`;
}

export function piStreamUrl(nonce: number) {
  const params = new URLSearchParams({ t: String(nonce) });
  return `/api/pi/stream.mjpeg?${params.toString()}`;
}

export function createPiWebRTCAnswer(offer: WebRTCOfferPayload) {
  return apiPost<WebRTCAnswerPayload>("/api/pi/webrtc/offer", offer);
}

export function artifactUrl(path: string) {
  const params = new URLSearchParams({ path });
  return `/api/artifact?${params.toString()}`;
}

export function reviewUrl(sessionDir: string) {
  const params = new URLSearchParams({ session_dir: sessionDir });
  return `/api/review?${params.toString()}`;
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.ok) {
    return response.json() as Promise<T>;
  }
  let payload: ApiErrorPayload = {};
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    payload = {};
  }
  throw new Error(payload.detail || `HTTP ${response.status}`);
}
