// "Reply feedback": the thumbs people gave agents' replies over the Insights
// window, per agent. Read from GET /api/analytics/feedback, which counts the
// votes kept on the box (olano/core/feedback.py), so it works with Langfuse
// off. Scoped server-side to the agents the viewer may see.

import { useEffect, useState } from "react";
import { Info } from "lucide-react";

import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { STR } from "../chat/feedbackStrings";
import { A2AByAgentBar, ChartCard } from "./charts";

interface FeedbackSummary {
  up: number;
  down: number;
  by_agent: { agent: string; up: number; down: number }[];
}

export function FeedbackCard({ days, refreshKey }: { days: number; refreshKey?: string | number }) {
  const t = useT(STR);
  const [data, setData] = useState<FeedbackSummary | null>(null);

  useEffect(() => {
    let live = true;
    const query = days === 1 ? "hours=24" : `days=${days}`;
    api
      .get<FeedbackSummary>(`/api/analytics/feedback?${query}`)
      .then((v) => live && setData(v))
      .catch(() => live && setData(null));
    return () => {
      live = false;
    };
  }, [days, refreshKey]);

  const rows = (data?.by_agent ?? []).map((r) => ({ agent: r.agent, sent: r.up, received: r.down }));
  const hint = t("fb.insights.hint");
  return (
    <div data-testid="insights-feedback">
      <ChartCard
        title={t("fb.insights.title")}
        height={300}
        empty={rows.length === 0}
        emptyLabel={t("fb.insights.empty")}
        control={
          <span className="muted" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}>
            {data && (data.up > 0 || data.down > 0) && (
              <span data-testid="insights-feedback-totals">
                {t("fb.insights.up")} {data.up} · {t("fb.insights.down")} {data.down}
              </span>
            )}
            <span title={hint} aria-label={hint} role="img" style={{ display: "inline-flex" }}>
              <Info size={13} />
            </span>
          </span>
        }
      >
        <A2AByAgentBar data={rows} labels={{ sent: t("fb.insights.up"), received: t("fb.insights.down") }} />
      </ChartCard>
    </div>
  );
}
