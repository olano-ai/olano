import { useCallback, useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { settingAnchor } from "../../core/settingAnchor";
import { useToast } from "../../ui/toast";
import { Section } from "../config/Section";
import { STR } from "./strings";

// config.yaml → observability.langfuse. The six switches ride ConfigView's
// form and its single Save, like every other block. The keys, the connection
// test and the status act at once through /api/observability/langfuse/*
// (admin-only): the keys are reserved vault rows, never part of config.yaml.

export interface LangfuseCfg {
  enabled: boolean;
  host: string;
  capture: string;
  sample_rate: number;
  environment: string;
  trace_background: boolean;
  project_id?: string;
}

export interface ObservabilityCfg {
  langfuse?: LangfuseCfg;
}

export interface ObservabilityForm {
  ob_enabled: boolean;
  ob_host: string;
  ob_capture: string;
  ob_sample: string;
  ob_env: string;
  ob_background: boolean;
}

interface ClientStats {
  host: string;
  exported_spans: number;
  failed_batches: number;
  masking_failures: number;
  last_error: string;
  last_error_at: number | null;
}

interface AgentRow {
  name: string;
  traced: boolean;
  capture: string | null;
  override: { enabled: boolean | null; capture: string | null; sample_rate: number | null };
}

interface LangfuseStatus {
  settings: LangfuseCfg;
  effective_host: string;
  credential_source: "vault" | "env" | "none" | "agent vault";
  public_key: string;
  project_url: string;
  agents: AgentRow[];
  clients: ClientStats[];
}

interface TestResult {
  ok: boolean;
  message?: string;
  project_name?: string;
  host?: string;
}

export function observabilityFormFrom(cfg: ObservabilityCfg | undefined): ObservabilityForm {
  const lf = cfg?.langfuse;
  return {
    ob_enabled: lf?.enabled ?? false,
    ob_host: lf?.host ?? "",
    ob_capture: lf?.capture === "full" ? "full" : "metadata",
    ob_sample: String(lf?.sample_rate ?? 1),
    ob_env: lf?.environment ?? "",
    ob_background: lf?.trace_background ?? true,
  };
}

// Mirrors LangfuseSettings' rules so Save disables instead of the server
// silently storing an empty host or a clamped rate.
export const hostOk = (raw: string): boolean => {
  const text = raw.trim();
  if (!text) return true;
  try {
    const url = new URL(text);
    if (url.username || url.password) return false;
    if (url.protocol === "https:") return true;
    return url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]", "host.docker.internal"].includes(url.hostname);
  } catch {
    return false;
  }
};

const sampleOk = (raw: string): boolean => {
  if (!/^\s*\d*\.?\d+\s*$/.test(raw)) return false;
  const n = Number(raw);
  return n >= 0 && n <= 1;
};

const envOk = (raw: string): boolean => {
  const text = raw.trim();
  return !text || /^(?!langfuse)[a-z0-9_-]{1,40}$/.test(text);
};

export function observabilityFormValid(f: ObservabilityForm): boolean {
  return hostOk(f.ob_host) && sampleOk(f.ob_sample) && envOk(f.ob_env);
}

export function observabilityPayload(f: ObservabilityForm): ObservabilityCfg {
  return {
    langfuse: {
      enabled: f.ob_enabled,
      host: f.ob_host.trim(),
      capture: f.ob_capture,
      sample_rate: Number(f.ob_sample),
      environment: f.ob_env.trim(),
      trace_background: f.ob_background,
    },
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

const bad = (ok: boolean): CSSProperties | undefined => (ok ? undefined : { borderColor: "var(--danger)" });

export function ObservabilityConfigSection({
  form,
  set,
}: {
  form: ObservabilityForm;
  // Not generic over the key like the page's own setter: this form mixes
  // boolean and string fields, which a generic signature cannot unify with
  // the page's wider Form. ConfigView adapts its setter at the call site.
  set: (key: keyof ObservabilityForm, value: string | boolean) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [status, setStatus] = useState<LangfuseStatus | null>(null);
  const [pk, setPk] = useState("");
  const [sk, setSk] = useState("");
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<TestResult | null>(null);

  const load = useCallback(() => {
    api
      .get<LangfuseStatus>("/api/observability/langfuse/status")
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);
  useEffect(load, [load]);

  const errText = (err: unknown) => (err instanceof ApiError ? err.detail : String(err));

  async function saveKeys() {
    setBusy(true);
    try {
      await api.put("/api/observability/langfuse/credentials", {
        public_key: pk.trim(),
        secret_key: sk.trim(),
        host: form.ob_host.trim(),
      });
      setPk("");
      setSk("");
      toast.push(t("ob.keysSaved"), "success");
      load();
    } catch (err) {
      toast.push(errText(err), "error");
    } finally {
      setBusy(false);
    }
  }

  async function clearKeys() {
    setBusy(true);
    try {
      await api.del("/api/observability/langfuse/credentials");
      toast.push(t("ob.keysCleared"), "success");
      load();
    } catch (err) {
      toast.push(errText(err), "error");
    } finally {
      setBusy(false);
    }
  }

  async function runTest() {
    setBusy(true);
    setTest(null);
    try {
      const both = pk.trim() && sk.trim();
      const res = await api.post<TestResult>("/api/observability/langfuse/test", {
        public_key: both ? pk.trim() : "",
        secret_key: both ? sk.trim() : "",
        host: form.ob_host.trim(),
      });
      setTest(res);
      load();
    } catch (err) {
      setTest({ ok: false, message: errText(err) });
    } finally {
      setBusy(false);
    }
  }

  const source = status?.credential_source ?? "none";
  const sourceLine =
    source === "vault"
      ? t("ob.keysSource.vault", { key: status?.public_key || "" })
      : source === "env"
        ? t("ob.keysSource.env", { key: status?.public_key || "" })
        : t("ob.keysSource.none");
  const client = status?.clients?.[0];
  const statusLine = !client
    ? t("ob.status.none")
    : client.last_error
      ? t("ob.status.error", {
          when: client.last_error_at ? new Date(client.last_error_at * 1000).toLocaleString() : "",
          error: client.last_error,
        })
      : t("ob.status.ok", { spans: String(client.exported_spans) });
  const overrides = (status?.agents ?? []).filter(
    (a) => a.override.enabled !== null || a.override.capture !== null || a.override.sample_rate !== null,
  );
  const dim = { opacity: form.ob_enabled ? 1 : 0.55 };

  return (
    <Section settingId="config.observability" title={t("ob.section")} hint={t("ob.section.hint")}>
      <div className="admin-form-row" style={{ gap: 16 }}>
        <Field
          settingId="config.observability.enabled"
          label={t("ob.enabled")}
          hint={t("ob.enabled.hint")}
          style={{ flex: "1 1 260px" }}
        >
          <label className="row" style={{ gap: 8, alignItems: "center", fontSize: 13 }}>
            <input
              type="checkbox"
              data-testid="observability-enabled"
              checked={form.ob_enabled}
              onChange={(e) => set("ob_enabled", e.target.checked)}
            />
            {t("ob.enabled")}
          </label>
        </Field>
        <Field
          settingId="config.observability.host"
          label={t("ob.host")}
          hint={t("ob.host.hint")}
          style={{ flex: "1 1 260px" }}
        >
          <input
            className="input"
            data-testid="observability-host"
            placeholder="https://cloud.langfuse.com"
            value={form.ob_host}
            onChange={(e) => set("ob_host", e.target.value)}
            style={bad(hostOk(form.ob_host))}
          />
        </Field>
      </div>

      <div className="admin-form-row" style={{ gap: 16, marginTop: 12, ...dim }}>
        <Field
          settingId="config.observability.capture"
          label={t("ob.capture")}
          hint={t("ob.capture.hint")}
          style={{ flex: "1 1 260px" }}
        >
          <select
            className="input"
            data-testid="observability-capture"
            value={form.ob_capture}
            onChange={(e) => set("ob_capture", e.target.value)}
          >
            <option value="metadata">{t("ob.capture.metadata")}</option>
            <option value="full">{t("ob.capture.full")}</option>
          </select>
        </Field>
        <Field
          settingId="config.observability.sampleRate"
          label={t("ob.sample")}
          hint={t("ob.sample.hint")}
          style={{ flex: "1 1 220px" }}
        >
          <input
            className="input"
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={form.ob_sample}
            onChange={(e) => set("ob_sample", e.target.value)}
            style={bad(sampleOk(form.ob_sample))}
          />
        </Field>
      </div>

      {form.ob_capture === "full" && (
        <div
          role="alert"
          data-testid="observability-full-warning"
          className="card"
          style={{ marginTop: 12, padding: 10, fontSize: 12.5, lineHeight: 1.5, borderColor: "var(--warn, #c98a00)" }}
        >
          {t("ob.fullWarning")}
        </div>
      )}

      <div className="admin-form-row" style={{ gap: 16, marginTop: 12, ...dim }}>
        <Field
          settingId="config.observability.environment"
          label={t("ob.environment")}
          hint={t("ob.environment.hint")}
          style={{ flex: "1 1 220px" }}
        >
          <input
            className="input"
            placeholder="production"
            value={form.ob_env}
            onChange={(e) => set("ob_env", e.target.value)}
            style={bad(envOk(form.ob_env))}
          />
        </Field>
        <Field
          settingId="config.observability.background"
          label={t("ob.background")}
          hint={t("ob.background.hint")}
          style={{ flex: "1 1 260px" }}
        >
          <label className="row" style={{ gap: 8, alignItems: "center", fontSize: 13 }}>
            <input
              type="checkbox"
              checked={form.ob_background}
              onChange={(e) => set("ob_background", e.target.checked)}
            />
            {t("ob.background")}
          </label>
        </Field>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "10px 0 0" }}>
        {t("ob.unsavedNote")}
      </p>

      <div style={{ marginTop: 16, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
        <Field settingId="config.observability.keys" label={t("ob.keys")} hint={t("ob.keys.hint")}>
          <div className="muted" data-testid="observability-key-source" style={{ fontSize: 12.5, marginBottom: 8 }}>
            {sourceLine}
          </div>
          <div className="admin-form-row" style={{ gap: 12 }}>
            <input
              className="input"
              aria-label={t("ob.publicKey")}
              placeholder={`${t("ob.publicKey")} (pk-lf-…)`}
              autoComplete="off"
              value={pk}
              onChange={(e) => setPk(e.target.value)}
              style={{ flex: "1 1 220px" }}
            />
            <input
              className="input"
              type="password"
              aria-label={t("ob.secretKey")}
              placeholder={`${t("ob.secretKey")} (sk-lf-…)`}
              autoComplete="new-password"
              value={sk}
              onChange={(e) => setSk(e.target.value)}
              style={{ flex: "1 1 220px" }}
            />
          </div>
          <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            <button
              className="btn primary"
              type="button"
              disabled={busy || !pk.trim() || !sk.trim()}
              onClick={() => void saveKeys()}
            >
              {t("ob.saveKeys")}
            </button>
            <button
              className="btn"
              type="button"
              data-testid="observability-test"
              disabled={busy}
              title={t("ob.test.hint")}
              onClick={() => void runTest()}
            >
              {busy ? t("ob.testing") : t("ob.test")}
            </button>
            {source === "vault" && (
              <button className="btn ghost" type="button" disabled={busy} onClick={() => void clearKeys()}>
                {t("ob.clearKeys")}
              </button>
            )}
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            {t("ob.test.hint")}
          </div>
          {test && (
            <div
              data-testid="observability-test-result"
              role="status"
              style={{ marginTop: 8, fontSize: 12.5, color: test.ok ? "var(--success)" : "var(--danger)" }}
            >
              {test.ok
                ? t("ob.test.ok", { project: test.project_name || "" })
                : t("ob.test.fail", { message: test.message || "" })}
            </div>
          )}
        </Field>
      </div>

      <div style={{ marginTop: 14, fontSize: 12.5, lineHeight: 1.6 }}>
        <div>
          <strong>{t("ob.status")}: </strong>
          <span className="muted">{statusLine}</span>
          {status?.project_url && (
            <>
              {" · "}
              <a href={status.project_url} target="_blank" rel="noopener noreferrer">
                {t("ob.openProject")}
              </a>
            </>
          )}
        </div>
        <div>
          <strong>{t("ob.overrides")}: </strong>
          <span className="muted">
            {overrides.length === 0
              ? t("ob.overrides.none")
              : overrides
                  .map((a) => `${a.name} (${a.traced ? `${t("ob.override.on")}, ${a.capture}` : t("ob.override.off")})`)
                  .join(", ")}
          </span>
        </div>
      </div>
    </Section>
  );
}
