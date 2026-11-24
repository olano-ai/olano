import { useEffect, useState } from "react";
import { Coins, ExternalLink } from "lucide-react";
import { api } from "../../core/api";
import { fmtCreditsAmount } from "../../core/costDisplay";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

// Olano-credit balance card (Overview + Insights). The number is CLOUD truth —
// GET /api/olano/credits proxies the deployment's account at cloud.olano.ai
// (cached ~60s, stale-on-error) — never a locally computed sum; the runtime's
// per-turn charges are display estimates. Hidden entirely on unmanaged
// installs ({available: false}): there is no credit account to show.

interface CreditsBalance {
  available: boolean;
  balance?: number;
  included_remaining?: number;
  purchased_remaining?: number;
  monthly_included?: number;
  monthly_used?: number;
  period_end?: string | null;
  credits_per_usd?: number;
  manage_url?: string;
  stale?: boolean;
  age_seconds?: number;
}

function fmtResetDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function CreditsCard() {
  const t = useT(STR);
  const [data, setData] = useState<CreditsBalance | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      api
        .cachedGet<CreditsBalance>("/api/olano/credits", 60_000)
        .then((r) => {
          if (alive) setData(r);
        })
        .catch(() => {
          /* keep the last state; the card simply doesn't refresh */
        });
    };
    load();
    const id = window.setInterval(() => {
      if (!document.hidden) load();
    }, 60_000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  // Unmanaged (or never reachable): no account, no card.
  if (!data || !data.available) return null;

  const balance = data.balance ?? 0;
  const included = data.included_remaining ?? 0;
  const purchased = data.purchased_remaining ?? 0;
  const monthlyIncluded = data.monthly_included ?? 0;
  const monthlyUsed = data.monthly_used ?? 0;
  const usedPct =
    monthlyIncluded > 0 ? Math.min(100, Math.max(0, (monthlyUsed / monthlyIncluded) * 100)) : 0;
  const reset = fmtResetDate(data.period_end);
  const low = balance <= Math.max(50, monthlyIncluded * 0.05);

  return (
    <div className="card" style={{ marginTop: 14, padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <span
            style={{
              fontWeight: 600,
              fontSize: 14,
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <Coins size={15} strokeWidth={1.75} /> {t("credits.title")}
          </span>
          <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
            {t("credits.subtitle")}
            {data.stale ? t("credits.staleSuffix") : ""}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div
            style={{
              fontSize: 22,
              fontWeight: 650,
              fontVariantNumeric: "tabular-nums",
              color: low ? "var(--danger)" : undefined,
            }}
            title={t("credits.balanceBreakdown", {
              included: included.toLocaleString(),
              purchased: purchased.toLocaleString(),
            })}
          >
            {fmtCreditsAmount(balance)}
          </div>
          <div className="muted" style={{ fontSize: 11 }}>
            {t("credits.left")}
          </div>
        </div>
      </div>

      {monthlyIncluded > 0 && (
        <div style={{ marginTop: 10 }}>
          <div
            className="row"
            style={{ justifyContent: "space-between", fontSize: 12 }}
            aria-hidden="true"
          >
            <span className="muted">
              {t("credits.monthlyIncluded")}
              {reset ? t("credits.resetsSuffix", { reset }) : ""}
            </span>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {t("credits.usedOf", {
                used: Math.floor(monthlyUsed).toLocaleString(),
                total: Math.floor(monthlyIncluded).toLocaleString(),
              })}
            </span>
          </div>
          <div
            role="progressbar"
            aria-label={t("credits.usedLabel")}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(usedPct)}
            style={{
              marginTop: 4,
              height: 6,
              borderRadius: 999,
              background: "var(--bg-2, rgba(128,128,128,0.15))",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${usedPct}%`,
                borderRadius: 999,
                background: usedPct > 90 ? "var(--danger)" : "var(--accent)",
                transition: "width .5s",
              }}
            />
          </div>
        </div>
      )}

      <div className="row" style={{ justifyContent: "space-between", marginTop: 10, fontSize: 12 }}>
        <span className="muted">
          {t("credits.purchasedTopups")}{" "}
          <span style={{ fontVariantNumeric: "tabular-nums" }}>
            {Math.floor(purchased).toLocaleString()}
          </span>
        </span>
        {data.manage_url && (
          <a
            className="btn ghost"
            href={data.manage_url}
            target="_blank"
            rel="noreferrer"
            style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12 }}
          >
            {t("credits.buyCredits")} <ExternalLink size={12} strokeWidth={1.75} />
          </a>
        )}
      </div>
    </div>
  );
}
