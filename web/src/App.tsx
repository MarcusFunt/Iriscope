import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  Aperture,
  CheckCircle2,
  CircleAlert,
  Database,
  Eye,
  FolderOpen,
  Gauge,
  HardDrive,
  History,
  ListChecks,
  Play,
  RefreshCw,
  Save,
  ScanEye,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Tag,
  Terminal,
} from "lucide-react";
import { apiPost, getSessions, getStatus, preprocessSession, saveLabel, snapshotUrl } from "./api";
import type { LabelRecord, PreprocessReport, SessionRecord, StatusResponse } from "./types";

type CaptureForm = {
  subject: string;
  eye: "left" | "right";
  count: number;
  shutter_us: number;
  gain: number;
  awb_red: number;
  awb_blue: number;
};

type LogItem = {
  time: string;
  level: "info" | "ok" | "warn" | "error";
  message: string;
};

const defaultCapture: CaptureForm = {
  subject: "S001",
  eye: "left",
  count: 12,
  shutter_us: 8000,
  gain: 1,
  awb_red: 1.8,
  awb_blue: 1.4,
};

const defaultLabel: LabelRecord = {
  subject_code: "S001",
  eye: "left",
  consent_recorded: false,
  biometric_category: "iris_visible_light",
  allowed_use: "local_enhancement_only",
  exclude_from_training: true,
  operator: "",
  lighting: "diffuse white LED",
  lens: "macro lens",
  capture_distance_mm: null,
  quality_label: "unreviewed",
  tags: ["macro", "visible-light"],
  notes: "",
  updated_at: null,
};

export default function App() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [selectedSession, setSelectedSession] = useState("");
  const [capture, setCapture] = useState<CaptureForm>(defaultCapture);
  const [label, setLabel] = useState<LabelRecord>(defaultLabel);
  const [preprocess, setPreprocess] = useState<PreprocessReport | null>(null);
  const [logs, setLogs] = useState<LogItem[]>([
    { time: now(), level: "info", message: "Iriscope host interface ready." },
  ]);
  const [busy, setBusy] = useState<string | null>(null);
  const [snapshotNonce, setSnapshotNonce] = useState(Date.now());
  const [snapshotFailed, setSnapshotFailed] = useState(false);

  const appendLog = useCallback((level: LogItem["level"], message: string) => {
    setLogs((items) => [{ time: now(), level, message }, ...items].slice(0, 80));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextSessions] = await Promise.all([getStatus(), getSessions()]);
      setStatus(nextStatus);
      setSessions(nextSessions);
      if (!selectedSession && nextSessions[0]) {
        setSelectedSession(nextSessions[0].path);
      }
    } catch (error) {
      appendLog("error", errorMessage(error));
    }
  }, [appendLog, selectedSession]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const cameraName = useMemo(() => {
    const devices = status?.camera_devices ?? [];
    const piDevice =
      devices.find((device) => device.instance_id?.toLowerCase().includes("vid_1d6b&pid_0104")) ??
      devices.find((device) => device.name === "UVC Camera");
    return piDevice?.name ?? devices[0]?.name ?? "UVC Camera";
  }, [status]);

  const piReady = Boolean(status?.config.pi_host);
  const serialPort = status?.serial_ports.find((port) => port === "COM22") ?? status?.serial_ports[0] ?? "not detected";

  async function runAction<T>(name: string, action: () => Promise<T>, onSuccess?: (result: T) => void) {
    setBusy(name);
    appendLog("info", `${name} started.`);
    try {
      const result = await action();
      onSuccess?.(result);
      appendLog("ok", `${name} completed.`);
      await refresh();
    } catch (error) {
      appendLog("error", `${name} failed: ${errorMessage(error)}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="app-shell">
      <Sidebar />
      <main className="workspace">
      <TopBar status={status} piReady={piReady} serialPort={serialPort} cameraName={cameraName} onRefresh={refresh} />

        <section className="work-grid" aria-label="Iriscope workstation">
          <div className="preview-panel">
            <PanelTitle icon={<ScanEye size={18} />} title="Live Preview" actionLabel={cameraName} />
            <div className="preview-frame">
              <img
                src={snapshotFailed ? "/iris-placeholder.png" : snapshotUrl(cameraName, snapshotNonce)}
                alt="Live camera preview"
                onError={() => setSnapshotFailed(true)}
              />
              <button
                className="icon-button preview-refresh"
                title="Refresh preview"
                onClick={() => {
                  setSnapshotFailed(false);
                  setSnapshotNonce(Date.now());
                }}
              >
                <RefreshCw size={17} />
              </button>
            </div>
            <QualityStrip preprocess={preprocess} />
          </div>

          <CapturePanel
            capture={capture}
            setCapture={setCapture}
            busy={busy}
            piReady={piReady}
            onCalibrate={() =>
              runAction("Calibration", () => apiPost("/api/calibrate"), (result) => {
                appendLog("info", JSON.stringify(result));
              })
            }
            onCapture={() =>
              runAction("Capture", () =>
                apiPost("/api/capture", {
                  ...capture,
                  awb_red: capture.awb_red,
                  awb_blue: capture.awb_blue,
                }),
              )
            }
          />

          <SessionRail
            sessions={sessions}
            selectedSession={selectedSession}
            onSelect={(session) => {
              setSelectedSession(session.path);
              setLabel((current) => ({ ...current, subject_code: session.name.split("_")[0] || current.subject_code }));
            }}
          />

          <PreprocessPanel
            sessionDir={selectedSession}
            setSessionDir={setSelectedSession}
            report={preprocess}
            busy={busy}
            onPreprocess={() =>
              runAction("Preprocess", () => preprocessSession(selectedSession), (result) => {
                setPreprocess(result.report);
              })
            }
            onProcess={() =>
              runAction("Process", () =>
                apiPost("/api/process", {
                  session_dir: selectedSession,
                  stack_method: "sigma",
                  sigma: 2.5,
                  min_frames: 3,
                }),
              )
            }
          />

          <LabelPanel
            label={label}
            setLabel={setLabel}
            sessionDir={selectedSession}
            busy={busy}
            onSave={() =>
              runAction("Save Label", () => saveLabel(selectedSession, label), (result) => {
                setLabel(result.label);
              })
            }
          />

          <LogPanel logs={logs} />
        </section>
      </main>
    </div>
  );
}

function Sidebar() {
  return (
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand">
        <div className="brand-mark">
          <Aperture size={22} />
        </div>
        <div>
          <strong>Iriscope</strong>
          <span>Host</span>
        </div>
      </div>
      <nav>
        <NavItem icon={<Eye size={18} />} label="Capture" active />
        <NavItem icon={<SlidersHorizontal size={18} />} label="Preprocess" />
        <NavItem icon={<Tag size={18} />} label="Label" />
        <NavItem icon={<Sparkles size={18} />} label="Review" />
        <NavItem icon={<Settings size={18} />} label="Settings" />
      </nav>
      <div className="privacy-note">
        <Database size={16} />
        <span>Local data only</span>
      </div>
    </aside>
  );
}

function NavItem({ icon, label, active = false }: { icon: ReactNode; label: string; active?: boolean }) {
  return (
    <button className={active ? "nav-item active" : "nav-item"} type="button">
      {icon}
      <span>{label}</span>
    </button>
  );
}

function TopBar({
  status,
  piReady,
  serialPort,
  cameraName,
  onRefresh,
}: {
  status: StatusResponse | null;
  piReady: boolean;
  serialPort: string;
  cameraName: string;
  onRefresh: () => void;
}) {
  const moduleCount = status ? Object.values(status.tools.python_modules).filter(Boolean).length : 0;
  const moduleTotal = status ? Object.values(status.tools.python_modules).length : 0;
  return (
    <header className="topbar">
      <div>
        <h1>Capture workstation</h1>
        <p>{status?.capture_root ?? "captures"} </p>
      </div>
      <div className="status-strip">
        <StatusPill tone={serialPort === "COM22" ? "ok" : "warn"} icon={<Terminal size={16} />} label={serialPort} />
        <StatusPill tone={cameraName === "UVC Camera" ? "ok" : "warn"} icon={<ScanEye size={16} />} label={cameraName} />
        <StatusPill tone={piReady ? "ok" : "warn"} icon={<HardDrive size={16} />} label={piReady ? status?.config.pi_host ?? "Pi" : "No SSH host"} />
        <StatusPill tone={piReady ? "ok" : "warn"} icon={<CircleAlert size={16} />} label={piReady ? "Pi OS ready" : "Pi OS pending"} />
        <StatusPill tone={moduleCount === moduleTotal ? "ok" : "warn"} icon={<Gauge size={16} />} label={`${moduleCount}/${moduleTotal} deps`} />
        <button className="icon-button" title="Refresh status" onClick={onRefresh}>
          <RefreshCw size={17} />
        </button>
      </div>
    </header>
  );
}

function StatusPill({
  tone,
  icon,
  label,
}: {
  tone: "ok" | "warn" | "error";
  icon: ReactNode;
  label: string;
}) {
  return (
    <span className={`status-pill ${tone}`}>
      {icon}
      {label}
    </span>
  );
}

function PanelTitle({ icon, title, actionLabel }: { icon: ReactNode; title: string; actionLabel?: string }) {
  return (
    <div className="panel-title">
      <div>
        {icon}
        <h2>{title}</h2>
      </div>
      {actionLabel ? <span>{actionLabel}</span> : null}
    </div>
  );
}

function CapturePanel({
  capture,
  setCapture,
  busy,
  piReady,
  onCalibrate,
  onCapture,
}: {
  capture: CaptureForm;
  setCapture: (value: CaptureForm) => void;
  busy: string | null;
  piReady: boolean;
  onCalibrate: () => void;
  onCapture: () => void;
}) {
  return (
    <section className="panel capture-panel">
      <PanelTitle icon={<Activity size={18} />} title="Capture" />
      <div className="form-grid">
        <Field label="Subject">
          <input value={capture.subject} onChange={(event) => setCapture({ ...capture, subject: event.target.value })} />
        </Field>
        <Field label="Eye">
          <div className="segmented">
            <button className={capture.eye === "left" ? "selected" : ""} onClick={() => setCapture({ ...capture, eye: "left" })}>
              Left
            </button>
            <button className={capture.eye === "right" ? "selected" : ""} onClick={() => setCapture({ ...capture, eye: "right" })}>
              Right
            </button>
          </div>
        </Field>
        <Field label="Frames">
          <input type="number" min={1} max={60} value={capture.count} onChange={(event) => setCapture({ ...capture, count: Number(event.target.value) })} />
        </Field>
        <Field label="Shutter us">
          <input type="number" value={capture.shutter_us} onChange={(event) => setCapture({ ...capture, shutter_us: Number(event.target.value) })} />
        </Field>
        <Field label="Gain">
          <input type="number" step="0.1" value={capture.gain} onChange={(event) => setCapture({ ...capture, gain: Number(event.target.value) })} />
        </Field>
        <Field label="AWB red">
          <input type="number" step="0.1" value={capture.awb_red} onChange={(event) => setCapture({ ...capture, awb_red: Number(event.target.value) })} />
        </Field>
        <Field label="AWB blue">
          <input type="number" step="0.1" value={capture.awb_blue} onChange={(event) => setCapture({ ...capture, awb_blue: Number(event.target.value) })} />
        </Field>
      </div>
      <div className="button-row">
        <button className="secondary" onClick={onCalibrate} disabled={busy !== null}>
          <ListChecks size={17} />
          Calibrate
        </button>
        <button className="primary" onClick={onCapture} disabled={busy !== null || !piReady}>
          <Play size={17} />
          Capture Stack
        </button>
      </div>
    </section>
  );
}

function PreprocessPanel({
  sessionDir,
  setSessionDir,
  report,
  busy,
  onPreprocess,
  onProcess,
}: {
  sessionDir: string;
  setSessionDir: (value: string) => void;
  report: PreprocessReport | null;
  busy: string | null;
  onPreprocess: () => void;
  onProcess: () => void;
}) {
  return (
    <section className="panel preprocess-panel">
      <PanelTitle icon={<SlidersHorizontal size={18} />} title="Pre-processing" />
      <Field label="Session path">
        <input value={sessionDir} onChange={(event) => setSessionDir(event.target.value)} placeholder="captures/S001_left_..." />
      </Field>
      <div className="metrics-row">
        <Metric label="Frames" value={report ? `${report.frames_inspected}/${report.frames_total}` : "-"} />
        <Metric label="Focus" value={report ? report.summary.focus_score_median.toFixed(1) : "-"} />
        <Metric label="Clipping" value={report ? `${(report.summary.clip_fraction_max * 100).toFixed(1)}%` : "-"} />
        <Metric label="Mask" value={report ? (report.summary.mask_ready ? "ok" : "check") : "-"} />
        <Metric
          label="Pupil/Iris"
          value={report?.summary.pupil_to_iris_ratio ? report.summary.pupil_to_iris_ratio.toFixed(2) : "-"}
        />
      </div>
      <ul className="recommendations">
        {(report?.recommendations ?? ["Run inspection before stacking."]).map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <div className="button-row">
        <button className="secondary" onClick={onPreprocess} disabled={busy !== null || !sessionDir}>
          <RefreshCw size={17} />
          Inspect Frames
        </button>
        <button className="primary" onClick={onProcess} disabled={busy !== null || !sessionDir}>
          <Sparkles size={17} />
          Process Session
        </button>
      </div>
    </section>
  );
}

function LabelPanel({
  label,
  setLabel,
  sessionDir,
  busy,
  onSave,
}: {
  label: LabelRecord;
  setLabel: (value: LabelRecord) => void;
  sessionDir: string;
  busy: string | null;
  onSave: () => void;
}) {
  return (
    <section className="panel label-panel">
      <PanelTitle icon={<Tag size={18} />} title="Biometric Label" />
      <div className="form-grid">
        <Field label="Subject code">
          <input value={label.subject_code} onChange={(event) => setLabel({ ...label, subject_code: event.target.value })} />
        </Field>
        <Field label="Quality">
          <select value={label.quality_label} onChange={(event) => setLabel({ ...label, quality_label: event.target.value })}>
            <option value="unreviewed">Unreviewed</option>
            <option value="accept">Accept</option>
            <option value="needs_recapture">Needs recapture</option>
            <option value="exclude">Exclude</option>
          </select>
        </Field>
        <Field label="Lighting">
          <input value={label.lighting} onChange={(event) => setLabel({ ...label, lighting: event.target.value })} />
        </Field>
        <Field label="Lens">
          <input value={label.lens} onChange={(event) => setLabel({ ...label, lens: event.target.value })} />
        </Field>
      </div>
      <label className="checkbox-line">
        <input
          type="checkbox"
          checked={label.consent_recorded}
          onChange={(event) => setLabel({ ...label, consent_recorded: event.target.checked })}
        />
        Consent recorded
      </label>
      <label className="checkbox-line">
        <input
          type="checkbox"
          checked={label.exclude_from_training}
          onChange={(event) => setLabel({ ...label, exclude_from_training: event.target.checked })}
        />
        Exclude from model training
      </label>
      <Field label="Notes">
        <textarea value={label.notes} onChange={(event) => setLabel({ ...label, notes: event.target.value })} />
      </Field>
      <button className="primary full" disabled={busy !== null || !sessionDir} onClick={onSave}>
        <Save size={17} />
        Save Label
      </button>
    </section>
  );
}

function SessionRail({
  sessions,
  selectedSession,
  onSelect,
}: {
  sessions: SessionRecord[];
  selectedSession: string;
  onSelect: (session: SessionRecord) => void;
}) {
  return (
    <section className="panel session-rail">
      <PanelTitle icon={<History size={18} />} title="Sessions" />
      <div className="session-list">
        {sessions.length === 0 ? <p className="empty">No local capture sessions yet.</p> : null}
        {sessions.map((session) => (
          <button
            key={session.path}
            className={selectedSession === session.path ? "session-row selected" : "session-row"}
            onClick={() => onSelect(session)}
          >
            <span>
              <strong>{session.name}</strong>
              <small>{session.frame_count} frames</small>
            </span>
            <span className="session-badges">
              {session.preprocessed ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}
              {session.labeled ? <Tag size={15} /> : null}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

function LogPanel({ logs }: { logs: LogItem[] }) {
  return (
    <section className="panel log-panel">
      <PanelTitle icon={<Terminal size={18} />} title="Run Log" />
      <div className="logs">
        {logs.map((item) => (
          <div className={`log-line ${item.level}`} key={`${item.time}-${item.message}`}>
            <span>{item.time}</span>
            <p>{item.message}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function QualityStrip({ preprocess }: { preprocess: PreprocessReport | null }) {
  return (
    <div className="quality-strip">
      <Metric label="Focus" value={preprocess ? preprocess.summary.focus_score_median.toFixed(1) : "pending"} />
      <Metric label="Luma" value={preprocess ? preprocess.summary.mean_luma_median.toFixed(2) : "pending"} />
      <Metric label="Ready" value={preprocess?.summary.ready_for_stack ? "yes" : "no"} />
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function now() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
