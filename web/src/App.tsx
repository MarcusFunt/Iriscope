import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  Undo2,
} from "lucide-react";
import {
  apiPost,
  applyCalibration,
  artifactUrl,
  createPiWebRTCAnswer,
  getCalibrationStatus,
  getConfig,
  getLabel,
  getSessions,
  getStatus,
  piSnapshotUrl,
  piStreamUrl,
  preprocessSession,
  processSession,
  reviewUrl,
  saveLabel,
  saveConfig,
  snapshotUrl,
  startCalibration,
  revertCalibration,
} from "./api";
import type {
  AwbMode,
  CalibrationStatus,
  ConfigPayload,
  ExposureMode,
  HdrMode,
  HealthCheck,
  LabelRecord,
  MeteringMode,
  PreprocessReport,
  ProcessOptions,
  ProcessResponse,
  QualityThresholds,
  SessionRecord,
  StatusResponse,
} from "./types";

type CaptureForm = {
  subject: string;
  eye: "left" | "right";
  count: number;
  shutter_us: number;
  gain: number;
  awb: AwbMode;
  awb_red: number;
  awb_blue: number;
};

type LogItem = {
  id: string;
  time: string;
  level: "info" | "ok" | "warn" | "error";
  message: string;
};

type ProcessedOutputState = {
  sessionDir: string;
  result: ProcessResponse;
};

type PreviewMode = "webrtc" | "stream" | "snapshot";
type ActiveView = "capture" | "preprocess" | "label" | "review" | "settings";
type SharpnessTone = "measuring" | "soft" | "ok" | "sharp" | "unavailable";
type WorkflowState = "done" | "current" | "waiting";

type LiveQualityReading = {
  score: number | null;
  meanLuma: number | null;
  clipFraction: number | null;
  ready: boolean | null;
  tone: SharpnessTone;
};

type WorkflowStep = {
  key: string;
  view: ActiveView;
  label: string;
  detail: string;
  state: WorkflowState;
};

const calibrationPhases = [
  { key: "precheck", label: "Precheck" },
  { key: "auto_baseline", label: "Auto baseline" },
  { key: "exposure_sweep", label: "Exposure sweep" },
  { key: "awb_lock_test", label: "AWB lock" },
  { key: "focus_geometry_check", label: "Focus check" },
  { key: "recommendation", label: "Recommendation" },
];

const defaultCapture: CaptureForm = {
  subject: "S001",
  eye: "left",
  count: 12,
  shutter_us: 0,
  gain: 0,
  awb: "auto",
  awb_red: 3.2,
  awb_blue: 1.4,
};

const awbModes: Array<{ value: AwbMode; label: string }> = [
  { value: "auto", label: "Auto" },
  { value: "manual", label: "Manual gains" },
  { value: "daylight", label: "Daylight" },
  { value: "cloudy", label: "Cloudy" },
  { value: "indoor", label: "Indoor" },
  { value: "tungsten", label: "Tungsten" },
  { value: "fluorescent", label: "Fluorescent" },
  { value: "incandescent", label: "Incandescent" },
  { value: "custom", label: "Custom" },
];

const meteringModes: Array<{ value: MeteringMode; label: string }> = [
  { value: "centre", label: "Centre" },
  { value: "spot", label: "Spot" },
  { value: "average", label: "Average" },
  { value: "custom", label: "Custom" },
];

const exposureModes: Array<{ value: ExposureMode; label: string }> = [
  { value: "normal", label: "Normal" },
  { value: "sport", label: "Sport" },
];

const hdrModes: Array<{ value: HdrMode; label: string }> = [
  { value: "off", label: "Off" },
  { value: "auto", label: "Auto" },
  { value: "sensor", label: "Sensor" },
  { value: "single-exp", label: "Single exposure" },
];

const tuningFileOptions = [
  { value: "", label: "Default tuning" },
  { value: "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json", label: "IMX477 scientific" },
  { value: "/usr/share/libcamera/ipa/rpi/vc4/imx477_noir.json", label: "IMX477 NoIR" },
  { value: "/usr/share/libcamera/ipa/rpi/vc4/imx477.json", label: "IMX477 standard vc4" },
];

const defaultLabel: LabelRecord = {
  subject_code: "",
  eye: "",
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

const defaultProcessOptions: ProcessOptions = {
  stack_method: "sigma",
  sigma: 2.5,
  min_frames: 3,
  save_intermediates: true,
  max_working_edge: null,
  dark_path: "",
  flat_path: "",
};

let logSequence = 0;

export default function App() {
  const [activeView, setActiveView] = useState<ActiveView>("capture");
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [selectedSession, setSelectedSession] = useState("");
  const [capture, setCapture] = useState<CaptureForm>(defaultCapture);
  const [captureHydrated, setCaptureHydrated] = useState(false);
  const [processHydrated, setProcessHydrated] = useState(false);
  const [settingsHydrated, setSettingsHydrated] = useState(false);
  const [label, setLabel] = useState<LabelRecord>(defaultLabel);
  const [settingsDraft, setSettingsDraft] = useState<ConfigPayload | null>(null);
  const [processOptions, setProcessOptions] = useState<ProcessOptions>(defaultProcessOptions);
  const [preprocess, setPreprocess] = useState<PreprocessReport | null>(null);
  const [processedOutput, setProcessedOutput] = useState<ProcessedOutputState | null>(null);
  const [calibration, setCalibration] = useState<CalibrationStatus | null>(null);
  const [logs, setLogs] = useState<LogItem[]>([
    { id: logId(), time: now(), level: "info", message: "Iriscope host interface ready." },
  ]);
  const [busy, setBusy] = useState<string | null>(null);
  const [snapshotNonce, setSnapshotNonce] = useState(Date.now());
  const [previewMode, setPreviewMode] = useState<PreviewMode>("webrtc");
  const [snapshotFailed, setSnapshotFailed] = useState(false);
  const [livePreviewReady, setLivePreviewReady] = useState(false);

  const appendLog = useCallback((level: LogItem["level"], message: string) => {
    setLogs((items) => [{ id: logId(), time: now(), level, message }, ...items].slice(0, 80));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextSessions, nextConfig, nextCalibration] = await Promise.all([
        getStatus(),
        getSessions(),
        getConfig(),
        getCalibrationStatus(),
      ]);
      setStatus(nextStatus);
      setSessions(nextSessions);
      setCalibration(nextCalibration);
      if (!captureHydrated) {
        setCapture(captureFromConfig(nextConfig.config));
        setCaptureHydrated(true);
      }
      if (!processHydrated) {
        setProcessOptions(processFromConfig(nextConfig.config));
        setProcessHydrated(true);
      }
      if (!settingsHydrated) {
        setSettingsDraft(nextConfig.config);
        setSettingsHydrated(true);
      }
      if (!selectedSession && nextSessions[0]) {
        setSelectedSession(nextSessions[0].path);
      }
    } catch (error) {
      appendLog("error", errorMessage(error));
    }
  }, [appendLog, captureHydrated, processHydrated, selectedSession, settingsHydrated]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!calibration?.active) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      void getCalibrationStatus()
        .then(setCalibration)
        .catch((error) => appendLog("warn", `Calibration status failed: ${errorMessage(error)}`));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [appendLog, calibration?.active]);

  useEffect(() => {
    const listedSession = sessions.some((session) => session.path === selectedSession);
    if (!selectedSession || !listedSession) {
      return;
    }

    let cancelled = false;
    void getLabel(selectedSession)
      .then((result) => {
        if (!cancelled) {
          setLabel(withSessionFallback(result.label, selectedSession));
        }
      })
      .catch((error) => {
        if (!cancelled) {
          appendLog("warn", `Label load failed: ${errorMessage(error)}`);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [appendLog, selectedSession, sessions]);

  const displayStatus = useMemo(() => (livePreviewReady ? withLivePreviewOk(status) : status), [livePreviewReady, status]);

  const cameraName = useMemo(() => {
    const devices = displayStatus?.camera_devices ?? [];
    const readyDevices = devices.filter((device) => (device.status ?? "").toLowerCase() === "ok");
    const piDevice =
      readyDevices.find((device) => device.instance_id?.toLowerCase().includes("vid_1d6b&pid_0104")) ??
      readyDevices.find((device) => device.name === "UVC Camera") ??
      readyDevices[0] ??
      devices[0];
    return piDevice?.name ?? "no camera";
  }, [displayStatus]);

  const piConfigured = Boolean(displayStatus?.config.pi_host);
  const piWebRTCAvailable = displayStatus?.config.preview.webrtc_available !== false;
  const preferredPreviewMode: PreviewMode = piConfigured
    ? piWebRTCAvailable
      ? "webrtc"
      : "stream"
    : "snapshot";
  const piReady = Boolean(displayStatus?.health?.ssh?.ok && displayStatus?.health?.rpicam?.ok);
  const previewLabel = piConfigured ? "Pi HQ camera" : cameraName;
  const previewSourceKey = piConfigured ? `pi:${displayStatus?.config.pi_host ?? ""}` : `uvc:${cameraName}`;
  const qualityThresholds = displayStatus?.config.processing.quality;
  const liveSharpnessThreshold = qualityThresholds?.min_median_focus ?? 10;
  const previewSrc = snapshotFailed
    || !displayStatus
    ? "/iris-placeholder.png"
    : piConfigured
      ? previewMode === "snapshot"
        ? piSnapshotUrl(snapshotNonce)
        : previewMode === "stream"
        ? piStreamUrl(snapshotNonce)
        : "/iris-placeholder.png"
      : snapshotUrl(cameraName, snapshotNonce);
  const serialPort = displayStatus?.serial_ports[0] ?? "not detected";
  const selectedRecord = useMemo(
    () => sessions.find((session) => session.path === selectedSession),
    [selectedSession, sessions],
  );
  const outputPaths = useMemo(() => {
    const base = selectedRecord?.outputs ?? {};
    if (processedOutput?.sessionDir !== selectedSession) {
      return base;
    }
    return {
      ...base,
      enhanced_jpg: processedOutput.result.enhanced_jpg,
      enhanced_tif: processedOutput.result.enhanced_tif,
      report_json: processedOutput.result.report_json,
      contact_sheet: processedOutput.result.contact_sheet,
    };
  }, [processedOutput, selectedRecord, selectedSession]);
  const currentProcessResult = processedOutput?.sessionDir === selectedSession ? processedOutput.result : null;
  const workflowSteps = useMemo(
    () =>
      buildWorkflowSteps({
        piReady,
        livePreviewReady,
        selectedRecord,
        selectedSession,
        preprocess,
        currentProcessResult,
        label,
      }),
    [currentProcessResult, label, livePreviewReady, piReady, preprocess, selectedRecord, selectedSession],
  );

  useEffect(() => {
    setLivePreviewReady(false);
    setSnapshotFailed(false);
    setPreviewMode(preferredPreviewMode);
    setSnapshotNonce(Date.now());
  }, [preferredPreviewMode, previewSourceKey]);

  const handleStreamFallback = useCallback(() => {
    setLivePreviewReady(false);
    setPreviewMode("snapshot");
    setSnapshotFailed(false);
    setSnapshotNonce(Date.now());
    appendLog("warn", "Pi stream unavailable; using still preview fallback.");
  }, [appendLog]);

  const handleWebRTCFallback = useCallback(
    (message: string) => {
      setLivePreviewReady(false);
      setPreviewMode("stream");
      setSnapshotFailed(false);
      setSnapshotNonce(Date.now());
      appendLog("warn", `WebRTC preview unavailable; using MJPEG fallback. ${message}`);
    },
    [appendLog],
  );

  const handlePreviewFailed = useCallback(() => {
    setLivePreviewReady(false);
    setSnapshotFailed(true);
  }, []);

  const handlePreviewReady = useCallback(() => {
    setLivePreviewReady(true);
    setStatus((current) =>
      current
        ? withLivePreviewOk(current)
        : current,
    );
  }, []);

  const handleRefreshPreview = useCallback(() => {
    setSnapshotFailed(false);
    setPreviewMode(preferredPreviewMode);
    setSnapshotNonce(Date.now());
  }, [preferredPreviewMode]);

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

  async function handleRunAutoCalibration() {
    setBusy("Auto Calibration");
    appendLog("info", "Auto Calibration started.");
    try {
      const next = await startCalibration();
      setCalibration(next);
      appendLog("ok", "Auto Calibration is running in the background.");
      setPreviewMode(preferredPreviewMode);
      setSnapshotFailed(false);
      setSnapshotNonce(Date.now());
    } catch (error) {
      appendLog("error", `Auto Calibration failed: ${errorMessage(error)}`);
    } finally {
      setBusy(null);
    }
  }

  async function handleApplyCalibration() {
    await runAction("Apply Calibration", applyCalibration, () => {
      setPreviewMode(preferredPreviewMode);
      setSnapshotFailed(false);
      setSnapshotNonce(Date.now());
    });
  }

  async function handleRevertCalibration() {
    await runAction("Revert Calibration", revertCalibration, () => {
      setPreviewMode(preferredPreviewMode);
      setSnapshotFailed(false);
      setSnapshotNonce(Date.now());
    });
  }

  return (
    <div className="app-shell">
      <Sidebar activeView={activeView} onChange={setActiveView} />
      <main className="workspace">
        <TopBar status={displayStatus} serialPort={serialPort} cameraName={cameraName} onRefresh={refresh} />
        <WorkflowStrip steps={workflowSteps} activeView={activeView} onChange={setActiveView} selectedSession={selectedRecord?.name} />

        <section className={`work-grid ${activeView}`} aria-label="Iriscope workstation">
          {activeView === "capture" ? (
            <>
              <PreviewPanel
                previewLabel={previewLabel}
                previewSrc={previewSrc}
                preprocess={preprocess}
                previewMode={previewMode}
                snapshotFailed={snapshotFailed}
                piConfigured={piConfigured}
                piWebRTCAvailable={piWebRTCAvailable}
                nonce={snapshotNonce}
                sharpnessThreshold={liveSharpnessThreshold}
                qualityThresholds={qualityThresholds}
                onStreamFallback={handleStreamFallback}
                onWebRTCFallback={handleWebRTCFallback}
                onPreviewFailed={handlePreviewFailed}
                onPreviewReady={handlePreviewReady}
                onRefreshPreview={handleRefreshPreview}
              />
              <CalibrationPanel
                calibration={calibration}
                busy={busy}
                piReady={piReady}
                onRun={() => void handleRunAutoCalibration()}
                onApply={() => void handleApplyCalibration()}
                onRevert={() => void handleRevertCalibration()}
              />
              <CapturePanel
                capture={capture}
                setCapture={setCapture}
                busy={busy}
                piReady={piReady}
                onCapture={() =>
                  runAction(
                    "Capture",
                    () =>
                      apiPost("/api/capture", {
                        ...capture,
                        awb_red: capture.awb === "manual" ? capture.awb_red : null,
                        awb_blue: capture.awb === "manual" ? capture.awb_blue : null,
                      }),
                    () => {
                      setPreviewMode(preferredPreviewMode);
                      setSnapshotFailed(false);
                      setSnapshotNonce(Date.now());
                    },
                  )
                }
              />
              <SessionRail sessions={sessions} selectedSession={selectedSession} onSelect={(session) => setSelectedSession(session.path)} />
              <LogPanel logs={logs} />
            </>
          ) : null}

          {activeView === "preprocess" ? (
            <>
              <PreprocessPanel
                sessionDir={selectedSession}
                setSessionDir={setSelectedSession}
                report={preprocess}
                processOptions={processOptions}
                setProcessOptions={setProcessOptions}
                busy={busy}
                onPreprocess={() =>
                  runAction("Preprocess", () => preprocessSession(selectedSession), (result) => {
                    setPreprocess(result.report);
                  })
                }
                onProcess={() =>
                  runAction("Process", () => processSession(selectedSession, processOptions), (result) => {
                    setProcessedOutput({ sessionDir: selectedSession, result });
                  })
                }
              />
              <OutputPanel
                sessionDir={selectedSession}
                outputs={outputPaths}
                processed={Boolean(selectedRecord?.processed || processedOutput?.sessionDir === selectedSession)}
                result={currentProcessResult}
              />
              <SessionRail sessions={sessions} selectedSession={selectedSession} onSelect={(session) => setSelectedSession(session.path)} />
              <LogPanel logs={logs} />
            </>
          ) : null}

          {activeView === "label" ? (
            <>
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
              <SessionRail sessions={sessions} selectedSession={selectedSession} onSelect={(session) => setSelectedSession(session.path)} />
              <LogPanel logs={logs} />
            </>
          ) : null}

          {activeView === "review" ? (
            <>
              <OutputPanel
                sessionDir={selectedSession}
                outputs={outputPaths}
                processed={Boolean(selectedRecord?.processed || processedOutput?.sessionDir === selectedSession)}
                result={currentProcessResult}
              />
              <SessionRail sessions={sessions} selectedSession={selectedSession} onSelect={(session) => setSelectedSession(session.path)} />
              <LogPanel logs={logs} />
            </>
          ) : null}

          {activeView === "settings" ? (
            <>
              <SettingsPanel
                settings={settingsDraft}
                setSettings={setSettingsDraft}
                busy={busy}
                onSave={() =>
                  settingsDraft
                    ? runAction("Save Settings", () => saveConfig(settingsDraft), (result) => {
                        setSettingsDraft(result.config);
                        setCapture(captureFromConfig(result.config));
                        setProcessOptions(processFromConfig(result.config));
                        setCaptureHydrated(true);
                        setProcessHydrated(true);
                        setSettingsHydrated(true);
                      })
                    : undefined
                }
              />
              <HealthPanel status={displayStatus} />
              <LogPanel logs={logs} />
            </>
          ) : null}
        </section>
      </main>
    </div>
  );
}

function Sidebar({
  activeView,
  onChange,
}: {
  activeView: ActiveView;
  onChange: (view: ActiveView) => void;
}) {
  const items: Array<{ view: ActiveView; icon: ReactNode; label: string }> = [
    { view: "capture", icon: <Eye size={18} />, label: "Capture" },
    { view: "preprocess", icon: <SlidersHorizontal size={18} />, label: "Preprocess" },
    { view: "label", icon: <Tag size={18} />, label: "Label" },
    { view: "review", icon: <Sparkles size={18} />, label: "Review" },
    { view: "settings", icon: <Settings size={18} />, label: "Settings" },
  ];
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
        {items.map((item) => (
          <NavItem
            key={item.view}
            icon={item.icon}
            label={item.label}
            active={activeView === item.view}
            onClick={() => onChange(item.view)}
          />
        ))}
      </nav>
      <div className="privacy-note">
        <Database size={16} />
        <span>Local data only</span>
      </div>
    </aside>
  );
}

function NavItem({
  icon,
  label,
  active = false,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button className={active ? "nav-item active" : "nav-item"} type="button" onClick={onClick}>
      {icon}
      <span>{label}</span>
    </button>
  );
}

function TopBar({
  status,
  serialPort,
  cameraName,
  onRefresh,
}: {
  status: StatusResponse | null;
  serialPort: string;
  cameraName: string;
  onRefresh: () => void;
}) {
  const moduleCount = status ? Object.values(status.tools.python_modules).filter(Boolean).length : 0;
  const moduleTotal = status ? Object.values(status.tools.python_modules).length : 0;
  const systemSummary = statusSummary(status, serialPort, cameraName, moduleCount, moduleTotal);
  return (
    <header className="topbar">
      <div>
        <h1>Capture workstation</h1>
        <p>{status?.capture_root ?? "captures"} </p>
      </div>
      <div className="topbar-actions">
        <details className={`diagnostics-summary ${systemSummary.tone}`}>
          <summary>
            <Gauge size={16} />
            <span>
              <strong>{systemSummary.label}</strong>
              <small>{systemSummary.detail}</small>
            </span>
          </summary>
          <div className="status-strip">
            <StatusPill tone={healthTone(status?.health?.ssh)} icon={<Terminal size={16} />} label={`SSH ${healthLabel(status?.health?.ssh)}`} />
            <StatusPill tone={healthTone(status?.health?.rpicam)} icon={<ScanEye size={16} />} label={`Camera ${healthLabel(status?.health?.rpicam)}`} />
            <StatusPill tone={healthTone(status?.health?.preview)} icon={<Eye size={16} />} label={`Preview ${healthLabel(status?.health?.preview)}`} />
            <StatusPill tone={healthTone(status?.health?.disk)} icon={<HardDrive size={16} />} label={`Disk ${diskLabel(status?.health?.disk)}`} />
            <StatusPill tone={healthTone(status?.health?.windows_pnp)} icon={<CircleAlert size={16} />} label={`PnP ${healthLabel(status?.health?.windows_pnp)}`} />
            <StatusPill tone={serialPort === "not detected" ? "warn" : "ok"} icon={<Terminal size={16} />} label={serialPort} />
            <StatusPill tone={cameraName === "no camera" ? "warn" : "ok"} icon={<ScanEye size={16} />} label={cameraName} />
            <StatusPill tone={moduleCount === moduleTotal ? "ok" : "warn"} icon={<Gauge size={16} />} label={`${moduleCount}/${moduleTotal} deps`} />
          </div>
        </details>
        <button className="icon-button" title="Refresh status" onClick={onRefresh}>
          <RefreshCw size={17} />
        </button>
      </div>
    </header>
  );
}

function WorkflowStrip({
  steps,
  activeView,
  selectedSession,
  onChange,
}: {
  steps: WorkflowStep[];
  activeView: ActiveView;
  selectedSession?: string;
  onChange: (view: ActiveView) => void;
}) {
  const current = steps.find((step) => step.state === "current") ?? steps[0];
  return (
    <section className="workflow-strip" aria-label="Capture workflow">
      <div className="workflow-current">
        <span>Next action</span>
        <strong>{current.label}</strong>
        <small>{selectedSession ? selectedSession : current.detail}</small>
      </div>
      <div className="workflow-steps">
        {steps.map((step, index) => (
          <button
            key={step.key}
            type="button"
            className={`workflow-step ${step.state} ${activeView === step.view ? "active" : ""}`}
            onClick={() => onChange(step.view)}
            aria-current={step.state === "current" ? "step" : undefined}
          >
            <span className="workflow-step-index">{step.state === "done" ? <CheckCircle2 size={14} /> : index + 1}</span>
            <span>
              <strong>{step.label}</strong>
              <small>{step.detail}</small>
            </span>
          </button>
        ))}
      </div>
    </section>
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

function PreviewPanel({
  previewLabel,
  previewSrc,
  preprocess,
  previewMode,
  snapshotFailed,
  piConfigured,
  piWebRTCAvailable,
  nonce,
  sharpnessThreshold,
  qualityThresholds,
  onStreamFallback,
  onWebRTCFallback,
  onPreviewFailed,
  onPreviewReady,
  onRefreshPreview,
}: {
  previewLabel: string;
  previewSrc: string;
  preprocess: PreprocessReport | null;
  previewMode: PreviewMode;
  snapshotFailed: boolean;
  piConfigured: boolean;
  piWebRTCAvailable: boolean;
  nonce: number;
  sharpnessThreshold: number;
  qualityThresholds?: QualityThresholds;
  onStreamFallback: () => void;
  onWebRTCFallback: (message: string) => void;
  onPreviewFailed: () => void;
  onPreviewReady: () => void;
  onRefreshPreview: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const [webrtcReady, setWebrtcReady] = useState(false);
  const liveQuality = useLiveQuality({
    imageRef,
    videoRef,
    enabled: !snapshotFailed,
    mode: previewMode,
    nonce,
    thresholds: qualityThresholds,
  });

  useEffect(() => {
    setWebrtcReady(false);
    if (!piConfigured || previewMode !== "webrtc" || snapshotFailed) {
      return undefined;
    }
    let cancelled = false;
    let frameReceived = false;
    let fallbackSent = false;
    let trackReceived = false;
    let pc: RTCPeerConnection | null = null;
    let startupTimer: number | undefined;

    const clearStartupTimer = () => {
      if (startupTimer !== undefined) {
        window.clearTimeout(startupTimer);
        startupTimer = undefined;
      }
    };

    const markReady = () => {
      if (cancelled || frameReceived) {
        return;
      }
      frameReceived = true;
      clearStartupTimer();
      setWebrtcReady(true);
      onPreviewReady();
    };

    const fallback = (message: string) => {
      if (cancelled || fallbackSent || frameReceived) {
        return;
      }
      fallbackSent = true;
      clearStartupTimer();
      pc?.close();
      onWebRTCFallback(message);
    };

    async function startWebRTCPreview() {
      if (!("RTCPeerConnection" in window)) {
        throw new Error("Browser does not expose RTCPeerConnection.");
      }
      pc = new RTCPeerConnection();
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.onconnectionstatechange = () => {
        if (pc?.connectionState === "failed") {
          fallback(`WebRTC peer connection failed before a frame was received (${webrtcStateSummary(pc, trackReceived)}).`);
        }
      };
      pc.ontrack = (event) => {
        trackReceived = true;
        const video = videoRef.current;
        if (!video) {
          return;
        }
        const stream = event.streams[0] ?? new MediaStream([event.track]);
        video.srcObject = stream;
        video.addEventListener("loadeddata", markReady, { once: true });
        video.addEventListener("playing", markReady, { once: true });
        event.track.addEventListener("unmute", markReady, { once: true });
        if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
          markReady();
        }
        void video.play().catch(() => undefined);
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      if (!pc.localDescription) {
        throw new Error("WebRTC offer creation failed.");
      }
      await waitForIceGatheringComplete(pc);
      const answer = await createPiWebRTCAnswer({
        sdp: pc.localDescription.sdp,
        type: "offer",
      });
      if (cancelled) {
        return;
      }
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
      startupTimer = window.setTimeout(() => {
        fallback(`No WebRTC video frame was received within 60 seconds (${webrtcStateSummary(pc, trackReceived)}).`);
      }, 60_000);
    }

    void startWebRTCPreview().catch((error) => {
      if (!cancelled) {
        pc?.close();
        onWebRTCFallback(errorMessage(error));
      }
    });

    return () => {
      cancelled = true;
      clearStartupTimer();
      if (videoRef.current?.srcObject instanceof MediaStream) {
        videoRef.current.srcObject.getTracks().forEach((track) => track.stop());
        videoRef.current.srcObject = null;
      }
      pc?.close();
    };
  }, [nonce, onPreviewReady, onWebRTCFallback, piConfigured, previewMode, snapshotFailed]);

  return (
    <div className="preview-panel">
      <PanelTitle icon={<ScanEye size={18} />} title="Live Preview" actionLabel={previewLabel} />
      <div className="preview-frame">
        {piConfigured && previewMode === "webrtc" && !snapshotFailed ? (
          <video ref={videoRef} aria-label="Live camera preview" autoPlay muted playsInline />
        ) : (
          <img
            ref={imageRef}
            src={previewSrc}
            alt="Live camera preview"
            onLoad={() => {
              if (!snapshotFailed) {
                onPreviewReady();
              }
            }}
            onError={() => {
              if (piConfigured && previewMode === "stream") {
                onStreamFallback();
                return;
              }
              onPreviewFailed();
            }}
          />
        )}
        <button className="icon-button preview-refresh" title="Refresh preview" onClick={onRefreshPreview}>
          <RefreshCw size={17} />
        </button>
        <LiveSharpnessIndicator reading={liveQuality} threshold={sharpnessThreshold} />
      </div>
      {previewMode === "webrtc" && !snapshotFailed ? (
        <p className={webrtcReady ? "preview-message ok" : "preview-message"}>
          {webrtcReady ? "WebRTC preview active." : "Starting WebRTC preview..."}
        </p>
      ) : null}
      {previewMode === "stream" && !snapshotFailed ? (
        <p className="preview-message">
          {piConfigured && !piWebRTCAvailable ? "Using MJPEG preview." : "Using MJPEG preview fallback."}
        </p>
      ) : null}
      {previewMode === "snapshot" && !snapshotFailed ? (
        <p className="preview-message">Live stream unavailable. Showing still preview fallback.</p>
      ) : null}
      {snapshotFailed ? <p className="preview-message">Preview unavailable. Check Pi SSH key access and refresh.</p> : null}
      <PreviewReadiness previewMode={previewMode} snapshotFailed={snapshotFailed} reading={liveQuality} />
      <QualityStrip preprocess={preprocess} liveQuality={liveQuality} />
    </div>
  );
}

function PreviewReadiness({
  previewMode,
  snapshotFailed,
  reading,
}: {
  previewMode: PreviewMode;
  snapshotFailed: boolean;
  reading: LiveQualityReading;
}) {
  const readiness = previewReadinessCopy(reading, snapshotFailed);
  return (
    <div className={`preview-readiness ${readiness.tone}`}>
      <div>
        <span>Transport</span>
        <strong>{transportLabel(previewMode, snapshotFailed)}</strong>
      </div>
      <div>
        <span>Frame</span>
        <strong>{readiness.frame}</strong>
      </div>
      <p>{readiness.guidance}</p>
    </div>
  );
}

function useLiveQuality({
  imageRef,
  videoRef,
  enabled,
  mode,
  nonce,
  thresholds,
}: {
  imageRef: React.RefObject<HTMLImageElement | null>;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  enabled: boolean;
  mode: PreviewMode;
  nonce: number;
  thresholds?: QualityThresholds;
}) {
  const [reading, setReading] = useState<LiveQualityReading>({
    score: null,
    meanLuma: null,
    clipFraction: null,
    ready: null,
    tone: "measuring",
  });

  useEffect(() => {
    if (!enabled) {
      setReading({ score: null, meanLuma: null, clipFraction: null, ready: null, tone: "unavailable" });
      return undefined;
    }

    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) {
      setReading({ score: null, meanLuma: null, clipFraction: null, ready: null, tone: "unavailable" });
      return undefined;
    }

    const sample = () => {
      const source = currentPreviewSource(mode, videoRef.current, imageRef.current);
      if (!source) {
        setReading({ score: null, meanLuma: null, clipFraction: null, ready: null, tone: "measuring" });
        return;
      }

      try {
        const dimensions = previewSourceDimensions(source);
        if (!dimensions) {
          setReading({ score: null, meanLuma: null, clipFraction: null, ready: null, tone: "measuring" });
          return;
        }
        const maxEdge = 320;
        const scale = Math.min(1, maxEdge / Math.max(dimensions.width, dimensions.height));
        canvas.width = Math.max(1, Math.round(dimensions.width * scale));
        canvas.height = Math.max(1, Math.round(dimensions.height * scale));
        context.drawImage(source, 0, 0, canvas.width, canvas.height);
        const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
        const frameQuality = liveQualityFromImageData(imageData.data, canvas.width, canvas.height);
        setReading({
          ...frameQuality,
          ready: liveQualityReady(frameQuality, thresholds),
          tone: sharpnessTone(frameQuality.score, thresholds?.min_median_focus ?? 10),
        });
      } catch {
        setReading({ score: null, meanLuma: null, clipFraction: null, ready: null, tone: "unavailable" });
      }
    };

    setReading({ score: null, meanLuma: null, clipFraction: null, ready: null, tone: "measuring" });
    sample();
    const interval = window.setInterval(sample, mode === "snapshot" ? 2000 : 750);

    return () => {
      window.clearInterval(interval);
    };
  }, [enabled, imageRef, mode, nonce, thresholds, videoRef]);

  return reading;
}

function LiveSharpnessIndicator({ reading, threshold }: { reading: LiveQualityReading; threshold: number }) {
  const scoreLabel = reading.score === null ? "--" : formatSharpnessScore(reading.score);
  const meterWidth = reading.score === null ? 0 : Math.min(100, (reading.score / Math.max(threshold * 3, 1)) * 100);

  return (
    <div className={`sharpness-indicator ${reading.tone}`} aria-label={`Sharpness ${scoreLabel}`}>
      <div className="sharpness-readout">
        <span>Sharpness</span>
        <strong>{scoreLabel}</strong>
      </div>
      <span className="sharpness-status">{sharpnessLabel(reading.tone)}</span>
      <div className="sharpness-meter" aria-hidden="true">
        <span style={{ width: `${meterWidth}%` }} />
      </div>
    </div>
  );
}

function currentPreviewSource(
  mode: PreviewMode,
  video: HTMLVideoElement | null,
  image: HTMLImageElement | null,
): HTMLVideoElement | HTMLImageElement | null {
  if (mode === "webrtc") {
    return video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0 ? video : null;
  }
  return image && image.naturalWidth > 0 && image.naturalHeight > 0 ? image : null;
}

function previewSourceDimensions(source: HTMLVideoElement | HTMLImageElement) {
  if (source instanceof HTMLVideoElement) {
    return source.videoWidth && source.videoHeight ? { width: source.videoWidth, height: source.videoHeight } : null;
  }
  return source.naturalWidth && source.naturalHeight ? { width: source.naturalWidth, height: source.naturalHeight } : null;
}

function liveQualityFromImageData(data: Uint8ClampedArray, width: number, height: number) {
  const pixelCount = Math.max(1, width * height);
  const gray = new Float32Array(width * height);
  let lumaSum = 0;
  let clippedPixels = 0;
  for (let pixel = 0, grayIndex = 0; pixel < data.length; pixel += 4, grayIndex += 1) {
    const luma = data[pixel] * 0.299 + data[pixel + 1] * 0.587 + data[pixel + 2] * 0.114;
    const normalizedLuma = luma / 255;
    gray[grayIndex] = luma;
    lumaSum += normalizedLuma;
    if (normalizedLuma <= 0.002 || normalizedLuma >= 0.998) {
      clippedPixels += 1;
    }
  }

  return {
    score: laplacianVariance(gray, width, height),
    meanLuma: lumaSum / pixelCount,
    clipFraction: clippedPixels / pixelCount,
  };
}

function laplacianVariance(gray: Float32Array, width: number, height: number) {
  if (width < 3 || height < 3) {
    return 0;
  }

  let count = 0;
  let sum = 0;
  let sumSquares = 0;
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const index = y * width + x;
      const laplacian = gray[index - width] + gray[index - 1] - 4 * gray[index] + gray[index + 1] + gray[index + width];
      sum += laplacian;
      sumSquares += laplacian * laplacian;
      count += 1;
    }
  }

  const mean = sum / count;
  return Math.max(0, sumSquares / count - mean * mean);
}

function liveQualityReady(
  reading: { score: number; meanLuma: number; clipFraction: number },
  thresholds?: QualityThresholds,
) {
  return (
    reading.score >= (thresholds?.min_median_focus ?? 10) &&
    thresholdsValueInRange(reading.meanLuma, thresholds?.min_mean_luma ?? 0.02, thresholds?.max_mean_luma ?? 0.98) &&
    reading.clipFraction <= (thresholds?.max_clip_fraction ?? 0.2)
  );
}

function thresholdsValueInRange(value: number, min: number, max: number) {
  return min <= value && value <= max;
}

function sharpnessTone(score: number, threshold: number): SharpnessTone {
  const minimum = Math.max(threshold, 1);
  if (score < minimum) {
    return "soft";
  }
  if (score < minimum * 2.5) {
    return "ok";
  }
  return "sharp";
}

function sharpnessLabel(tone: SharpnessTone) {
  if (tone === "sharp") {
    return "crisp";
  }
  if (tone === "ok") {
    return "ready";
  }
  if (tone === "soft") {
    return "soft";
  }
  if (tone === "unavailable") {
    return "unavailable";
  }
  return "measuring";
}

function formatSharpnessScore(score: number) {
  return score >= 100 ? String(Math.round(score)) : score.toFixed(1);
}

function waitForIceGatheringComplete(pc: RTCPeerConnection, timeoutMs = 5000) {
  if (pc.iceGatheringState === "complete") {
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    const cleanup = () => {
      window.clearTimeout(timeout);
      pc.removeEventListener("icegatheringstatechange", checkState);
      resolve();
    };
    const checkState = () => {
      if (pc.iceGatheringState === "complete") {
        cleanup();
      }
    };
    const timeout = window.setTimeout(cleanup, timeoutMs);
    pc.addEventListener("icegatheringstatechange", checkState);
    checkState();
  });
}

function webrtcStateSummary(pc: RTCPeerConnection | null, trackReceived: boolean) {
  if (!pc) {
    return "peer not created";
  }
  return `peer ${pc.connectionState}, ICE ${pc.iceConnectionState}, gather ${pc.iceGatheringState}, track ${trackReceived ? "yes" : "no"}`;
}

function CapturePanel({
  capture,
  setCapture,
  busy,
  piReady,
  onCapture,
}: {
  capture: CaptureForm;
  setCapture: (value: CaptureForm) => void;
  busy: string | null;
  piReady: boolean;
  onCapture: () => void;
}) {
  return (
    <section className="panel capture-panel">
      <PanelTitle icon={<Activity size={18} />} title="Capture" />
      <div className="capture-focus-card">
        <span>{piReady ? "Ready for stack capture" : "Camera not ready"}</span>
        <strong>{piReady ? `${capture.count} frames, ${capture.eye} eye` : "Check SSH and camera status"}</strong>
      </div>
      <div className="button-row action-row">
        <button className="primary" onClick={onCapture} disabled={busy !== null || !piReady}>
          <Play size={17} />
          Capture Stack
        </button>
      </div>
      <div className="form-grid capture-core-grid">
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
      </div>
      <details className="advanced-controls">
        <summary>
          <SlidersHorizontal size={16} />
          <span>
            <strong>Camera tuning</strong>
            <small>Shutter, gain, white balance</small>
          </span>
        </summary>
        <div className="form-grid">
          <Field label="Shutter us">
            <input
              type="number"
              min={0}
              title="0 lets rpicam choose exposure automatically"
              value={capture.shutter_us}
              onChange={(event) => setCapture({ ...capture, shutter_us: Number(event.target.value) })}
            />
          </Field>
          <Field label="ISO equiv">
            <input
              type="number"
              min={0}
              step={50}
              title="Blank or 0 lets rpicam choose analogue gain automatically"
              value={capture.gain > 0 ? Math.round(capture.gain * 100) : ""}
              onChange={(event) => setCapture({ ...capture, gain: event.target.value ? Number(event.target.value) / 100 : 0 })}
            />
          </Field>
          <Field label="Gain">
            <input
              type="number"
              min={0}
              step="0.1"
              value={capture.gain}
              onChange={(event) => setCapture({ ...capture, gain: Number(event.target.value) })}
            />
          </Field>
          <Field label="AWB mode">
            <select value={capture.awb} onChange={(event) => setCapture({ ...capture, awb: event.target.value as AwbMode })}>
              {awbModes.map((mode) => (
                <option value={mode.value} key={mode.value}>
                  {mode.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="AWB red">
            <input
              type="number"
              step="0.1"
              disabled={capture.awb !== "manual"}
              value={capture.awb_red}
              onChange={(event) => setCapture({ ...capture, awb_red: Number(event.target.value) })}
            />
          </Field>
          <Field label="AWB blue">
            <input
              type="number"
              step="0.1"
              disabled={capture.awb !== "manual"}
              value={capture.awb_blue}
              onChange={(event) => setCapture({ ...capture, awb_blue: Number(event.target.value) })}
            />
          </Field>
        </div>
      </details>
    </section>
  );
}

function CalibrationPanel({
  calibration,
  busy,
  piReady,
  onRun,
  onApply,
  onRevert,
}: {
  calibration: CalibrationStatus | null;
  busy: string | null;
  piReady: boolean;
  onRun: () => void;
  onApply: () => void;
  onRevert: () => void;
}) {
  const status = calibration ?? {
    ok: true,
    active: false,
    status: "idle",
    job_id: null,
    phase: "idle",
    progress: 0,
    message: "No calibration run yet.",
    started_at: null,
    completed_at: null,
    candidates: [],
    warnings: [],
    recommendation: null,
    report_path: null,
    remote_dir: null,
    local_dir: null,
    error: null,
    applied_profile: null,
  } satisfies CalibrationStatus;
  const recommendation = status.recommendation;
  const bestThumbnail =
    recommendation?.artifacts.selected_best_frame ??
    recommendation?.artifacts.best_thumbnail ??
    recommendation?.artifacts.best_frame ??
    null;
  const baselineThumbnail = recommendation?.artifacts.baseline_thumbnail ?? recommendation?.artifacts.baseline_frame ?? null;
  const canApply = Boolean(recommendation && !status.active && status.status !== "applied");
  const canRevert = Boolean(status.applied_profile && !status.active);
  return (
    <section className="panel calibration-panel">
      <PanelTitle icon={<ListChecks size={18} />} title="Auto Calibration" actionLabel={status.status} />
      <div className={`calibration-summary ${status.status === "failed" ? "error" : recommendation ? "ok" : "warn"}`}>
        <span>{status.phase.replaceAll("_", " ")}</span>
        <strong>{recommendation ? `${recommendation.confidence} confidence, score ${formatScore(recommendation.score)}` : status.message}</strong>
        <div className="calibration-progress" aria-label={`Calibration progress ${Math.round((status.progress ?? 0) * 100)}%`}>
          <span style={{ width: `${Math.max(0, Math.min(100, (status.progress ?? 0) * 100))}%` }} />
        </div>
      </div>
      <div className="calibration-phases" aria-label="Calibration phases">
        {calibrationPhases.map((phase, index) => {
          const activeIndex = calibrationPhases.findIndex((item) => item.key === status.phase);
          const done = status.status === "complete" || status.status === "applied" || (activeIndex >= 0 && index < activeIndex);
          const active = phase.key === status.phase;
          return (
            <span className={done ? "done" : active ? "active" : ""} key={phase.key}>
              {done ? <CheckCircle2 size={12} /> : index + 1}
              {phase.label}
            </span>
          );
        })}
      </div>
      {recommendation ? (
        <>
          <div className="calibration-evidence">
            {baselineThumbnail ? (
              <figure>
                <img src={artifactUrl(baselineThumbnail)} alt="Baseline calibration frame" />
                <figcaption>Before</figcaption>
              </figure>
            ) : null}
            {bestThumbnail ? (
              <figure>
                <img src={artifactUrl(bestThumbnail)} alt="Best calibration candidate" />
                <figcaption>Best</figcaption>
              </figure>
            ) : null}
          </div>
          <div className="metrics-row compact">
            <Metric label="Luma" value={formatMetric(recommendation.quality.mean_luma, 2)} />
            <Metric label="Clipping" value={formatPercent(recommendation.quality.clip_fraction)} />
            <Metric label="Focus" value={formatMetric(recommendation.quality.focus_score, 1)} />
            <Metric label="Mask" value={formatPercent(recommendation.quality.mask_coverage)} />
          </div>
          <div className="settings-diff">
            {recommendation.settings_diff.length === 0 ? <p>No capture setting changes recommended.</p> : null}
            {recommendation.settings_diff.map((item) => (
              <div key={item.field}>
                <span>{item.field}</span>
                <strong>
                  {formatCalibrationValue(item.before)} to {formatCalibrationValue(item.after)}
                </strong>
              </div>
            ))}
          </div>
        </>
      ) : null}
      {status.error ? <p className="panel-error">{status.error}</p> : null}
      {status.warnings?.length ? (
        <ul className="recommendations compact">
          {status.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
      <div className="button-row action-row">
        <button className="secondary" onClick={onRun} disabled={busy !== null || status.active || !piReady}>
          <RefreshCw size={17} />
          Run Auto Calibration
        </button>
        <button className="primary" onClick={onApply} disabled={busy !== null || !canApply}>
          <CheckCircle2 size={17} />
          Apply Profile
        </button>
        <button className="secondary" onClick={onRevert} disabled={busy !== null || !canRevert}>
          <Undo2 size={17} />
          Revert Profile
        </button>
      </div>
    </section>
  );
}

function PreprocessPanel({
  sessionDir,
  setSessionDir,
  report,
  processOptions,
  setProcessOptions,
  busy,
  onPreprocess,
  onProcess,
}: {
  sessionDir: string;
  setSessionDir: (value: string) => void;
  report: PreprocessReport | null;
  processOptions: ProcessOptions;
  setProcessOptions: (value: ProcessOptions) => void;
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
      <div className="button-row action-row">
        <button className="secondary" onClick={onPreprocess} disabled={busy !== null || !sessionDir}>
          <RefreshCw size={17} />
          Inspect Frames
        </button>
        <button className="primary" onClick={onProcess} disabled={busy !== null || !sessionDir}>
          <Sparkles size={17} />
          Process Session
        </button>
      </div>
      <div className="form-grid process-controls">
        <Field label="Stack method">
          <select
            value={processOptions.stack_method}
            onChange={(event) =>
              setProcessOptions({ ...processOptions, stack_method: event.target.value as ProcessOptions["stack_method"] })
            }
          >
            <option value="sigma">Sigma clip</option>
            <option value="median">Median</option>
            <option value="mean">Mean</option>
          </select>
        </Field>
        <Field label="Sigma">
          <input
            type="number"
            step="0.1"
            min={0.1}
            value={processOptions.sigma}
            onChange={(event) => setProcessOptions({ ...processOptions, sigma: Number(event.target.value) })}
          />
        </Field>
        <Field label="Min frames">
          <input
            type="number"
            min={1}
            value={processOptions.min_frames}
            onChange={(event) => setProcessOptions({ ...processOptions, min_frames: Number(event.target.value) })}
          />
        </Field>
        <Field label="Max working edge">
          <input
            type="number"
            min={64}
            value={processOptions.max_working_edge ?? ""}
            onChange={(event) =>
              setProcessOptions({
                ...processOptions,
                max_working_edge: event.target.value ? Number(event.target.value) : null,
              })
            }
          />
        </Field>
        <Field label="Dark frame">
          <input value={processOptions.dark_path} onChange={(event) => setProcessOptions({ ...processOptions, dark_path: event.target.value })} />
        </Field>
        <Field label="Flat field">
          <input value={processOptions.flat_path} onChange={(event) => setProcessOptions({ ...processOptions, flat_path: event.target.value })} />
        </Field>
      </div>
      <label className="checkbox-line">
        <input
          type="checkbox"
          checked={processOptions.save_intermediates}
          onChange={(event) => setProcessOptions({ ...processOptions, save_intermediates: event.target.checked })}
        />
        Save stacked image and iris mask
      </label>
    </section>
  );
}

function OutputPanel({
  sessionDir,
  outputs,
  processed,
  result,
}: {
  sessionDir: string;
  outputs: Record<string, string | null | undefined>;
  processed: boolean;
  result: ProcessResponse | null;
}) {
  const enhanced = outputPath(outputs, "enhanced_jpg");
  const contactSheet = outputPath(outputs, "contact_sheet");
  const mask = outputPath(outputs, "iris_mask");
  const report = outputPath(outputs, "report_json");
  const enhancedTif = outputPath(outputs, "enhanced_tif");
  const hasPreview = Boolean(enhanced || contactSheet || mask);

  return (
    <section className="panel output-panel">
      <PanelTitle icon={<FolderOpen size={18} />} title="Output Review" actionLabel={processed ? "processed" : "not processed"} />
      {result ? (
        <div className={`quality-result ${result.requires_recapture ? "error" : result.quality_status === "review" ? "warn" : "ok"}`}>
          <strong>{qualityStatusLabel(result.quality_status)}</strong>
          <span>{result.requires_recapture ? "Recapture recommended" : "No recapture flag"}</span>
          {result.quality_flags.length ? <small>{result.quality_flags.join(", ")}</small> : null}
        </div>
      ) : null}
      {!hasPreview ? <p className="empty">No processed outputs for the selected session.</p> : null}
      {hasPreview ? (
        <div className="output-grid">
          {enhanced ? <OutputImage label="Enhanced" path={enhanced} /> : null}
          {contactSheet ? <OutputImage label="Contact sheet" path={contactSheet} /> : null}
          {mask ? <OutputImage label="Mask" path={mask} /> : null}
        </div>
      ) : null}
      <div className="button-row">
        <button
          className="secondary"
          type="button"
          disabled={!processed || !sessionDir}
          onClick={() => window.open(reviewUrl(sessionDir), "_blank", "noopener,noreferrer")}
        >
          <Sparkles size={17} />
          Open Review
        </button>
        {report ? (
          <a className="link-button" href={artifactUrl(report)} target="_blank" rel="noreferrer">
            Report JSON
          </a>
        ) : null}
        {enhancedTif ? (
          <a className="link-button" href={artifactUrl(enhancedTif)} target="_blank" rel="noreferrer">
            Master TIFF
          </a>
        ) : null}
      </div>
    </section>
  );
}

function OutputImage({ label, path }: { label: string; path: string }) {
  return (
    <figure className="output-image">
      <img src={artifactUrl(path)} alt={label} />
      <figcaption>{label}</figcaption>
    </figure>
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
        <Field label="Eye">
          <select value={label.eye} onChange={(event) => setLabel({ ...label, eye: event.target.value })}>
            <option value="">Unspecified</option>
            <option value="left">Left</option>
            <option value="right">Right</option>
          </select>
        </Field>
        <Field label="Quality">
          <select value={label.quality_label} onChange={(event) => setLabel({ ...label, quality_label: event.target.value })}>
            <option value="unreviewed">Unreviewed</option>
            <option value="accept">Accept</option>
            <option value="needs_recapture">Needs recapture</option>
            <option value="exclude">Exclude</option>
          </select>
        </Field>
        <Field label="Operator">
          <input value={label.operator} onChange={(event) => setLabel({ ...label, operator: event.target.value })} />
        </Field>
        <Field label="Allowed use">
          <input value={label.allowed_use} onChange={(event) => setLabel({ ...label, allowed_use: event.target.value })} />
        </Field>
        <Field label="Biometric category">
          <input value={label.biometric_category} onChange={(event) => setLabel({ ...label, biometric_category: event.target.value })} />
        </Field>
        <Field label="Lighting">
          <input value={label.lighting} onChange={(event) => setLabel({ ...label, lighting: event.target.value })} />
        </Field>
        <Field label="Lens">
          <input value={label.lens} onChange={(event) => setLabel({ ...label, lens: event.target.value })} />
        </Field>
        <Field label="Distance mm">
          <input
            type="number"
            min={1}
            value={label.capture_distance_mm ?? ""}
            onChange={(event) =>
              setLabel({ ...label, capture_distance_mm: event.target.value ? Number(event.target.value) : null })
            }
          />
        </Field>
        <Field label="Tags">
          <input value={label.tags.join(", ")} onChange={(event) => setLabel({ ...label, tags: parseTags(event.target.value) })} />
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
      {label.updated_at ? <p className="field-note">Updated {label.updated_at}</p> : null}
      <button className="primary full" disabled={busy !== null || !sessionDir} onClick={onSave}>
        <Save size={17} />
        Save Label
      </button>
    </section>
  );
}

function SettingsPanel({
  settings,
  setSettings,
  busy,
  onSave,
}: {
  settings: ConfigPayload | null;
  setSettings: (value: ConfigPayload | null) => void;
  busy: string | null;
  onSave: () => void | Promise<void> | undefined;
}) {
  if (!settings) {
    return (
      <section className="panel settings-panel">
        <PanelTitle icon={<Settings size={18} />} title="Settings" />
        <p className="empty">Configuration has not loaded yet.</p>
      </section>
    );
  }
  const updatePi = (patch: Partial<ConfigPayload["pi"]>) => setSettings({ ...settings, pi: { ...settings.pi, ...patch } });
  const updateCapture = (patch: Partial<ConfigPayload["capture"]>) =>
    setSettings({ ...settings, capture: { ...settings.capture, ...patch } });
  const updatePreview = (patch: Partial<ConfigPayload["preview"]>) =>
    setSettings({ ...settings, preview: { ...settings.preview, ...patch } });
  const updateProcessing = (patch: Partial<ConfigPayload["processing"]>) =>
    setSettings({ ...settings, processing: { ...settings.processing, ...patch } });
  const updateCalibration = (patch: Partial<ConfigPayload["calibration"]>) =>
    setSettings({ ...settings, calibration: { ...settings.calibration, ...patch } });
  const updateCalibrationWeights = (patch: Partial<ConfigPayload["calibration"]["weights"]>) =>
    setSettings({
      ...settings,
      calibration: {
        ...settings.calibration,
        weights: { ...settings.calibration.weights, ...patch },
      },
    });

  return (
    <section className="panel settings-panel">
      <PanelTitle icon={<Settings size={18} />} title="Settings" actionLabel="writes .iriscope.toml" />
      <h3>Pi</h3>
      <div className="form-grid">
        <Field label="Host">
          <input value={settings.pi.host ?? ""} onChange={(event) => updatePi({ host: event.target.value || null })} />
        </Field>
        <Field label="User">
          <input value={settings.pi.user} onChange={(event) => updatePi({ user: event.target.value })} />
        </Field>
        <Field label="Port">
          <input type="number" min={1} value={settings.pi.port} onChange={(event) => updatePi({ port: Number(event.target.value) })} />
        </Field>
        <Field label="Remote root">
          <input value={settings.pi.remote_root} onChange={(event) => updatePi({ remote_root: event.target.value })} />
        </Field>
        <Field label="SSH key">
          <input value={settings.pi.ssh_key ?? ""} onChange={(event) => updatePi({ ssh_key: event.target.value || null })} />
        </Field>
        <Field label="Connect timeout">
          <input
            type="number"
            min={1}
            value={settings.pi.connect_timeout}
            onChange={(event) => updatePi({ connect_timeout: Number(event.target.value) })}
          />
        </Field>
      </div>

      <h3>Capture</h3>
      <div className="form-grid">
        <Field label="Frames">
          <input type="number" min={1} value={settings.capture.count} onChange={(event) => updateCapture({ count: Number(event.target.value) })} />
        </Field>
        <Field label="Shutter us">
          <input
            type="number"
            min={0}
            title="0 lets rpicam choose exposure automatically"
            value={settings.capture.shutter_us}
            onChange={(event) => updateCapture({ shutter_us: Number(event.target.value) })}
          />
        </Field>
        <Field label="ISO equiv">
          <input
            type="number"
            min={0}
            step={50}
            title="Blank or 0 lets rpicam choose analogue gain automatically"
            value={settings.capture.gain > 0 ? Math.round(settings.capture.gain * 100) : ""}
            onChange={(event) => updateCapture({ gain: event.target.value ? Number(event.target.value) / 100 : 0 })}
          />
        </Field>
        <Field label="Gain">
          <input type="number" min={0} step="0.1" value={settings.capture.gain} onChange={(event) => updateCapture({ gain: Number(event.target.value) })} />
        </Field>
        <Field label="AWB mode">
          <select value={settings.capture.awb} onChange={(event) => updateCapture({ awb: event.target.value as AwbMode })}>
            {awbModes.map((mode) => (
              <option value={mode.value} key={mode.value}>
                {mode.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="AWB red">
          <input
            type="number"
            step="0.1"
            disabled={settings.capture.awb !== "manual"}
            value={awbGain(settings.capture.awb_gains, 0)}
            onChange={(event) => updateCapture({ awb_gains: [Number(event.target.value), awbGain(settings.capture.awb_gains, 1)] })}
          />
        </Field>
        <Field label="AWB blue">
          <input
            type="number"
            step="0.1"
            disabled={settings.capture.awb !== "manual"}
            value={awbGain(settings.capture.awb_gains, 1)}
            onChange={(event) => updateCapture({ awb_gains: [awbGain(settings.capture.awb_gains, 0), Number(event.target.value)] })}
          />
        </Field>
        <Field label="Metering">
          <select value={settings.capture.metering} onChange={(event) => updateCapture({ metering: event.target.value as MeteringMode })}>
            {meteringModes.map((mode) => (
              <option value={mode.value} key={mode.value}>
                {mode.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Exposure">
          <select value={settings.capture.exposure} onChange={(event) => updateCapture({ exposure: event.target.value as ExposureMode })}>
            {exposureModes.map((mode) => (
              <option value={mode.value} key={mode.value}>
                {mode.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="EV">
          <input type="number" step="0.1" min={-10} max={10} value={settings.capture.ev} onChange={(event) => updateCapture({ ev: Number(event.target.value) })} />
        </Field>
        <Field label="Denoise">
          <select value={settings.capture.denoise} onChange={(event) => updateCapture({ denoise: event.target.value as ConfigPayload["capture"]["denoise"] })}>
            <option value="off">Off</option>
            <option value="auto">Auto</option>
            <option value="cdn_off">CDN off</option>
            <option value="cdn_fast">CDN fast</option>
            <option value="cdn_hq">CDN high quality</option>
          </select>
        </Field>
        <Field label="Quality">
          <input type="number" min={1} max={100} value={settings.capture.quality} onChange={(event) => updateCapture({ quality: Number(event.target.value) })} />
        </Field>
        <Field label="Brightness">
          <input type="number" step="0.05" min={-1} max={1} value={settings.capture.brightness} onChange={(event) => updateCapture({ brightness: Number(event.target.value) })} />
        </Field>
        <Field label="Contrast">
          <input type="number" step="0.1" min={0} value={settings.capture.contrast} onChange={(event) => updateCapture({ contrast: Number(event.target.value) })} />
        </Field>
        <Field label="Saturation">
          <input type="number" step="0.1" min={0} value={settings.capture.saturation} onChange={(event) => updateCapture({ saturation: Number(event.target.value) })} />
        </Field>
        <Field label="Sharpness">
          <input type="number" step="0.1" min={0} value={settings.capture.sharpness} onChange={(event) => updateCapture({ sharpness: Number(event.target.value) })} />
        </Field>
        <Field label="Tuning file">
          <select value={settings.capture.tuning_file ?? ""} onChange={(event) => updateCapture({ tuning_file: event.target.value || null })}>
            {tuningFileOptions.map((option) => (
              <option value={option.value} key={option.value || "default"}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Sensor mode">
          <input value={settings.capture.mode ?? ""} onChange={(event) => updateCapture({ mode: event.target.value || null })} />
        </Field>
        <Field label="HDR">
          <select value={settings.capture.hdr} onChange={(event) => updateCapture({ hdr: event.target.value as HdrMode })}>
            {hdrModes.map((mode) => (
              <option value={mode.value} key={mode.value}>
                {mode.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Width">
          <input
            type="number"
            value={settings.capture.width ?? ""}
            onChange={(event) => updateCapture({ width: event.target.value ? Number(event.target.value) : null })}
          />
        </Field>
        <Field label="Height">
          <input
            type="number"
            value={settings.capture.height ?? ""}
            onChange={(event) => updateCapture({ height: event.target.value ? Number(event.target.value) : null })}
          />
        </Field>
      </div>
      <label className="checkbox-line">
        <input type="checkbox" checked={settings.capture.raw} onChange={(event) => updateCapture({ raw: event.target.checked })} />
        Capture RAW/DNG
      </label>
      <label className="checkbox-line">
        <input type="checkbox" checked={settings.capture.nopreview} onChange={(event) => updateCapture({ nopreview: event.target.checked })} />
        Disable rpicam preview window
      </label>

      <h3>Preview</h3>
      <div className="form-grid">
        <Field label="Width">
          <input type="number" min={64} value={settings.preview.width} onChange={(event) => updatePreview({ width: Number(event.target.value) })} />
        </Field>
        <Field label="Height">
          <input type="number" min={64} value={settings.preview.height} onChange={(event) => updatePreview({ height: Number(event.target.value) })} />
        </Field>
        <Field label="Framerate">
          <input type="number" min={1} value={settings.preview.framerate} onChange={(event) => updatePreview({ framerate: Number(event.target.value) })} />
        </Field>
        <Field label="Quality">
          <input type="number" min={1} max={100} value={settings.preview.quality} onChange={(event) => updatePreview({ quality: Number(event.target.value) })} />
        </Field>
        <Field label="Stream timeout s">
          <input
            type="number"
            min={0}
            value={settings.preview.stream_timeout_s}
            onChange={(event) => updatePreview({ stream_timeout_s: Number(event.target.value) })}
          />
        </Field>
      </div>

      <h3>Calibration</h3>
      <div className="form-grid">
        <Field label="Target luma min">
          <input
            type="number"
            min={0}
            max={1}
            step="0.01"
            value={settings.calibration.target_luma_min}
            onChange={(event) => updateCalibration({ target_luma_min: Number(event.target.value) })}
          />
        </Field>
        <Field label="Target luma max">
          <input
            type="number"
            min={0}
            max={1}
            step="0.01"
            value={settings.calibration.target_luma_max}
            onChange={(event) => updateCalibration({ target_luma_max: Number(event.target.value) })}
          />
        </Field>
        <Field label="Max clipping">
          <input
            type="number"
            min={0}
            max={1}
            step="0.005"
            value={settings.calibration.max_clip_fraction}
            onChange={(event) => updateCalibration({ max_clip_fraction: Number(event.target.value) })}
          />
        </Field>
        <Field label="Sample budget">
          <input
            type="number"
            min={2}
            max={40}
            value={settings.calibration.sample_budget}
            onChange={(event) => updateCalibration({ sample_budget: Number(event.target.value) })}
          />
        </Field>
        <Field label="Max shutter us">
          <input
            type="number"
            min={1}
            value={settings.calibration.max_shutter_us}
            onChange={(event) => updateCalibration({ max_shutter_us: Number(event.target.value) })}
          />
        </Field>
        <Field label="Max gain">
          <input
            type="number"
            min={0.1}
            step="0.1"
            value={settings.calibration.max_gain}
            onChange={(event) => updateCalibration({ max_gain: Number(event.target.value) })}
          />
        </Field>
        <Field label="Command timeout s">
          <input
            type="number"
            min={5}
            value={settings.calibration.command_timeout_s}
            onChange={(event) => updateCalibration({ command_timeout_s: Number(event.target.value) })}
          />
        </Field>
        <Field label="Thumbnail edge">
          <input
            type="number"
            min={64}
            value={settings.calibration.thumbnail_edge}
            onChange={(event) => updateCalibration({ thumbnail_edge: Number(event.target.value) })}
          />
        </Field>
      </div>
      <label className="checkbox-line">
        <input
          type="checkbox"
          checked={settings.calibration.retain_artifacts}
          onChange={(event) => updateCalibration({ retain_artifacts: event.target.checked })}
        />
        Keep calibration reports and thumbnails
      </label>
      <details className="advanced-controls">
        <summary>
          <SlidersHorizontal size={16} />
          <span>
            <strong>Calibration scoring</strong>
            <small>Weights for recommendation ranking</small>
          </span>
        </summary>
        <div className="form-grid">
          {(["luma", "clipping", "focus", "mask", "color", "gain", "metadata"] as const).map((key) => (
            <Field label={`${key} weight`} key={key}>
              <input
                type="number"
                min={0}
                step="0.01"
                value={settings.calibration.weights[key]}
                onChange={(event) => updateCalibrationWeights({ [key]: Number(event.target.value) })}
              />
            </Field>
          ))}
        </div>
      </details>

      <h3>Processing Defaults</h3>
      <div className="form-grid">
        <Field label="Stack method">
          <select
            value={settings.processing.stack_method}
            onChange={(event) => updateProcessing({ stack_method: event.target.value as ConfigPayload["processing"]["stack_method"] })}
          >
            <option value="sigma">Sigma clip</option>
            <option value="median">Median</option>
            <option value="mean">Mean</option>
          </select>
        </Field>
        <Field label="Sigma">
          <input type="number" step="0.1" min={0.1} value={settings.processing.sigma} onChange={(event) => updateProcessing({ sigma: Number(event.target.value) })} />
        </Field>
        <Field label="Min frames">
          <input type="number" min={1} value={settings.processing.min_frames} onChange={(event) => updateProcessing({ min_frames: Number(event.target.value) })} />
        </Field>
        <Field label="Max working edge">
          <input
            type="number"
            min={64}
            value={settings.processing.max_working_edge ?? ""}
            onChange={(event) => updateProcessing({ max_working_edge: event.target.value ? Number(event.target.value) : null })}
          />
        </Field>
      </div>
      <label className="checkbox-line">
        <input
          type="checkbox"
          checked={settings.processing.save_intermediates}
          onChange={(event) => updateProcessing({ save_intermediates: event.target.checked })}
        />
        Save stacked image and iris mask
      </label>
      <button className="primary full" disabled={busy !== null} onClick={() => void onSave()}>
        <Save size={17} />
        Save Settings
      </button>
    </section>
  );
}

function HealthPanel({ status }: { status: StatusResponse | null }) {
  const checks: Array<[string, HealthCheck | undefined]> = [
    ["SSH", status?.health?.ssh],
    ["rpicam-hello", status?.health?.rpicam],
    ["Preview frame", status?.health?.preview],
    ["Remote disk", status?.health?.disk],
    ["Windows PnP", status?.health?.windows_pnp],
  ];
  return (
    <section className="panel health-panel">
      <PanelTitle icon={<Activity size={18} />} title="Health Checks" />
      <div className="health-list">
        {checks.map(([label, check]) => (
          <div className={`health-row ${healthTone(check)}`} key={label}>
            <strong>{label}</strong>
            <span>{check?.message ?? "Not checked yet."}</span>
          </div>
        ))}
      </div>
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
  const latestWarning = logs.find((item) => item.level === "warn" || item.level === "error");
  return (
    <details className="panel log-panel">
      <summary className="log-summary">
        <span>
          <Terminal size={18} />
          <strong>Run Log</strong>
        </span>
        <small>{latestWarning ? latestWarning.message : `${logs.length} events`}</small>
      </summary>
      <div className="logs">
        {logs.map((item) => (
          <div className={`log-line ${item.level}`} key={item.id}>
            <span>{item.time}</span>
            <p>{item.message}</p>
          </div>
        ))}
      </div>
    </details>
  );
}

function QualityStrip({
  preprocess,
  liveQuality,
}: {
  preprocess: PreprocessReport | null;
  liveQuality: LiveQualityReading;
}) {
  const focusValue = liveQuality.score !== null
    ? formatSharpnessScore(liveQuality.score)
    : preprocess
      ? preprocess.summary.focus_score_median.toFixed(1)
      : liveMetricPlaceholder(liveQuality);
  const lumaValue = liveQuality.meanLuma !== null
    ? liveQuality.meanLuma.toFixed(2)
    : preprocess
      ? preprocess.summary.mean_luma_median.toFixed(2)
      : liveMetricPlaceholder(liveQuality);
  const readyValue = liveQuality.ready !== null
    ? liveQuality.ready
      ? "yes"
      : "no"
    : preprocess
      ? preprocess.summary.ready_for_stack
        ? "yes"
        : "no"
      : liveMetricPlaceholder(liveQuality);

  return (
    <div className="quality-strip">
      <Metric label="Focus" value={focusValue} />
      <Metric label="Luma" value={lumaValue} />
      <Metric label="Ready" value={readyValue} />
    </div>
  );
}

function liveMetricPlaceholder(reading: LiveQualityReading) {
  return reading.tone === "unavailable" ? "-" : "measuring";
}

function buildWorkflowSteps({
  piReady,
  livePreviewReady,
  selectedRecord,
  selectedSession,
  preprocess,
  currentProcessResult,
  label,
}: {
  piReady: boolean;
  livePreviewReady: boolean;
  selectedRecord: SessionRecord | undefined;
  selectedSession: string;
  preprocess: PreprocessReport | null;
  currentProcessResult: ProcessResponse | null;
  label: LabelRecord;
}): WorkflowStep[] {
  const hasSession = Boolean(selectedSession || selectedRecord);
  const preprocessed = Boolean(preprocess || selectedRecord?.preprocessed);
  const processed = Boolean(currentProcessResult || selectedRecord?.processed);
  const labeled = Boolean(selectedRecord?.labeled || label.updated_at || label.consent_recorded);
  const previewReady = piReady && livePreviewReady;
  const currentKey = !previewReady
    ? "preview"
    : !hasSession
      ? "capture"
      : !preprocessed
        ? "inspect"
        : !processed
          ? "process"
          : !labeled
            ? "label"
            : "review";

  const rawSteps = [
    {
      key: "preview",
      view: "capture" as ActiveView,
      label: "Preview ready",
      detail: previewReady ? "Focus and exposure visible" : "Confirm camera feed",
    },
    {
      key: "capture",
      view: "capture" as ActiveView,
      label: "Capture stack",
      detail: hasSession ? "Session selected" : "Create a frame stack",
    },
    {
      key: "inspect",
      view: "preprocess" as ActiveView,
      label: "Inspect frames",
      detail: preprocessed ? "Frame checks complete" : "Check focus and mask",
    },
    {
      key: "process",
      view: "preprocess" as ActiveView,
      label: "Process output",
      detail: processed ? "Enhanced output ready" : "Stack and enhance",
    },
    {
      key: "label",
      view: "label" as ActiveView,
      label: "Govern labels",
      detail: labeled ? "Label saved" : "Consent and use metadata",
    },
    {
      key: "review",
      view: "review" as ActiveView,
      label: "Review export",
      detail: processed ? "Open artifacts" : "Needs processed output",
    },
  ];
  const currentIndex = rawSteps.findIndex((step) => step.key === currentKey);
  return rawSteps.map((step, index) => ({
    ...step,
    state: index < currentIndex ? "done" : index === currentIndex ? "current" : "waiting",
  }));
}

function statusSummary(
  status: StatusResponse | null,
  serialPort: string,
  cameraName: string,
  moduleCount: number,
  moduleTotal: number,
) {
  if (!status) {
    return { tone: "warn" as const, label: "Checking system", detail: "Status pending" };
  }
  const checks: Array<"ok" | "warn" | "error"> = [
    healthTone(status.health?.ssh),
    healthTone(status.health?.rpicam),
    healthTone(status.health?.preview),
    healthTone(status.health?.disk),
    healthTone(status.health?.windows_pnp),
    serialPort === "not detected" ? "warn" : "ok",
    cameraName === "no camera" ? "warn" : "ok",
    moduleCount === moduleTotal ? "ok" : "warn",
  ];
  const errors = checks.filter((tone) => tone === "error").length;
  const warnings = checks.filter((tone) => tone === "warn").length;
  if (errors > 0) {
    return { tone: "error" as const, label: `${errors} system issue${errors === 1 ? "" : "s"}`, detail: "Open diagnostics" };
  }
  if (warnings > 0) {
    return { tone: "warn" as const, label: `${warnings} item${warnings === 1 ? "" : "s"} to check`, detail: "Open diagnostics" };
  }
  return { tone: "ok" as const, label: "System ready", detail: "Diagnostics available" };
}

function previewReadinessCopy(reading: LiveQualityReading, snapshotFailed: boolean) {
  if (snapshotFailed || reading.tone === "unavailable") {
    return {
      tone: "error" as const,
      frame: "unavailable",
      guidance: "Refresh the preview after checking Pi power, SSH, and camera ownership.",
    };
  }
  if (reading.tone === "measuring") {
    return {
      tone: "warn" as const,
      frame: "measuring",
      guidance: "Hold the subject steady while Iriscope samples focus and exposure.",
    };
  }
  if (reading.ready) {
    return {
      tone: "ok" as const,
      frame: "ready",
      guidance: "Focus, luminance, and clipping are within the configured capture thresholds.",
    };
  }
  if (reading.tone === "soft") {
    return {
      tone: "warn" as const,
      frame: "soft focus",
      guidance: "Refocus the lens or stabilize the head support before capturing the stack.",
    };
  }
  return {
    tone: "warn" as const,
    frame: "review",
    guidance: "Check luma and clipping before capture; the frame is close but not fully ready.",
  };
}

function transportLabel(previewMode: PreviewMode, snapshotFailed: boolean) {
  if (snapshotFailed) {
    return "offline";
  }
  if (previewMode === "webrtc") {
    return "WebRTC";
  }
  if (previewMode === "stream") {
    return "MJPEG";
  }
  return "still";
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

function formatMetric(value: number | null | undefined, digits: number) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "-";
}

function formatPercent(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 1000) / 10}%` : "-";
}

function formatScore(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "-";
}

function formatCalibrationValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => formatCalibrationValue(item)).join(", ");
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  }
  if (value === null || value === undefined || value === "") {
    return "auto";
  }
  return String(value);
}

function now() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function logId() {
  logSequence += 1;
  return `${Date.now()}-${logSequence}`;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function healthTone(check: HealthCheck | undefined): "ok" | "warn" | "error" {
  if (!check) {
    return "warn";
  }
  if (check.ok) {
    return "ok";
  }
  return check.status === "skipped" || check.status === "warming_up" || check.status === "not_applicable" || check.status === "busy"
    ? "warn"
    : "error";
}

function healthLabel(check: HealthCheck | undefined) {
  if (!check) {
    return "pending";
  }
  if (check.ok) {
    return "ok";
  }
  return check.status.replaceAll("_", " ");
}

function diskLabel(check: HealthCheck | undefined) {
  if (!check) {
    return "pending";
  }
  if (typeof check.free_gb === "number") {
    return `${check.free_gb.toFixed(1)} GB`;
  }
  return healthLabel(check);
}

function qualityStatusLabel(status: ProcessResponse["quality_status"]) {
  if (status === "requires_recapture") {
    return "Requires recapture";
  }
  if (status === "review") {
    return "Needs review";
  }
  if (status === "pass") {
    return "Quality pass";
  }
  return "Quality unknown";
}

function parseTags(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function captureFromConfig(config: ConfigPayload): CaptureForm {
  const capture = config.capture;
  return {
    subject: defaultCapture.subject,
    eye: defaultCapture.eye,
    count: capture.count,
    shutter_us: capture.shutter_us,
    gain: capture.gain,
    awb: capture.awb ?? defaultCapture.awb,
    awb_red: awbGain(capture.awb_gains, 0),
    awb_blue: awbGain(capture.awb_gains, 1),
  };
}

function processFromConfig(config: ConfigPayload): ProcessOptions {
  const processing = config.processing;
  return {
    stack_method: processing.stack_method,
    sigma: processing.sigma,
    min_frames: processing.min_frames,
    save_intermediates: processing.save_intermediates,
    max_working_edge: processing.max_working_edge ?? null,
    dark_path: "",
    flat_path: "",
  };
}

function withSessionFallback(label: LabelRecord, sessionDir: string): LabelRecord {
  const sessionName = sessionDir.split(/[\\/]/).filter(Boolean).at(-1) ?? "";
  const parts = sessionName.split("_");
  const inferredEye = parts.includes("left") ? "left" : parts.includes("right") ? "right" : "";
  return {
    ...label,
    subject_code: label.subject_code || parts[0] || "",
    eye: label.eye || inferredEye,
  };
}

function outputPath(outputs: Record<string, string | null | undefined>, key: string) {
  return outputs[key] || "";
}

function withLivePreviewOk(status: StatusResponse | null): StatusResponse | null {
  if (!status) {
    return status;
  }
  return {
    ...status,
    health: {
      ...status.health,
      preview: {
        ok: true,
        status: "ok",
        message: "WebRTC preview frame received.",
      },
    },
  };
}

function awbGain(gains: [number, number] | number[] | null | undefined, index: 0 | 1) {
  const fallback = index === 0 ? defaultCapture.awb_red : defaultCapture.awb_blue;
  return Number(gains?.[index] ?? fallback);
}
