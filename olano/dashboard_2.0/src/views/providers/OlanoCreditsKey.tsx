import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { PasswordField } from "../vault/components";
import { STR } from "./strings";

// The Olano account API key on a self-hosted box, in one component for the two
// places it is pasted: the Providers card and the onboarding wizard. A person
// mints the key on cloud.olano.ai (API keys), pastes it here, and the box runs
// the olano:* tiers, image/vision/speech and web search on that account's
// credits. The backend verifies the key against the cloud BEFORE storing it, so
// a typo or a revoked key never lands in the vault; every refusal arrives with a
// code this component turns into a sentence.
//
// Shown only when GET /api/olano/credits-key says `manageable` (open posture):
// a managed deployment bills its own wallet and the route answers 409 there.

export interface OlanoKeyStatus {
  manageable: boolean;
  posture: string;
  source: "env" | "deployment" | "account" | null;
  connected: boolean;
  /** The gateway serves THIS key (false while an env or enrolled key outranks it). */
  active?: boolean;
  /** What outranks the stored key, when something does. */
  shadowed_by?: "env" | "deployment" | null;
  /** Set as OLANO_CREDITS_KEY in the environment: not replaceable or removable here. */
  from_env?: boolean;
  key_prefix: string | null;
  rejected: boolean;
  cloud_url: string;
  key_url: string;
  billing_url: string;
}

export interface OlanoKeyVerified {
  org_name: string | null;
  balance: number | null;
  lifetime_used: number | null;
  credits_per_usd: number | null;
  services: string[];
  key: { id?: string; name?: string; prefix?: string };
}

export type OlanoKeyConnectResult = OlanoKeyStatus & {
  verified: OlanoKeyVerified;
  live_models_updated: number;
};

export const OLANO_KEY_PREFIX = "sk-olano-";
export const OLANO_KEYS_URL = "https://cloud.olano.ai/app/api-keys";

/** GET the key status, or null when the route is unreachable / forbidden. */
export async function fetchOlanoKeyStatus(): Promise<OlanoKeyStatus | null> {
  try {
    return await api.get<OlanoKeyStatus>("/api/olano/credits-key");
  } catch {
    return null;
  }
}

export function OlanoCreditsKey({
  status,
  onChanged,
  onConnected,
  compact = false,
  autoFocus = false,
}: {
  // The status the parent already fetched (it decides whether to render this
  // at all); the component refetches after every change it makes.
  status: OlanoKeyStatus;
  // Fired after a successful connect or remove, with the fresh status.
  onChanged?: (status: OlanoKeyStatus) => void;
  // Fired after a successful connect with the cloud's verify payload (account
  // name, balance, services), so a parent can show what the key bought.
  onConnected?: (result: OlanoKeyConnectResult) => void;
  // Tighter spacing, for the wizard card that carries its own heading.
  compact?: boolean;
  autoFocus?: boolean;
}) {
  const t = useT(STR);
  const [current, setCurrent] = useState<OlanoKeyStatus>(status);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [replacing, setReplacing] = useState(false);

  useEffect(() => setCurrent(status), [status]);

  const refresh = useCallback(async () => {
    const next = await fetchOlanoKeyStatus();
    if (next) {
      setCurrent(next);
      onChanged?.(next);
    }
  }, [onChanged]);

  function describe(e: unknown): string {
    if (e instanceof ApiError) {
      switch (e.code) {
        case "invalid_key":
          return t("olano.err.invalid", { prefix: OLANO_KEY_PREFIX });
        case "key_rejected":
          return t("olano.err.rejected");
        case "account_credits_unavailable":
          return t("olano.err.unavailable");
        case "cloud_busy":
          return t("olano.err.busy");
        case "cloud_unreachable":
        case "cloud_error":
          return t("olano.err.cloud");
        case "vault_locked":
          return t("olano.err.vaultLocked");
        case "managed_box":
          return t("olano.err.managed");
        default:
          return e.detail || t("olano.err.generic");
      }
    }
    return t("olano.err.generic");
  }

  async function connect() {
    const value = key.trim();
    if (!value) return;
    setBusy(true);
    setErr("");
    try {
      const result = await api.put<OlanoKeyConnectResult>("/api/olano/credits-key", { key: value });
      setKey("");
      setReplacing(false);
      setCurrent(result);
      onChanged?.(result);
      onConnected?.(result);
    } catch (e) {
      setErr(describe(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setErr("");
    try {
      await api.del("/api/olano/credits-key");
      setConfirmRemove(false);
      await refresh();
    } catch (e) {
      setErr(describe(e));
    } finally {
      setBusy(false);
    }
  }

  const gap = compact ? 8 : 10;
  // An operator's own OLANO_GATEWAY_KEY in the environment outranks a pasted
  // key, so the form says so instead of offering a paste that would be ignored.
  const envWins = current.source === "env";

  if (current.connected && !replacing) {
    return (
      <div className="olano-key" data-testid="olano-credits-key" style={{ display: "grid", gap }}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <span
            className="badge"
            style={{
              background: current.rejected ? "var(--warn-soft)" : "var(--success-soft)",
              color: current.rejected ? "var(--warn)" : "var(--success)",
              fontFamily: "var(--font-mono)",
            }}
          >
            {current.key_prefix ?? OLANO_KEY_PREFIX}…
          </span>
          <span className="muted" style={{ fontSize: 12 }}>
            {current.rejected ? t("olano.rejected") : t("olano.connected")}
          </span>
        </div>
        {current.shadowed_by ? (
          <p className="muted" data-testid="olano-key-shadowed" style={{ margin: 0, fontSize: 12 }}>
            {current.shadowed_by === "env" ? t("olano.shadowedEnv") : t("olano.shadowedDeployment")}
          </p>
        ) : null}
        {current.from_env ? (
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            {t("olano.fromEnv")}
          </p>
        ) : null}
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <a className="btn" href={current.billing_url} target="_blank" rel="noreferrer noopener">
            {t("olano.buyCredits")} ↗
          </a>
          <a className="btn" href={current.key_url} target="_blank" rel="noreferrer noopener">
            {t("olano.manageKeys")} ↗
          </a>
          {current.from_env ? null : (
            <button type="button" className="btn" disabled={busy} onClick={() => setReplacing(true)}>
              {t("olano.replace")}
            </button>
          )}
          {current.from_env ? null : confirmRemove ? (
            <>
              <button type="button" className="btn danger" disabled={busy} onClick={() => void remove()}>
                {busy ? <span className="spinner" /> : null}
                {t("olano.removeConfirm")}
              </button>
              <button type="button" className="btn" disabled={busy} onClick={() => setConfirmRemove(false)}>
                {t("olano.cancel")}
              </button>
            </>
          ) : (
            <button type="button" className="btn" disabled={busy} onClick={() => setConfirmRemove(true)}>
              {t("olano.remove")}
            </button>
          )}
        </div>
        {confirmRemove ? (
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            {t("olano.removeHint")}
          </p>
        ) : null}
        {err ? <div className="error-text">{err}</div> : null}
      </div>
    );
  }

  return (
    <form
      className="olano-key"
      data-testid="olano-credits-key"
      style={{ display: "grid", gap }}
      onSubmit={(e) => {
        e.preventDefault();
        void connect();
      }}
    >
      {envWins ? (
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          {t("olano.envWins")}
        </p>
      ) : null}
      <a className="ob-extlink" href={current.key_url || OLANO_KEYS_URL} target="_blank" rel="noreferrer noopener">
        {t("olano.getKey")}
        <span aria-hidden>↗</span>
      </a>
      <div className="field">
        <label>{t("olano.keyLabel")}</label>
        <PasswordField
          value={key}
          onChange={setKey}
          autoComplete="off"
          placeholder={`${OLANO_KEY_PREFIX}…`}
          autoFocus={autoFocus}
          name="olano-credits-key"
        />
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          {t("olano.keyHelp")}
        </p>
      </div>
      {err ? <div className="error-text">{err}</div> : null}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn primary" disabled={busy || !key.trim()}>
          {busy ? <span className="spinner" /> : null}
          {busy ? t("olano.checking") : t("olano.connect")}
        </button>
        {replacing ? (
          <button type="button" className="btn" disabled={busy} onClick={() => setReplacing(false)}>
            {t("olano.cancel")}
          </button>
        ) : null}
      </div>
    </form>
  );
}
