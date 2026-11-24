// Overview card for `agent.json5 → gmail_watch`: wake the agent when new Gmail
// matching a search arrives.
//
// The watch reads Gmail only through the agent's Google connection, so the
// card leads with that connection and the watch's own last check (from
// GET /api/agents/{name}/gmail-watch, backed by olano/scheduler/gmail_watch.py)
// before the fields. A watch switched on without a connection is the failure
// this card exists to make visible: the runtime also puts it on the bell, but
// the person editing the setting should not have to find that out later.
//
// Edits save through the agent PUT and apply live (the route re-registers the
// scheduler job), like the Heartbeat card.

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { KvRow, Toggle } from "./Toggle";
import { STR } from "./strings";
import type { GmailWatchConfig } from "./types";

interface WatchStatus {
  enabled: boolean;
  status: string;
  detail: string;
  last_check_ms: number | null;
  watching_since_ms: number | null;
  last_delivered_ms: number | null;
  last_delivered_count: number | null;
  query: string;
  google: {
    connected?: boolean;
    scope?: string;
    shared?: boolean | null;
    needs_reauth?: boolean;
    auth_error?: string;
  };
}

const POLL_OPTIONS = [1, 2, 5, 10, 15, 30, 60, 120, 360, 720, 1440];
const DEFAULTS: Required<GmailWatchConfig> = {
  enabled: false,
  query: "",
  poll_minutes: 5,
  prompt: "",
  max_per_check: 10,
};

function when(ms: number | null | undefined): string {
  return ms ? new Date(ms).toLocaleString() : "";
}

export function GmailWatchCard({
  agentName,
  value,
  onSave,
}: {
  agentName: string;
  value: GmailWatchConfig | undefined;
  onSave: (next: GmailWatchConfig) => Promise<boolean> | void;
}) {
  const t = useT(STR);
  const navigate = useNavigate();
  const cfg = { ...DEFAULTS, ...(value || {}) };
  const [status, setStatus] = useState<WatchStatus | null>(null);
  const [query, setQuery] = useState(cfg.query);
  const [prompt, setPrompt] = useState(cfg.prompt);
  const [maxPer, setMaxPer] = useState(String(cfg.max_per_check));
  useEffect(() => setQuery(cfg.query), [cfg.query]);
  useEffect(() => setPrompt(cfg.prompt), [cfg.prompt]);
  useEffect(() => setMaxPer(String(cfg.max_per_check)), [cfg.max_per_check]);

  const load = useCallback(() => {
    api
      .get<WatchStatus>(`/api/agents/${encodeURIComponent(agentName)}/gmail-watch`)
      .then(setStatus)
      .catch(() => setStatus(null));
  }, [agentName]);
  useEffect(load, [load]);

  async function save(patch: Partial<GmailWatchConfig>) {
    const next: GmailWatchConfig = { ...(value || {}), ...patch };
    await onSave(next);
    load();
  }

  const google = status?.google || {};
  const connected = Boolean(google.connected);
  const needsReauth = Boolean(google.needs_reauth);
  const scopeLabel =
    google.scope === "agent"
      ? t("gw.scopeAgent")
      : google.scope === "shared"
        ? t("gw.scopeShared")
        : google.scope === "system"
          ? t("gw.scopeSystem")
          : "";
  const statusKey = `gw.status.${status?.status || (cfg.enabled ? "pending" : "off")}`;
  const openConnections = () => navigate(`/connections?agent=${encodeURIComponent(agentName)}`);

  return (
    <div className="card" style={{ padding: 16 }} data-testid="gmail-watch-card">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10, gap: 8 }}>
        <strong style={{ fontSize: 13 }}>{t("gw.title")}</strong>
        <button className="btn ghost" style={{ padding: "3px 8px", fontSize: 12 }} onClick={load}>
          {t("gw.refresh")}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "0 0 8px", lineHeight: 1.5 }}>
        {t("gw.intro")}
      </p>

      <KvRow
        label={t("gw.google")}
        hint={
          connected
            ? `${t("gw.googleConnected")}${scopeLabel ? ` (${scopeLabel})` : ""}`
            : needsReauth
              ? `${t("gw.googleRefused")}${google.auth_error ? `: ${google.auth_error}` : ""}`
              : t("gw.googleMissing")
        }
      >
        {connected ? (
          <span className="pill" style={{ fontSize: 11 }}>{t("gw.connectedPill")}</span>
        ) : (
          <button className="btn" style={{ fontSize: 12 }} onClick={openConnections}>
            {needsReauth ? t("gw.reconnect") : t("gw.connect")}
          </button>
        )}
      </KvRow>

      {cfg.enabled && status && !connected && (
        <div
          role="alert"
          className="card"
          style={{
            margin: "8px 0",
            padding: 10,
            fontSize: 12,
            lineHeight: 1.5,
            borderColor: "var(--warn, #c98a00)",
          }}
        >
          {t("gw.offlineWarning")}
        </div>
      )}

      <KvRow label={t("gw.enabled")} hint={t("gw.enabledHint")}>
        <Toggle
          checked={cfg.enabled}
          ariaLabel={t("gw.enabled")}
          onChange={(v) => void save({ enabled: v })}
        />
      </KvRow>

      {cfg.enabled && (
        <KvRow
          label={t("gw.state")}
          hint={
            <>
              <span data-testid="gmail-watch-status">{t(statusKey)}</span>
              {status?.detail ? <> {status.detail}</> : null}
              {status?.last_check_ms ? (
                <div>{t("gw.lastCheck").replace("{when}", when(status.last_check_ms))}</div>
              ) : null}
              {status?.last_delivered_ms ? (
                <div>
                  {t("gw.lastDelivered")
                    .replace("{n}", String(status.last_delivered_count ?? 0))
                    .replace("{when}", when(status.last_delivered_ms))}
                </div>
              ) : null}
            </>
          }
        >
          <span />
        </KvRow>
      )}

      <div style={{ padding: "9px 0", borderBottom: "1px solid var(--border)" }}>
        <label style={{ fontSize: 13, display: "block" }} htmlFor="gw-query">
          {t("gw.query")}
        </label>
        <div className="muted" style={{ fontSize: 11, margin: "1px 0 6px", lineHeight: 1.4 }}>
          {t("gw.queryHint")}
        </div>
        <input
          id="gw-query"
          className="input"
          value={query}
          placeholder="from:(@broker.example) has:attachment"
          onChange={(e) => setQuery(e.target.value)}
          onBlur={() => {
            if (query !== cfg.query) void save({ query });
          }}
          style={{ width: "100%" }}
        />
      </div>

      <KvRow label={t("gw.poll")} hint={t("gw.pollHint")}>
        <select
          className="input"
          aria-label={t("gw.poll")}
          value={cfg.poll_minutes}
          onChange={(e) => void save({ poll_minutes: parseInt(e.target.value, 10) })}
          style={{ width: 160 }}
        >
          {!POLL_OPTIONS.includes(cfg.poll_minutes) && (
            <option value={cfg.poll_minutes}>
              {t("gw.everyMin").replace("{n}", String(cfg.poll_minutes))}
            </option>
          )}
          {POLL_OPTIONS.map((m) => (
            <option key={m} value={m}>
              {m < 60
                ? t("gw.everyMin").replace("{n}", String(m))
                : t("gw.everyHour").replace("{n}", String(m / 60))}
            </option>
          ))}
        </select>
      </KvRow>

      <div style={{ padding: "9px 0", borderBottom: "1px solid var(--border)" }}>
        <label style={{ fontSize: 13, display: "block" }} htmlFor="gw-prompt">
          {t("gw.prompt")}
        </label>
        <div className="muted" style={{ fontSize: 11, margin: "1px 0 6px", lineHeight: 1.4 }}>
          {t("gw.promptHint")}
        </div>
        <textarea
          id="gw-prompt"
          className="input"
          value={prompt}
          placeholder={t("gw.promptPlaceholder")}
          onChange={(e) => setPrompt(e.target.value)}
          onBlur={() => {
            if (prompt !== cfg.prompt) void save({ prompt });
          }}
          style={{ width: "100%", minHeight: 90, resize: "vertical", fontSize: 13, lineHeight: 1.5 }}
        />
      </div>

      <KvRow label={t("gw.maxPer")} hint={t("gw.maxPerHint")}>
        <input
          className="input"
          type="number"
          aria-label={t("gw.maxPer")}
          min={1}
          max={50}
          style={{ width: 90 }}
          value={maxPer}
          onChange={(e) => setMaxPer(e.target.value)}
          onBlur={() => {
            const n = parseInt(maxPer, 10);
            if (!Number.isFinite(n) || n < 1 || n > 50) {
              setMaxPer(String(cfg.max_per_check));
              return;
            }
            if (n !== cfg.max_per_check) void save({ max_per_check: n });
          }}
          onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
        />
      </KvRow>

      <p className="muted" style={{ fontSize: 11, margin: "8px 0 0", lineHeight: 1.45 }}>
        {t("gw.footer")}
      </p>
    </div>
  );
}
