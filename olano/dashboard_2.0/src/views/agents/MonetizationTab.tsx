// Per-agent Monetization tab — configure end-user subscription pricing from the
// dashboard instead of hand-editing agent.json5. Writes the non-secret
// `monetization` block (enabled, providers, trial, plan pricing/copy). Payment
// SECRETS (STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET) are NOT collected here — they
// live in the agent's vault; this tab only shows guidance + the webhook URL.
//
// Because the running agent reads its monetization config live, changes take
// effect only after a restart — hence the "Restart to apply" affordance.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useToast } from "../../ui/toast";
import { Field } from "./Modal";
import { Toggle, KvRow } from "./Toggle";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import type { AgentConfig, MonetizationConfig, MonetizationPlan } from "./types";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

const CURRENCY_SYMBOLS: Record<string, string> = { usd: "$", eur: "€", gbp: "£", jpy: "¥" };

// Mirror of olano/monetization/pricing.py:format_price so the preview matches
// exactly what the bot shows a user.
function formatPrice(plan: MonetizationPlan, providers: string[]): string {
  const interval = plan.interval === "year" ? "year" : "month";
  let fiat = "";
  if (providers.includes("stripe") && (plan.price_cents || 0) > 0) {
    const amt = (plan.price_cents || 0) / 100;
    const amtStr = Number.isInteger(amt) ? String(amt) : amt.toFixed(2);
    const cur = (plan.currency || "usd").toLowerCase();
    const sym = CURRENCY_SYMBOLS[cur];
    fiat = sym ? `${sym}${amtStr}/${interval}` : `${cur.toUpperCase()} ${amtStr}/${interval}`;
  }
  let stars = "";
  if (providers.includes("telegram_stars") && (plan.stars_amount || 0) > 0) {
    stars = `${plan.stars_amount} ⭐/${interval}`;
  }
  if (fiat && stars) return `${fiat} or ${stars}`;
  return fiat || stars;
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card" style={{ padding: 16, marginBottom: 14 }}>
      <strong style={{ fontSize: 13, display: "block", marginBottom: 10 }}>{title}</strong>
      {children}
    </div>
  );
}

const DEFAULT_PLAN: MonetizationPlan = {
  price_cents: 500,
  currency: "usd",
  interval: "month",
  stripe_price_id: "",
  stars_amount: 0,
  product_name: "Subscription",
  offer_copy: "Subscribe to keep receiving updates.",
};

export function MonetizationTab({
  cfg,
  setCfg,
  savePatch,
  agentName,
}: {
  cfg: AgentConfig;
  setCfg: (c: AgentConfig) => void;
  savePatch: (patch: Partial<AgentConfig>, msg?: string) => Promise<boolean>;
  agentName: string;
}) {
  const toast = useToast();
  const t = useT(STR);
  // Local draft so typing doesn't fire a PUT per keystroke; committed on Save.
  const [draft, setDraft] = useState<MonetizationConfig>({});
  const [saving, setSaving] = useState(false);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    const m = cfg.monetization || {};
    setDraft({
      enabled: !!m.enabled,
      providers: m.providers ? [...m.providers] : [],
      trial_days: m.trial_days ?? 3,
      gate_groups: !!m.gate_groups,
      plan: { ...DEFAULT_PLAN, ...(m.plan || {}) },
    });
  }, [cfg.monetization]);

  const plan = draft.plan || DEFAULT_PLAN;
  const providers = draft.providers || [];

  const setPlan = (patch: Partial<MonetizationPlan>) =>
    setDraft((d) => ({ ...d, plan: { ...(d.plan || DEFAULT_PLAN), ...patch } }));

  const toggleProvider = (name: "stripe" | "telegram_stars", on: boolean) =>
    setDraft((d) => {
      const set = new Set(d.providers || []);
      if (on) set.add(name);
      else set.delete(name);
      return { ...d, providers: Array.from(set) as ("stripe" | "telegram_stars")[] };
    });

  const pricePreview = useMemo(() => formatPrice(plan, providers), [plan, providers]);

  const webhookUrl = `${window.location.origin}/api/monetization/stripe/${encodeURIComponent(agentName)}`;

  const save = useCallback(async () => {
    setSaving(true);
    const monetization = { ...draft };
    const ok = await savePatch({ monetization }, t("mon.toast.pricingSaved"));
    if (ok) setCfg({ ...cfg, monetization });
    setSaving(false);
  }, [draft, savePatch, setCfg, cfg, t]);

  const restart = useCallback(async () => {
    setRestarting(true);
    try {
      await api.post(`/api/agents/${encodeURIComponent(agentName)}/restart`, {});
      toast.push(t("mon.toast.restartingInfo"));
    } catch (e) {
      toast.push(errText(e, t("mon.toast.restartFailed")), "error");
    } finally {
      setRestarting(false);
    }
  }, [agentName, toast, t]);

  return (
    <div style={{ maxWidth: 720 }}>
      <Card title={t("mon.subscription")}>
        <KvRow
          label={t("mon.chargeUsers")}
          hint={t("mon.chargeUsersHint")}
        >
          <Toggle checked={!!draft.enabled} onChange={(v) => setDraft((d) => ({ ...d, enabled: v }))} />
        </KvRow>
        <KvRow label={t("mon.trialDays")} hint={t("mon.trialDaysHint")}>
          <input
            type="number"
            className="input"
            style={{ width: 90 }}
            min={0}
            value={draft.trial_days ?? 3}
            onChange={(e) => setDraft((d) => ({ ...d, trial_days: Math.max(0, Number(e.target.value) || 0) }))}
          />
        </KvRow>
        <KvRow label={t("mon.gateGroups")} hint={t("mon.gateGroupsHint")}>
          <Toggle checked={!!draft.gate_groups} onChange={(v) => setDraft((d) => ({ ...d, gate_groups: v }))} />
        </KvRow>
      </Card>

      <Card title={t("mon.paymentRails")}>
        <KvRow label={t("mon.stripe")} hint={t("mon.stripeHint")}>
          <Toggle checked={providers.includes("stripe")} onChange={(v) => toggleProvider("stripe", v)} />
        </KvRow>
        <KvRow label={t("mon.telegramStars")} hint={t("mon.telegramStarsHint")}>
          <Toggle checked={providers.includes("telegram_stars")} onChange={(v) => toggleProvider("telegram_stars", v)} />
        </KvRow>
      </Card>

      <Card title={t("mon.pricing")}>
        <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
          <Field label={t("mon.priceCents")} hint={t("mon.priceCentsHint")}>
            <input
              type="number"
              className="input"
              min={0}
              value={plan.price_cents ?? 0}
              onChange={(e) => setPlan({ price_cents: Math.max(0, Number(e.target.value) || 0) })}
            />
          </Field>
          <Field label={t("mon.currency")}>
            <select
              className="input"
              value={plan.currency || "usd"}
              onChange={(e) => setPlan({ currency: e.target.value })}
            >
              {["usd", "eur", "gbp", "jpy", "crc"].map((c) => (
                <option key={c} value={c}>
                  {c.toUpperCase()}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("mon.interval")}>
            <select
              className="input"
              value={plan.interval || "month"}
              onChange={(e) => setPlan({ interval: e.target.value as "month" | "year" })}
            >
              <option value="month">{t("mon.monthly")}</option>
              <option value="year">{t("mon.yearly")}</option>
            </select>
          </Field>
          <Field label={t("mon.starsAmount")} hint={t("mon.starsAmountHint")}>
            <input
              type="number"
              className="input"
              min={0}
              value={plan.stars_amount ?? 0}
              onChange={(e) => setPlan({ stars_amount: Math.max(0, Number(e.target.value) || 0) })}
            />
          </Field>
        </div>
        <Field label={t("mon.stripePriceId")} hint={t("mon.stripePriceIdHint")}>
          <input
            className="input"
            placeholder={t("mon.stripePriceIdPlaceholder")}
            value={plan.stripe_price_id || ""}
            onChange={(e) => setPlan({ stripe_price_id: e.target.value })}
          />
        </Field>
        <Field label={t("mon.productName")}>
          <input
            className="input"
            value={plan.product_name || ""}
            onChange={(e) => setPlan({ product_name: e.target.value })}
          />
        </Field>
        <Field label={t("mon.offerCopy")} hint={t("mon.offerCopyHint")}>
          <textarea
            className="input"
            rows={2}
            value={plan.offer_copy || ""}
            onChange={(e) => setPlan({ offer_copy: e.target.value })}
          />
        </Field>
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {t("mon.usersWillSee")} <strong>{pricePreview || t("mon.noPricingSet")}</strong>
        </div>
      </Card>

      <Card title={t("mon.paymentSecrets")}>
        <div className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
          {t("mon.secretsIntroA")} <code>STRIPE_API_KEY</code> {t("mon.secretsIntroB")} <code>STRIPE_WEBHOOK_SECRET</code>{" "}
          {t("mon.secretsIntroC")} <strong>{t("mon.secretsVault")}</strong> {t("mon.secretsIntroD")}
        </div>
        <input className="input" readOnly value={webhookUrl} onFocus={(e) => e.target.select()} style={{ marginTop: 8 }} />
      </Card>

      <div className="row" style={{ gap: 10, marginTop: 4 }}>
        <button className="btn primary" onClick={save} disabled={saving}>
          {saving ? t("mon.saving") : t("mon.savePricing")}
        </button>
        <button className="btn" onClick={restart} disabled={restarting}>
          {restarting ? t("mon.restarting") : t("mon.restartToApply")}
        </button>
        <span className="muted" style={{ fontSize: 12, alignSelf: "center" }}>
          {t("mon.restartNote")}
        </span>
      </div>
    </div>
  );
}
