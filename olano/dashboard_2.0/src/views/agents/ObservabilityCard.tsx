// Overview card for `agent.json5 → observability.langfuse`: whether this
// agent's turns are traced to Langfuse, and how much each trace contains.
//
// Every field is three-state (empty follows Config → Observability), so each
// control has a "Follow Config" choice and the card says what is in effect
// right now (GET /api/observability/langfuse/links, which resolves the same
// policy the runner uses). Saved through the agent PUT, which is admin-only;
// the agent-config tools refuse a change without an admin (the backend's
// `_observability_denial`). Applies from the agent's next turn.

import { useCallback, useEffect, useState } from "react";

import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { STR } from "../observability/strings";
import { KvRow } from "./Toggle";
import type { AgentLangfuseConfig, AgentObservabilityConfig } from "./types";

interface Effective {
  traced: boolean;
  capture?: string;
  sample_rate?: number;
  credential_source?: string;
}

function triState(v: boolean | null | undefined): string {
  return v === true ? "on" : v === false ? "off" : "";
}

export function ObservabilityCard({
  agentName,
  value,
  onSave,
}: {
  agentName: string;
  value: AgentObservabilityConfig | undefined;
  onSave: (next: AgentObservabilityConfig) => Promise<boolean> | void;
}) {
  const t = useT(STR);
  const lf: AgentLangfuseConfig = value?.langfuse ?? {};
  const [rate, setRate] = useState(lf.sample_rate == null ? "" : String(lf.sample_rate));
  const [effective, setEffective] = useState<Effective | null>(null);
  useEffect(() => setRate(lf.sample_rate == null ? "" : String(lf.sample_rate)), [lf.sample_rate]);

  const load = useCallback(() => {
    api
      .get<Effective>(`/api/observability/langfuse/links?agent=${encodeURIComponent(agentName)}`)
      .then(setEffective)
      .catch(() => setEffective(null));
  }, [agentName]);
  useEffect(load, [load]);

  async function save(patch: Partial<AgentLangfuseConfig>) {
    const next: AgentObservabilityConfig = {
      ...(value || {}),
      langfuse: { enabled: null, capture: null, sample_rate: null, ...lf, ...patch },
    };
    await onSave(next);
    load();
  }

  const rateOk = rate.trim() === "" || (/^\d*\.?\d+$/.test(rate.trim()) && Number(rate) >= 0 && Number(rate) <= 1);
  const sourceLabel =
    effective?.credential_source === "agent vault"
      ? t("ob.agent.source.agentVault")
      : effective?.credential_source === "env"
        ? t("ob.agent.source.env")
        : t("ob.agent.source.vault");
  const state = effective?.traced
    ? t("ob.agent.effective.on", {
        capture: effective.capture === "full" ? t("ob.capture.full") : t("ob.capture.metadata"),
        rate: `${Math.round((effective.sample_rate ?? 1) * 100)}%`,
        source: sourceLabel,
      })
    : t("ob.agent.effective.off");

  return (
    <div className="card" style={{ padding: 16 }} data-testid="observability-card">
      <strong style={{ fontSize: 13 }}>{t("ob.agent.title")}</strong>
      <p className="muted" style={{ fontSize: 12, margin: "6px 0 8px", lineHeight: 1.5 }}>
        {t("ob.agent.intro")}
      </p>
      <KvRow label={t("ob.agent.enabled")} hint={t("ob.agent.enabled.hint")}>
        <select
          className="input"
          data-testid="observability-agent-enabled"
          value={triState(lf.enabled)}
          onChange={(e) =>
            void save({ enabled: e.target.value === "" ? null : e.target.value === "on" })
          }
          style={{ width: 180 }}
        >
          <option value="">{t("ob.agent.inherit")}</option>
          <option value="on">{t("ob.agent.on")}</option>
          <option value="off">{t("ob.agent.off")}</option>
        </select>
      </KvRow>
      <KvRow label={t("ob.capture")} hint={t("ob.agent.capture.hint")}>
        <select
          className="input"
          value={lf.capture ?? ""}
          onChange={(e) => void save({ capture: e.target.value || null })}
          style={{ width: 180 }}
        >
          <option value="">{t("ob.agent.inherit")}</option>
          <option value="metadata">{t("ob.capture.metadata")}</option>
          <option value="full">{t("ob.capture.full")}</option>
        </select>
      </KvRow>
      {lf.capture === "full" && (
        <div
          role="alert"
          className="card"
          style={{ margin: "8px 0", padding: 10, fontSize: 12, lineHeight: 1.5, borderColor: "var(--warn, #c98a00)" }}
        >
          {t("ob.fullWarning")}
        </div>
      )}
      <KvRow label={t("ob.sample")} hint={t("ob.agent.sample.hint")}>
        <input
          className="input"
          inputMode="decimal"
          placeholder={t("ob.agent.inherit")}
          value={rate}
          onChange={(e) => setRate(e.target.value)}
          onBlur={() => {
            if (!rateOk) return;
            const next = rate.trim() === "" ? null : Number(rate);
            if (next !== (lf.sample_rate ?? null)) void save({ sample_rate: next });
          }}
          style={{ width: 180, ...(rateOk ? {} : { borderColor: "var(--danger)" }) }}
        />
      </KvRow>
      <div className="muted" data-testid="observability-agent-effective" style={{ fontSize: 12, marginTop: 8 }}>
        {t("ob.agent.effective", { state })}
      </div>
    </div>
  );
}
