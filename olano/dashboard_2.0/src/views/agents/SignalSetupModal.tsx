import { useCallback, useEffect, useRef, useState } from "react";
import { Sparkles } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useToast } from "../../ui/toast";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import { Modal, Field } from "./Modal";

// Guided Signal setup. Mirrors WhatsAppPairModal's structure but drives a
// step state-machine over signal-cli (install → register → captcha → verify →
// profile → save → restart). The captcha token is entered in a masked field
// and only ever posted to the dedicated /api/connectors/signal endpoints, never
// to /api/shell/stream (which logs commands).

type Step =
  | "detect"
  | "install"
  | "number"
  | "register"
  | "captcha"
  | "verify"
  | "profile"
  | "saved";

const STEP_TITLE_KEYS: Record<Step, string> = {
  detect: "sig.step.detect",
  install: "sig.step.install",
  number: "sig.step.number",
  register: "sig.step.register",
  captcha: "sig.step.captcha",
  verify: "sig.step.verify",
  profile: "sig.step.profile",
  saved: "sig.step.saved",
};

const CAPTCHA_URL = "https://signalcaptchas.org/registration/generate.html";

export function SignalSetupModal({
  agentName,
  initialAccount,
  httpUrl,
  onClose,
  onRestarted,
}: {
  agentName: string;
  initialAccount?: string;
  httpUrl?: string;
  onClose: () => void;
  onRestarted: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const abortRef = useRef<AbortController | null>(null);
  const [step, setStep] = useState<Step>("detect");
  const [account, setAccount] = useState(initialAccount || "");
  const [url, setUrl] = useState(httpUrl || "http://127.0.0.1:8080");
  const [captcha, setCaptcha] = useState("");
  const [code, setCode] = useState("");
  const [givenName, setGivenName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [logs, setLogs] = useState<string[]>([]);

  const appendLog = useCallback((line: string) => {
    setLogs((cur) => [...cur.slice(-160), line]);
  }, []);

  // ── step 1: detect signal-cli ──────────────────────────────────────────
  const runDetect = useCallback(async () => {
    setStep("detect");
    setError(null);
    setStatus(t("sig.status.checking"));
    try {
      const res = await api.post<{ installed: boolean; version: string | null }>(
        "/api/connectors/signal/detect",
        {},
      );
      if (res.installed) {
        appendLog(t("sig.log.found", { version: res.version || t("sig.unknownVersion") }));
        setStep("number");
      } else {
        appendLog(t("sig.log.notFound"));
        setStep("install");
      }
    } catch (e) {
      setError(errText(e, t));
      setStep("install");
    }
  }, [appendLog, t]);

  useEffect(() => {
    void runDetect();
    return () => abortRef.current?.abort();
  }, [runDetect]);

  // ── step 2: install via the allowlisted shell stream ───────────────────
  async function runInstall() {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setBusy(true);
    setError(null);
    setStatus(t("sig.status.installing"));
    try {
      const qs = new URLSearchParams({ command: "brew install signal-cli" });
      const resp = await fetch(`/api/shell/stream?${qs.toString()}`, {
        credentials: "include",
        headers: { Accept: "text/event-stream" },
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body)
        throw new Error(t("sig.err.installStreamFailed", { status: String(resp.status) }));
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let exit: number | null = null;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          const data = parseFrame(frame);
          if (!data) continue;
          if ((data.type === "stdout" || data.type === "stderr") && data.data) appendLog(data.data);
          else if (data.type === "error" && data.message) appendLog(t("csetup.errorLog", { message: data.message }));
          else if (data.type === "done") exit = typeof data.exit_code === "number" ? data.exit_code : null;
        }
      }
      if (exit === 0) {
        appendLog(t("sig.log.installFinished"));
        await runDetect();
      } else {
        setError(t("sig.err.installIncomplete"));
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(errText(e, t));
    } finally {
      setBusy(false);
      if (abortRef.current === ctrl) abortRef.current = null;
    }
  }

  // ── step 4: register (+ captcha) ───────────────────────────────────────
  async function doRegister(withCaptcha: boolean) {
    if (!isE164(account)) {
      setError(t("sig.err.badNumber"));
      return;
    }
    setBusy(true);
    setError(null);
    setStatus(t("sig.status.registering"));
    try {
      const body: { account: string; captcha?: string } = { account };
      if (withCaptcha) body.captcha = captcha;
      const res = await api.post<{ ok: boolean; captcha_required: boolean; already_registered?: boolean; message: string }>(
        "/api/connectors/signal/register",
        body,
      );
      if (res.ok) {
        appendLog(t("sig.log.registered"));
        setStep("verify");
      } else if (res.already_registered) {
        // Not an error — the number is usable. Skip verification entirely.
        appendLog(t("sig.log.alreadyRegistered"));
        setStep("profile");
      } else if (res.captcha_required) {
        appendLog(t("sig.log.captchaRequired"));
        setStep("captcha");
        if (withCaptcha) setError(t("sig.err.captchaRejected"));
      } else {
        setError(res.message || t("sig.err.registerFailed"));
      }
    } catch (e) {
      setError(errText(e, t));
    } finally {
      setBusy(false);
    }
  }

  // ── step 5: verify SMS code ────────────────────────────────────────────
  async function doVerify() {
    if (!code.trim()) {
      setError(t("sig.err.codeRequired"));
      return;
    }
    setBusy(true);
    setError(null);
    setStatus(t("sig.status.verifying"));
    try {
      const res = await api.post<{ ok: boolean; message: string }>(
        "/api/connectors/signal/verify",
        { account, code: code.trim() },
      );
      if (res.ok) {
        appendLog(t("sig.log.verified"));
        setStep("profile");
      } else {
        setError(res.message || t("sig.err.verifyFailed"));
      }
    } catch (e) {
      setError(errText(e, t));
    } finally {
      setBusy(false);
    }
  }

  // ── step 6: profile name (optional) ────────────────────────────────────
  async function doProfile(skip: boolean) {
    setBusy(true);
    setError(null);
    try {
      if (!skip && givenName.trim()) {
        setStatus(t("sig.status.settingProfile"));
        const res = await api.post<{ ok: boolean }>("/api/connectors/signal/profile", {
          account,
          given_name: givenName.trim(),
          agent: agentName,
        });
        appendLog(
          res.ok
            ? t("sig.log.profileSet", { name: givenName.trim() })
            : t("sig.log.profileFailed"),
        );
      }
      await doSave();
    } catch (e) {
      setError(errText(e, t));
      setBusy(false);
    }
  }

  // ── step 7: save the channel with the managed daemon enabled ───────────
  async function doSave() {
    setStatus(t("sig.status.saving"));
    try {
      await api.post(`/api/agents/${encodeURIComponent(agentName)}/channels`, {
        type: "platform:signal",
        config: {
          extra: {
            http_url: url || "http://127.0.0.1:8080",
            account,
            ignore_stories: true,
            manage_daemon: true,
          },
        },
      });
      appendLog(t("sig.log.saved"));
      setStep("saved");
    } catch (e) {
      setError(errText(e, t));
    } finally {
      setBusy(false);
    }
  }

  async function restartAgent() {
    setBusy(true);
    try {
      await api.post(`/api/agents/${encodeURIComponent(agentName)}/restart`);
      toast.push(t("sig.toast.restarted", { name: agentName }));
      onRestarted();
      onClose();
    } catch (e) {
      toast.push(errText(e, t), "error");
      setBusy(false);
    }
  }

  const stepIndex = ["detect", "install", "number", "register", "captcha", "verify", "profile", "saved"].indexOf(step) + 1;

  return (
    <Modal
      title={t("sig.title")}
      subtitle={t("sig.subtitle", { title: t(STEP_TITLE_KEYS[step]), n: String(stepIndex), agent: agentName })}
      onClose={onClose}
      width={680}
      footer={
        <button className="btn" onClick={onClose} disabled={busy}>
          {t("card.close")}
        </button>
      }
    >
      <div className="connector-main" style={{ gap: 14 }}>
        {error ? <div className="connector-error">{error}</div> : null}
        {status && !error ? <p className="muted">{status}</p> : null}

        {step === "detect" ? <p className="muted">{t("sig.detect.checking")}</p> : null}

        {step === "install" ? (
          <div className="connector-fieldset">
            <h3>{t("sig.install.title")}</h3>
            <p className="muted">{t("sig.install.body")}</p>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn primary row" onClick={() => void runInstall()} disabled={busy}>
                <Sparkles size={14} strokeWidth={1.75} /> {busy ? t("sig.install.installing") : t("sig.install.installBtn")}
              </button>
              <button className="btn" onClick={() => void runDetect()} disabled={busy}>
                {t("sig.install.recheck")}
              </button>
            </div>
          </div>
        ) : null}

        {step === "number" ? (
          <div className="connector-fieldset">
            <h3>{t("sig.number.title")}</h3>
            <Field label={t("sig.number.phoneLabel")} hint={t("sig.number.phoneHint")}>
              <input
                className="input"
                value={account}
                onChange={(e) => setAccount(e.target.value)}
                placeholder={t("sig.number.phonePlaceholder")}
              />
            </Field>
            <Field label={t("sig.number.urlLabel")} hint={t("sig.number.urlHint")}>
              <input
                className="input"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder={t("sig.number.urlPlaceholder")}
              />
            </Field>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn primary" onClick={() => void doRegister(false)} disabled={busy}>
                {busy ? t("sig.number.registering") : t("sig.number.registerBtn")}
              </button>
              <button
                className="btn"
                onClick={() => {
                  if (!isE164(account)) {
                    setError(t("sig.err.badNumber"));
                    return;
                  }
                  setError(null);
                  appendLog(t("sig.number.skipLog"));
                  setStep("profile");
                }}
                disabled={busy}
              >
                {t("sig.number.alreadyRegistered")}
              </button>
            </div>
          </div>
        ) : null}

        {step === "captcha" ? (
          <div className="connector-fieldset">
            <h3>{t("sig.captcha.title")}</h3>
            <ol className="muted" style={{ margin: "0 0 8px 18px", lineHeight: 1.5 }}>
              <li>
                {t("sig.captcha.openPre")}{" "}
                <a href={CAPTCHA_URL} target="_blank" rel="noreferrer">
                  signalcaptchas.org
                </a>{" "}
                {t("sig.captcha.openPost")}
              </li>
              <li>{t("sig.captcha.copyStep")}</li>
            </ol>
            <Field label={t("sig.captcha.tokenLabel")} hint={t("sig.captcha.tokenHint")}>
              <input
                className="input"
                type="password"
                value={captcha}
                onChange={(e) => setCaptcha(e.target.value)}
                placeholder={t("sig.captcha.tokenPlaceholder")}
                autoComplete="off"
                style={{ fontFamily: "var(--font-mono)" }}
              />
            </Field>
            <button className="btn primary" onClick={() => void doRegister(true)} disabled={busy || !captcha.trim()}>
              {busy ? t("sig.captcha.submitting") : t("sig.captcha.submitBtn")}
            </button>
          </div>
        ) : null}

        {step === "verify" ? (
          <div className="connector-fieldset">
            <h3>{t("sig.verify.title")}</h3>
            <Field label={t("sig.verify.codeLabel")} hint={t("sig.verify.codeHint")}>
              <input
                className="input"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder={t("sig.verify.codePlaceholder")}
              />
            </Field>
            <button className="btn primary" onClick={() => void doVerify()} disabled={busy}>
              {busy ? t("sig.verify.verifying") : t("sig.verify.verifyBtn")}
            </button>
          </div>
        ) : null}

        {step === "profile" ? (
          <div className="connector-fieldset">
            <h3>{t("sig.profile.title")}</h3>
            <p className="muted">{t("sig.profile.body")}</p>
            <Field label={t("sig.profile.nameLabel")} hint={t("sig.profile.nameHint")}>
              <input
                className="input"
                value={givenName}
                onChange={(e) => setGivenName(e.target.value)}
                placeholder={t("sig.profile.namePlaceholder")}
              />
            </Field>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn primary" onClick={() => void doProfile(false)} disabled={busy || !givenName.trim()}>
                {busy ? t("sig.profile.saving") : t("sig.profile.saveBtn")}
              </button>
              <button className="btn" onClick={() => void doProfile(true)} disabled={busy}>
                {t("sig.profile.skip")}
              </button>
            </div>
          </div>
        ) : null}

        {step === "saved" ? (
          <div className="connector-fieldset">
            <h3>{t("sig.saved.title")}</h3>
            <p className="muted">{t("sig.saved.body")}</p>
            <button className="btn primary" onClick={() => void restartAgent()} disabled={busy}>
              {busy ? t("sig.saved.restarting") : t("csetup.restartAgent")}
            </button>
          </div>
        ) : null}

        <details className="wa-pair__log-wrap">
          <summary>{t("csetup.technicalLog")}</summary>
          <pre className="wa-pair__log">{logs.join("\n") || t("csetup.noLogLines")}</pre>
        </details>
      </div>
    </Modal>
  );
}

interface ShellFrame {
  type?: "started" | "stdout" | "stderr" | "done" | "error";
  data?: string;
  message?: string;
  exit_code?: number;
}

function parseFrame(frame: string): ShellFrame | null {
  const line = frame.split("\n").find((l) => l.startsWith("data:"));
  if (!line) return null;
  try {
    return JSON.parse(line.slice(5).trim()) as ShellFrame;
  } catch {
    return null;
  }
}

function isE164(value: string): boolean {
  return /^\+[1-9]\d{6,14}$/.test(value.trim());
}

function errText(e: unknown, t: ReturnType<typeof useT>): string {
  if (e instanceof ApiError) return e.detail;
  if (e instanceof Error) return e.message;
  return t("sig.err.generic");
}
