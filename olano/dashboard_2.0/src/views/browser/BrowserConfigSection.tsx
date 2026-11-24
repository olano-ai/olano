import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { api, ApiError } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { useT } from "../../core/i18n";
import { settingAnchor } from "../../core/settingAnchor";
import { useToast } from "../../ui/toast";
import { Section } from "../config/Section";
import { STR } from "./strings";

// config.yaml → browser. The seven editable fields ride ConfigView's form and
// its single Save; this component renders them plus the read-only status the
// GET carries next to the block (what is in effect after the plan, hardware and
// Docker gates, and the live image's download state).

export interface BrowserCfg {
  mode: string;
  max_sessions: number;
  idle_timeout: number;
  provider: string;
  timezone: string;
  human_check?: string;
  human_check_wait?: number;
}

export interface BrowserImageStatus {
  image: string;
  source: string;
  present: boolean;
  docker: boolean;
  pull?: { status?: string; error?: string; via?: string };
}

export interface BrowserStatusCfg {
  availability: {
    requested: string;
    mode: string;
    provider: string;
    code: string;
    reason: string;
    enabled: boolean;
  };
  image: BrowserImageStatus;
  live_entitled: boolean;
}

export interface BrowserForm {
  br_mode: string;
  br_provider: string;
  br_max_sessions: string;
  br_idle_timeout: string;
  br_timezone: string;
  br_human_check: string;
  br_human_check_wait: string;
}

export function browserFormFrom(cfg: BrowserCfg | undefined): BrowserForm {
  return {
    br_mode: cfg?.mode ?? "off",
    br_provider: cfg?.provider ?? "local",
    br_max_sessions: String(cfg?.max_sessions ?? 1),
    br_idle_timeout: String(cfg?.idle_timeout ?? 10),
    br_timezone: cfg?.timezone ?? "",
    br_human_check: cfg?.human_check ?? "wait",
    br_human_check_wait: String(cfg?.human_check_wait ?? 5),
  };
}

const intIn = (raw: string, lo: number, hi: number): boolean => {
  const n = Number(raw);
  return /^\d+$/.test(raw.trim()) && n >= lo && n <= hi;
};

// A time zone the browser running the dashboard knows; the server checks
// against its own list again on save. Empty means "this machine's clock".
export const timezoneOk = (raw: string): boolean => {
  const name = raw.trim();
  if (!name) return true;
  try {
    new Intl.DateTimeFormat("en", { timeZone: name });
    return true;
  } catch {
    return false;
  }
};

// Mirrors BrowserSettings' clamps so Save disables instead of the server
// silently persisting a coerced value.
export function browserFormValid(f: BrowserForm): boolean {
  return (
    intIn(f.br_max_sessions, 1, 8) &&
    intIn(f.br_idle_timeout, 1, 240) &&
    timezoneOk(f.br_timezone) &&
    intIn(f.br_human_check_wait, 1, 120)
  );
}

export function browserPayload(f: BrowserForm): BrowserCfg {
  return {
    mode: f.br_mode,
    provider: f.br_provider,
    max_sessions: parseInt(f.br_max_sessions, 10),
    idle_timeout: parseInt(f.br_idle_timeout, 10),
    timezone: f.br_timezone.trim(),
    human_check: f.br_human_check,
    human_check_wait: parseInt(f.br_human_check_wait, 10),
  };
}

function Field({
  label,
  hint,
  children,
  style,
  settingId,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  style?: CSSProperties;
  settingId?: string;
}) {
  return (
    <div className="admin-field" style={style} {...settingAnchor(settingId)}>
      <label className="admin-field-label">
        {label}
        {hint && <span className="admin-field-hint">{hint}</span>}
      </label>
      {children}
    </div>
  );
}

export function BrowserConfigSection({
  form,
  set,
  status,
  isAdmin,
}: {
  form: BrowserForm;
  set: <K extends keyof BrowserForm>(key: K, value: BrowserForm[K]) => void;
  status: BrowserStatusCfg | undefined;
  isAdmin: boolean;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [image, setImage] = useState<BrowserImageStatus | undefined>(status?.image);

  useEffect(() => setImage(status?.image), [status?.image]);

  // The download runs server-side; its progress arrives as browser_image frames.
  useEffect(
    () =>
      subscribeStream((frame) => {
        if (frame.type === "browser_image") setImage(frame.data as BrowserImageStatus);
      }),
    [],
  );

  const off = form.br_mode === "off";
  const avail = status?.availability;
  const effective = avail
    ? t(`br.effective.${avail.mode}`) + (avail.enabled && avail.provider === "remote" ? t("br.effective.remote") : "")
    : "";
  const pullState = image?.pull?.status;
  // "building": the download was refused or failed, so the image is being
  // built on this machine from the build files the runtime ships.
  const fetching = pullState === "pulling" || pullState === "building";
  const imageText = !image
    ? ""
    : pullState === "pulling"
      ? t("br.image.pulling")
      : pullState === "building"
        ? t("br.image.building")
        : image.present
          ? t(image.pull?.via === "build" ? "br.image.built" : "br.image.present")
          : pullState === "failed"
            ? t("br.image.failed", { error: image.pull?.error || "" })
            : t("br.image.missing");

  async function pull() {
    try {
      setImage(await api.post<BrowserImageStatus>("/api/browser/image/pull"));
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    }
  }

  return (
    <Section settingId="config.browser" title={t("br.section")} hint={t("br.section.hint")}>
      <div className="admin-form-row" style={{ gap: 16 }}>
        <Field
          settingId="config.browser.mode"
          label={t("br.mode")}
          hint={t("br.mode.hint")}
          style={{ flex: "1 1 260px" }}
        >
          <select
            className="input"
            data-testid="browser-mode"
            value={form.br_mode}
            onChange={(e) => set("br_mode", e.target.value)}
          >
            <option value="off">{t("br.mode.off")}</option>
            <option value="headless">{t("br.mode.headless")}</option>
            <option value="live" disabled={status ? !status.live_entitled : false}>
              {t("br.mode.live")}
            </option>
          </select>
        </Field>
        <Field
          settingId="config.browser.provider"
          label={t("br.provider")}
          hint={t("br.provider.hint")}
          style={{ flex: "1 1 260px", opacity: off ? 0.55 : 1 }}
        >
          <select
            className="input"
            value={form.br_provider}
            disabled={off}
            onChange={(e) => set("br_provider", e.target.value)}
          >
            <option value="local">{t("br.provider.local")}</option>
            <option value="remote">{t("br.provider.remote")}</option>
          </select>
        </Field>
      </div>
      <div className="admin-form-row" style={{ gap: 16, marginTop: 12, opacity: off ? 0.55 : 1 }}>
        <Field
          settingId="config.browser.maxSessions"
          label={t("br.maxSessions")}
          hint={t("br.maxSessions.hint")}
          style={{ flex: "1 1 220px" }}
        >
          <input
            className="input"
            type="number"
            min={1}
            max={8}
            disabled={off}
            value={form.br_max_sessions}
            onChange={(e) => set("br_max_sessions", e.target.value)}
            style={!intIn(form.br_max_sessions, 1, 8) ? { borderColor: "var(--danger)" } : undefined}
          />
        </Field>
        <Field
          settingId="config.browser.idleTimeout"
          label={t("br.idle")}
          hint={t("br.idle.hint")}
          style={{ flex: "1 1 220px" }}
        >
          <input
            className="input"
            type="number"
            min={1}
            max={240}
            disabled={off}
            value={form.br_idle_timeout}
            onChange={(e) => set("br_idle_timeout", e.target.value)}
            style={!intIn(form.br_idle_timeout, 1, 240) ? { borderColor: "var(--danger)" } : undefined}
          />
        </Field>
      </div>
      <div className="admin-form-row" style={{ gap: 16, marginTop: 12, opacity: off ? 0.55 : 1 }}>
        <Field
          settingId="config.browser.timezone"
          label={t("br.timezone")}
          hint={t("br.timezone.hint")}
          style={{ flex: "1 1 260px" }}
        >
          <input
            className="input"
            type="text"
            data-testid="browser-timezone"
            disabled={off}
            spellCheck={false}
            autoComplete="off"
            placeholder={t("br.timezone.placeholder")}
            value={form.br_timezone}
            onChange={(e) => set("br_timezone", e.target.value)}
            style={!timezoneOk(form.br_timezone) ? { borderColor: "var(--danger)" } : undefined}
          />
          {!timezoneOk(form.br_timezone) && (
            <span className="admin-field-hint" style={{ color: "var(--danger)" }}>
              {t("br.timezone.invalid")}
            </span>
          )}
        </Field>
      </div>
      <div className="admin-form-row" style={{ gap: 16, marginTop: 12, opacity: off ? 0.55 : 1 }}>
        <Field
          settingId="config.browser.humanCheck"
          label={t("br.humanCheck")}
          hint={t("br.humanCheck.hint")}
          style={{ flex: "1 1 260px" }}
        >
          <select
            className="input"
            data-testid="browser-human-check"
            disabled={off}
            value={form.br_human_check}
            onChange={(e) => set("br_human_check", e.target.value)}
          >
            <option value="wait">{t("br.humanCheck.wait")}</option>
            <option value="hold">{t("br.humanCheck.hold")}</option>
            <option value="skip">{t("br.humanCheck.skip")}</option>
          </select>
        </Field>
        <Field
          settingId="config.browser.humanCheckWait"
          label={t("br.humanCheckWait")}
          hint={t("br.humanCheckWait.hint")}
          style={{ flex: "1 1 220px" }}
        >
          <input
            className="input"
            type="number"
            min={1}
            max={120}
            data-testid="browser-human-check-wait"
            disabled={off || form.br_human_check !== "wait"}
            value={form.br_human_check_wait}
            onChange={(e) => set("br_human_check_wait", e.target.value)}
            style={!intIn(form.br_human_check_wait, 1, 120) ? { borderColor: "var(--danger)" } : undefined}
          />
        </Field>
      </div>
      {avail && (
        <p className="muted" style={{ fontSize: 12.5, margin: "12px 0 0" }} data-testid="browser-effective">
          <strong>{t("br.effective")}:</strong> {effective}
          {avail.reason ? ` · ${avail.reason}` : ""}
          {avail.requested !== form.br_mode ? ` · ${t("br.effective.unsaved")}` : ""}
        </p>
      )}
      {image && (form.br_mode === "live" || image.present || fetching) && (
        <div className="row" style={{ gap: 10, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
          <span className="muted" style={{ fontSize: 12.5 }}>
            <strong>{t("br.image")}:</strong> {imageText}
          </span>
          {isAdmin && image.docker && !image.present && !fetching && (
            <button className="btn ghost sm" type="button" onClick={pull}>
              {t("br.image.pull")}
            </button>
          )}
        </div>
      )}
      {form.br_mode === "live" && (
        <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
          {t("br.image.hint")}
        </p>
      )}
    </Section>
  );
}
