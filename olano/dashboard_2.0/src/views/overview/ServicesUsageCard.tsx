import { useEffect, useState } from "react";
import { BarChart3 } from "lucide-react";
import { api } from "../../core/api";
import { fmtCreditsAmount } from "../../core/costDisplay";
import { useT } from "../../core/i18n";
import { STR } from "./strings";

// Per-service olano-credit usage (Overview + Insights). CLOUD truth — GET
// /api/olano/services/usage proxies the deployment's gateway_calls aggregation
// at cloud.olano.ai (cached ~60s, stale-on-error), so these numbers match the
// cloud deployment view exactly. CREDITS ONLY: each managed service (chat tiers,
// olano-vision, olano-image, olano-tts, olano-stt, olano-web-search) with the
// credits it consumed — never a backend model, token count, or unit. Hidden on
// unmanaged installs ({available: false}) and when nothing has been spent.

interface ServiceLine {
  service: string;
  label: string;
  calls: number;
  credits: number;
}
interface ServiceUsage {
  available: boolean;
  window?: { since: string; days: number };
  total_credits?: number;
  services?: ServiceLine[];
  stale?: boolean;
}

export function ServicesUsageCard() {
  const t = useT(STR);
  const [data, setData] = useState<ServiceUsage | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      api
        .cachedGet<ServiceUsage>("/api/olano/services/usage", 60_000)
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

  // Unmanaged, unreachable, or nothing spent yet: no card.
  if (!data || !data.available) return null;
  const services = (data.services ?? []).filter((s) => s.credits > 0);
  if (services.length === 0) return null;

  const days = data.window?.days ?? 30;
  const total = data.total_credits ?? 0;

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
            <BarChart3 size={15} strokeWidth={1.75} /> {t("services.title")}
          </span>
          <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
            {t("services.lastDays", { days: String(days) })}
            {data.stale ? t("services.staleSuffix") : ""}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 22, fontWeight: 650, fontVariantNumeric: "tabular-nums" }}>
            {fmtCreditsAmount(total)}
          </div>
          <div className="muted" style={{ fontSize: 11 }}>
            {t("services.total")}
          </div>
        </div>
      </div>

      <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
        {services.map((s) => (
          <div
            key={s.service}
            className="row"
            style={{ justifyContent: "space-between", fontSize: 13 }}
          >
            <span className="muted">{s.label}</span>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>{fmtCreditsAmount(s.credits)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
