import { useCallback, useEffect, useRef, useState } from "react";
import { QrCode, RotateCw, WifiOff } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { api, ApiError } from "../../core/api";
import { useToast } from "../../ui/toast";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import { Modal } from "./Modal";

interface PairPayload {
  type?: "status" | "log" | "done";
  status?: string;
  line?: string;
  qr?: string;
  jid?: string;
  ok?: boolean;
  reason?: string;
}

export function WhatsAppPairModal({
  agentName,
  port,
  onClose,
  onRestarted,
}: {
  agentName: string;
  port?: number;
  onClose: () => void;
  onRestarted: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const abortRef = useRef<AbortController | null>(null);
  const [status, setStatus] = useState(t("wa.status.starting"));
  const [statusKind, setStatusKind] = useState("starting");
  const [qr, setQr] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [recoverable, setRecoverable] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [paired, setPaired] = useState(false);
  const bridgePort = port || 3000;

  const appendLog = useCallback((line: string) => {
    setLogs((current) => [...current.slice(-120), line]);
  }, []);

  const startPairing = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStatus(t("wa.status.starting"));
    setStatusKind("starting");
    setQr("");
    setRecoverable(false);
    setPaired(false);

    const qs = new URLSearchParams({ agent: agentName, port: String(bridgePort), timeout: "180" });
    try {
      const resp = await fetch(`/api/connectors/whatsapp/pair?${qs.toString()}`, {
        credentials: "include",
        headers: { Accept: "text/event-stream" },
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`Pairing stream failed: HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames)
          handleFrame(frame, t, appendLog, setQr, setStatus, setStatusKind, setRecoverable, setPaired);
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      const msg = e instanceof Error ? e.message : t("csetup.pairingStreamFailed");
      appendLog(t("csetup.errorLog", { message: msg }));
      setStatus(t("csetup.streamError", { msg }));
      setStatusKind("error");
      setRecoverable(/logged|session|bridge/i.test(msg));
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
    }
  }, [agentName, appendLog, bridgePort, t]);

  useEffect(() => {
    void startPairing();
    return () => abortRef.current?.abort();
  }, [startPairing]);

  async function resetSession() {
    setResetting(true);
    setStatus(t("wa.status.resettingSession"));
    setStatusKind("starting");
    appendLog(t("wa.log.resetRequesting", { name: agentName }));
    try {
      const result = await api.post<{ steps?: string[]; errors?: string[]; restarted_agent?: boolean }>(
        `/api/connectors/whatsapp/reset?${new URLSearchParams({ agent: agentName }).toString()}`,
      );
      result.steps?.forEach((step) => appendLog(t("wa.log.resetStep", { step })));
      result.errors?.forEach((err) => appendLog(t("wa.log.resetWarning", { err })));
      await startPairing();
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : t("wa.err.resetFailed");
      appendLog(t("wa.log.resetFailed", { msg }));
      setStatus(t("wa.resetFailedStatus", { msg }));
      setStatusKind("error");
    } finally {
      setResetting(false);
    }
  }

  async function restartAgent() {
    try {
      await api.post(`/api/agents/${encodeURIComponent(agentName)}/restart`);
      toast.push(t("wa.toast.restarted", { name: agentName }));
      onRestarted();
      onClose();
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : t("wa.err.restartFailed");
      toast.push(msg, "error");
    }
  }

  return (
    <Modal
      title={t("wa.title")}
      subtitle={t("wa.subtitle", { port: String(bridgePort), agent: agentName })}
      onClose={onClose}
      width={680}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            {t("card.close")}
          </button>
          {recoverable ? (
            <button className="btn" onClick={() => void resetSession()} disabled={resetting}>
              {resetting ? t("wa.resetting") : t("wa.resetSession")}
            </button>
          ) : null}
          {paired ? (
            <button className="btn primary" onClick={() => void restartAgent()}>
              {t("csetup.restartAgent")}
            </button>
          ) : null}
        </>
      }
    >
      <div className="wa-pair">
        <div className={`wa-pair__status wa-pair__status--${statusKind}`}>
          <QrCode size={16} strokeWidth={1.75} />
          <span>{status}</span>
        </div>

        <div className="wa-pair__qr-wrap">
          {qr ? (
            <div className="wa-pair__qr">
              <QRCodeSVG value={qr} size={260} marginSize={2} />
              <p className="muted">{t("wa.qr.hint")}</p>
            </div>
          ) : recoverable ? (
            <div className="wa-pair__empty">
              <WifiOff size={34} strokeWidth={1.5} />
              <strong>{t("wa.recoverable.title")}</strong>
              <p>{t("wa.recoverable.hint")}</p>
            </div>
          ) : (
            <div className="wa-pair__empty">
              <RotateCw size={34} strokeWidth={1.5} />
              <strong>{t("wa.waiting.title")}</strong>
              <p>{t("wa.waiting.hint")}</p>
            </div>
          )}
        </div>

        <details className="wa-pair__log-wrap">
          <summary>{t("csetup.technicalLog")}</summary>
          <pre className="wa-pair__log">{logs.join("\n") || t("csetup.noLogLines")}</pre>
        </details>
      </div>
    </Modal>
  );
}

function handleFrame(
  frame: string,
  t: ReturnType<typeof useT>,
  appendLog: (line: string) => void,
  setQr: (qr: string) => void,
  setStatus: (status: string) => void,
  setStatusKind: (kind: string) => void,
  setRecoverable: (recoverable: boolean) => void,
  setPaired: (paired: boolean) => void,
) {
  const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
  if (!dataLine) return;
  let payload: PairPayload;
  try {
    payload = JSON.parse(dataLine.slice(5).trim()) as PairPayload;
  } catch {
    return;
  }
  if (payload.type === "log" && payload.line) {
    appendLog(payload.line);
    return;
  }
  if (payload.type === "status") {
    const status = payload.status || "connecting";
    setStatusKind(status === "logged_out" ? "error" : status);
    if (status === "starting") setStatus(t("wa.status.starting"));
    else if (status === "connecting") setStatus(t("wa.status.connecting"));
    else if (status === "qr") setStatus(t("wa.status.scanQr"));
    else if (status === "connected")
      setStatus(payload.jid ? t("wa.status.connectedAs", { jid: payload.jid }) : t("wa.status.connected"));
    else if (status === "logged_out") {
      setStatus(t("wa.status.loggedOut"));
      setRecoverable(true);
    } else setStatus(status);
    if (payload.qr) setQr(payload.qr);
    return;
  }
  if (payload.type === "done") {
    if (payload.ok) {
      setStatus(t("wa.status.paired"));
      setStatusKind("connected");
      setPaired(true);
      appendLog(t("wa.log.donePaired"));
    } else {
      const reason = payload.reason || t("csetup.pairingFailed");
      setStatus(reason);
      setStatusKind("error");
      setRecoverable(/Bridge exited|logged|session/i.test(reason));
      appendLog(t("wa.log.doneReason", { reason }));
    }
  }
}
