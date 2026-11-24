// Weekly / monthly rollup reports over the daily per-agent highlights. The
// narrative is scoped server-side to the viewer's agents exactly like the daily
// journal; generation (which spends model budget over the whole fleet) is
// admin-only. Mirrors the daily JournalFeed's narrative block visually.

import { useCallback, useEffect, useState } from "react";
import { Sparkles, RefreshCw } from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { Markdown } from "../../ui/Markdown";
import { STR } from "./strings";
import type { HighlightReport, ReportList, ReportPeriod } from "./types";

export function ReportPanel({ period, isAdmin }: { period: ReportPeriod; isAdmin: boolean }) {
  const t = useT(STR);
  const toast = useToast();
  const [keys, setKeys] = useState<string[] | null>(null);
  const [key, setKey] = useState<string | null>(null);
  const [report, setReport] = useState<HighlightReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);

  const loadKeys = useCallback(() => {
    setKeys(null);
    api
      .get<ReportList>(`/api/analytics/reports/${period}`)
      .then((l) => {
        setKeys(l.keys);
        setKey((cur) => (cur && l.keys.includes(cur) ? cur : (l.keys[0] ?? null)));
      })
      .catch(() => setKeys([]));
  }, [period]);

  // Reset the selection when switching period so a weekly key never leaks into
  // the monthly view before its own keys load.
  useEffect(() => {
    setKey(null);
    setReport(null);
    loadKeys();
  }, [loadKeys]);

  useEffect(() => {
    if (!key) {
      setReport(null);
      return;
    }
    setLoading(true);
    api
      .get<HighlightReport>(`/api/analytics/reports/${period}/${encodeURIComponent(key)}`)
      .then(setReport)
      .catch(() => setReport(null))
      .finally(() => setLoading(false));
  }, [period, key]);

  const generate = useCallback(async () => {
    setGenerating(true);
    try {
      // No anchor → the current period. Regenerates and returns the fresh report.
      const fresh = await api.post<HighlightReport>(`/api/analytics/reports/${period}/generate`);
      setReport(fresh);
      setKey(fresh.key);
      loadKeys();
      if (fresh.narrative && !fresh.narrative_error) toast.push(t("toast.report"), "info");
      else {
        const reason = fresh.narrative_error;
        toast.push(reason ? `${t("toast.reportFailed")}: ${reason}` : t("toast.reportFailed"), "error");
      }
    } catch {
      toast.push(t("toast.reportFailed"), "error");
    } finally {
      setGenerating(false);
    }
  }, [period, loadKeys, toast, t]);

  return (
    <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <select
          className="input"
          aria-label={t("report.pick")}
          value={key ?? ""}
          disabled={!keys || keys.length === 0}
          onChange={(e) => setKey(e.target.value)}
          style={{ maxWidth: 220 }}
        >
          {keys && keys.length === 0 && <option value="">{t("report.none")}</option>}
          {(keys ?? []).map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        {isAdmin && (
          <button
            className="btn"
            disabled={generating}
            style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 10px", fontSize: 12.5 }}
            onClick={() => void generate()}
          >
            {generating ? (
              <RefreshCw size={14} className="lc-spin" strokeWidth={1.75} />
            ) : (
              <Sparkles size={14} strokeWidth={1.75} />
            )}
            {generating ? t("report.generating") : t("report.generate")}
          </button>
        )}
      </div>

      {loading && !report ? (
        <div className="spinner" style={{ marginTop: 12 }} />
      ) : report ? (
        <div>
          {report.start && report.end && (
            <p className="muted" style={{ margin: "0 0 8px", fontSize: 12 }}>
              {t("report.range", { start: report.start, end: report.end })}
            </p>
          )}
          {report.narrative ? (
            <div style={{ fontSize: 13.5, lineHeight: 1.55 }}>
              <Markdown text={report.narrative} />
            </div>
          ) : (
            <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
              {t("report.quiet")}
            </p>
          )}
        </div>
      ) : (
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          {t("report.empty")}
        </p>
      )}
    </div>
  );
}
