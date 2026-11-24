import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, QrCode, RotateCw } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import { Modal } from "./Modal";

interface PairPayload {
  type?: "qr" | "status" | "done";
  qr?: string;
  refresh?: number;
  status?: string;
  ok?: boolean;
  account_id?: string;
  reason?: string;
}

/**
 * In-dashboard QR login for the personal-WeChat (iLink) connector.
 *
 * There is no console to copy keys from: Tencent mints the credentials when
 * the user scans this QR with WeChat and confirms. The server persists them
 * (the token never reaches the browser). On link this modal hands the account
 * id to the parent (`onLinked`), which pins it into the connector and saves —
 * so the flow finishes like every other connector: the dialog closes on Done
 * and the "restart the agent to apply it" prompt happens outside, on the
 * agent page. No restart button in here.
 */
export function WeixinPairModal({
  agentName,
  onClose,
  onLinked,
}: {
  agentName: string;
  onClose: () => void;
  /**
   * Called once with the just-linked iLink account id so the parent can pin
   * it into the connector config and SAVE it. Returns whether the save
   * landed; the Done button only appears after it has.
   */
  onLinked: (accountId: string) => Promise<boolean>;
}) {
  const t = useT(STR);
  const abortRef = useRef<AbortController | null>(null);
  const [status, setStatus] = useState(t("wx.status.requestingQr"));
  const [statusKind, setStatusKind] = useState("starting");
  const [qr, setQr] = useState("");
  const [accountId, setAccountId] = useState("");
  const [failed, setFailed] = useState(false);
  const [linked, setLinked] = useState(false);
  // "idle" until linked; then "saving" while the parent persists the pin,
  // "saved" on success, "error" if the save failed (connector left as-is).
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");

  const startPairing = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStatus(t("wx.status.requestingQr"));
    setStatusKind("starting");
    setQr("");
    setFailed(false);
    setLinked(false);

    const qs = new URLSearchParams({ agent: agentName, timeout: "480" });
    try {
      const resp = await fetch(`/api/connectors/weixin/pair?${qs.toString()}`, {
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
          handleFrame(frame, t, setQr, setStatus, setStatusKind, setFailed, setLinked, setAccountId);
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      const msg = e instanceof Error ? e.message : t("csetup.pairingStreamFailed");
      setStatus(t("csetup.streamError", { msg }));
      setStatusKind("error");
      setFailed(true);
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
    }
  }, [agentName, t]);

  useEffect(() => {
    void startPairing();
    return () => abortRef.current?.abort();
  }, [startPairing]);

  // On a successful link, hand the account id to the parent to pin + save the
  // connector, and keep the status line in step — it must never sit on
  // "Saving…" after the save has resolved. Fire exactly once (onLinked's
  // identity changes each render, so guard with a ref, not the effect deps).
  const linkHandledRef = useRef(false);
  useEffect(() => {
    if (!linked || !accountId || linkHandledRef.current) return;
    linkHandledRef.current = true;
    setSaveState("saving");
    onLinked(accountId)
      .then((ok) => {
        setSaveState(ok ? "saved" : "error");
        setStatus(ok ? t("wx.status.linkedSaved") : t("wx.status.linkedSaveFailed"));
        setStatusKind(ok ? "connected" : "error");
      })
      .catch(() => {
        setSaveState("error");
        setStatus(t("wx.status.linkedSaveFailed"));
        setStatusKind("error");
      });
  }, [linked, accountId, onLinked, t]);

  return (
    <Modal
      title={t("wx.title")}
      subtitle={agentName}
      onClose={onClose}
      width={680}
      footer={
        <>
          {failed ? (
            <button className="btn" onClick={() => void startPairing()}>
              {t("wx.tryAgain")}
            </button>
          ) : null}
          {saveState === "saving" ? (
            <button className="btn primary" disabled>
              {t("wx.saving")}
            </button>
          ) : saveState === "saved" ? (
            <button className="btn primary" onClick={onClose}>
              {t("wx.done")}
            </button>
          ) : (
            <button className="btn" onClick={onClose}>
              {saveState === "error" ? t("card.close") : t("common.cancel")}
            </button>
          )}
        </>
      }
    >
      <div className="wa-pair">
        <div className={`wa-pair__status wa-pair__status--${statusKind}`}>
          <QrCode size={16} strokeWidth={1.75} />
          <span>{status}</span>
        </div>

        <div className="wa-pair__qr-wrap">
          {linked ? (
            <div className="wa-pair__empty">
              <CheckCircle2 size={34} strokeWidth={1.5} />
              <strong>
                {accountId ? t("wx.linked.titleWithId", { id: accountId }) : t("wx.linked.title")}
              </strong>
              <p>{linkedBody(t, saveState)}</p>
            </div>
          ) : qr && !failed ? (
            <div className="wa-pair__qr">
              <QRCodeSVG value={qr} size={260} marginSize={2} />
              <p className="muted">{t("wx.qr.hint")}</p>
            </div>
          ) : failed ? (
            <div className="wa-pair__empty">
              <RotateCw size={34} strokeWidth={1.5} />
              <strong>{t("wx.failed.title")}</strong>
              <p>{t("wx.failed.hint")}</p>
            </div>
          ) : (
            <div className="wa-pair__empty">
              <RotateCw size={34} strokeWidth={1.5} />
              <strong>{t("wx.waiting.title")}</strong>
              <p>{t("wx.waiting.hint")}</p>
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}

function linkedBody(t: ReturnType<typeof useT>, saveState: "idle" | "saving" | "saved" | "error"): string {
  if (saveState === "saving") return t("wx.linked.saving");
  if (saveState === "error") return t("wx.linked.error");
  // saved (and the momentary idle before the save kicks off)
  return t("wx.linked.saved");
}

function handleFrame(
  frame: string,
  t: ReturnType<typeof useT>,
  setQr: (qr: string) => void,
  setStatus: (status: string) => void,
  setStatusKind: (kind: string) => void,
  setFailed: (failed: boolean) => void,
  setLinked: (linked: boolean) => void,
  setAccountId: (id: string) => void,
) {
  const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
  if (!dataLine) return;
  let payload: PairPayload;
  try {
    payload = JSON.parse(dataLine.slice(5).trim()) as PairPayload;
  } catch {
    return;
  }
  if (payload.type === "qr" && payload.qr) {
    setQr(payload.qr);
    setStatusKind("qr");
    setStatus(
      payload.refresh
        ? t("wx.status.qrRefreshed", { n: String(payload.refresh) })
        : t("wx.status.scanQr"),
    );
    return;
  }
  if (payload.type === "status") {
    if (payload.status === "scanned") {
      setStatusKind("connecting");
      setStatus(t("wx.status.scanned"));
    }
    return;
  }
  if (payload.type === "done") {
    if (payload.ok) {
      setStatus(t("wx.status.linkedSaving"));
      setStatusKind("connected");
      setLinked(true);
      setAccountId(payload.account_id || "");
    } else {
      setStatus(payload.reason || t("csetup.pairingFailed"));
      setStatusKind("error");
      setFailed(true);
    }
  }
}
