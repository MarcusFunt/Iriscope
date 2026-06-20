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
} from "lucide-react";
import {
  apiPost,
  artifactUrl,
  createPiWebRTCAnswer,
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
} from "./api";
import type {
  AwbMode,
  ConfigPayload,
  ExposureMode,
  HdrMode,
  HealthCheck,
  LabelRecord,
  MeteringMode,
  PreprocessReport,
  ProcessOptions,
  ProcessResponse,
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
  const [logs, setLogs] = useState<LogItem[]>([
    { time: now(), level: "info", message: "Iriscope host interface ready." },
  ]);
  const [busy, setBusy] = useState<string | null>(null);
  const [snapshotNonce, setSnapshotNonce] = useState(Date.now());
  const [previewMode, setPreviewMode] = useState<PreviewMode>("webrtc");
  const [snapshotFailed, setSnapshotFailed] = useState(false);
  const [livePreviewReady, setLivePreviewReady] = useState(false);

  const appendLog = useCallback((level: LogItem["level"], message: string) => {
    setLogs((items) => [{ time: now(), level, message }, ...items].slice(0, 80));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextSessions, nextConfig] = await Promise.all([getStatus(), getSessions(), getConfig()]);
      setStatus(nextStatus);
      setSessions(nextSessions);
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
  const piReady = Boolean(displayStatus?.health?.ssh?.ok && displayStatus?.health?.rpicam?.ok);
  const previewLabel = piConfigured ? "Pi HQ camera" : cameraName;
  const previewSourceKey = piConfigured ? `pi:${displayStatus?.config.pi_host ?? ""}` : `uvc:${cameraName}`;
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

  useEffect(() => {
    setLivePreviewReady(false);
    setSnapshotFailed(false);
    setPreviewMode(piConfigured ? "webrtc" : "snapshot");
    setSnapshotNonce(Date.now());
  }, [piConfigured, previewSourceKey]);

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
    setPreviewMode(piConfigured ? "webrtc" : "snapshot");
    setSnapshotNonce(Date.now());
  }, [piConfigured]);

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
      <Sidebar activeView={activeView} onChange={setActiveView} />
      <main className="workspace">
        <TopBar status={displayStatus} serialPort={serialPort} cameraName={cameraName} onRefresh={refresh} />

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
                nonce={snapshotNonce}
                onStreamFallback={handleStreamFallback}
                onWebRTCFallback={handleWebRTCFallback}
                onPreviewFailed={handlePreviewFailed}
                onPreviewReady={handlePreviewReady}
                onRefreshPreview={handleRefreshPreview}
              />
              <CapturePanel
                capture={capture}
                setCapture={setCapture}
                busy={busy}
                piReady={piReady}
                onCalibrate={() =>
                  runAction("Calibration", () => apiPost("/api/calibrate"), (result) => {
                    appendLog("info", JSON.stringify(result));
                    setPreviewMode(piConfigured ? "webrtc" : "snapshot");
                    setSnapshotFailed(false);
                    setSnapshotNonce(Date.now());
                  })
                }
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
                      setPreviewMode(piConfigured ? "webrtc" : "snapshot");
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
  return (
    <header className="topbar">
      <div>
        <h1>Capture workstation</h1>
        <p>{status?.capture_root ?? "captures"} </p>
      </div>
      <div className="status-strip">
        <StatusPill tone={healthTone(status?.health?.ssh)} icon={<Terminal size={16} />} label={`SSH ${healthLabel(status?.health?.ssh)}`} />
        <StatusPill tone={healthTone(status?.health?.rpicam)} icon={<ScanEye size={16} />} label={`Camera ${healthLabel(status?.health?.rpicam)}`} />
        <StatusPill tone={healthTone(status?.health?.preview)} icon={<Eye size={16} />} label={`Preview ${healthLabel(status?.health?.preview)}`} />
        <StatusPill tone={healthTone(status?.health?.disk)} icon={<HardDrive size={16} />} label={`Disk ${diskLabel(status?.health?.disk)}`} />
        <StatusPill tone={healthTone(status?.health?.windows_pnp)} icon={<CircleAlert size={16} />} label={`PnP ${healthLabel(status?.health?.windows_pnp)}`} />
        <StatusPill tone={serialPort === "not detected" ? "warn" : "ok"} icon={<Terminal size={16} />} label={serialPort} />
        <StatusPill tone={cameraName === "no camera" ? "warn" : "ok"} icon={<ScanEye size={16} />} label={cameraName} />
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

function PreviewPanel({
  previewLabel,
  previewSrc,
  preprocess,
  previewMode,
  snapshotFailed,
  piConfigured,
  nonce,
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
  nonce: number;
  onStreamFallback: () => void;
  onWebRTCFallback: (message: string) => void;
  onPreviewFailed: () => void;
  onPreviewReady: () => void;
  onRefreshPreview: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [webrtcReady, setWebrtcReady] = useState(false);

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
            src={previewSrc}
            alt="Live camera preview"
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
      </div>
      {previewMode === "webrtc" && !snapshotFailed ? (
        <p className={webrtcReady ? "preview-message ok" : "preview-message"}>
          {webrtcReady ? "WebRTC preview active." : "Starting WebRTC preview..."}
        </p>
      ) : null}
      {previewMode === "stream" && !snapshotFailed ? <p className="preview-message">Using MJPEG preview fallback.</p> : null}
      {previewMode === "snapshot" && !snapshotFailed ? (
        <p className="preview-message">Live stream unavailable. Showing still preview fallback.</p>
      ) : null}
      {snapshotFailed ? <p className="preview-message">Preview unavailable. Check Pi SSH key access and refresh.</p> : null}
      <QualityStrip preprocess={preprocess} />
    </div>
  );
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
