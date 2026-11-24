// Daily work-journal highlights, surfaced as an Overview section above
// Approvals. Shows the most recent human narrative of what the fleet actually
// DID — conversations handled, scheduled tasks completed, and cortex activity
// per agent — with NO usage statistics. The narrative is written automatically
// each morning for the just-completed day (scheduler fleet job) and can be
// (re)generated on demand from here for today via "Generate highlights".
// Both this panel and the Insights "Work journal" read the same journal
// narrative, so they never disagree.

import { useCallback, useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { api } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT, type LocalDict } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { Markdown } from "../../ui/Markdown";
import { fmtDay } from "../insights/helpers";

const STR: LocalDict = {
  en: {
    title: "Daily highlights",
    subtitle: "What the fleet actually did — conversations, scheduled tasks, and cortex activity, written up each morning.",
    empty: "No highlights yet — generate one from the day's activity.",
    emptyReadonly: "No highlights yet for your agents.",
    generate: "Generate highlights",
    generating: "Writing…",
    today: "Today",
    generatedAt: "Generated {time} UTC",
    "toast.ok": "Highlights generated",
    "toast.fail": "Could not generate highlights",
  },
  es: {
    title: "Resumen del día",
    subtitle: "Lo que hizo la flota — conversaciones, tareas programadas y actividad del cortex, redactado cada mañana.",
    empty: "Aún no hay resumen — genera uno a partir de la actividad del día.",
    emptyReadonly: "Aún no hay resumen para tus agentes.",
    generate: "Generar resumen",
    generating: "Escribiendo…",
    today: "Hoy",
    generatedAt: "Generado {time} UTC",
    "toast.ok": "Resumen generado",
    "toast.fail": "No se pudo generar el resumen",
  },
  zh: {
    title: "每日要点",
    subtitle: "智能体队伍实际完成的工作——对话、定时任务与 cortex 活动，每天早晨自动汇总。",
    empty: "尚无要点 — 可根据当天活动生成一份。",
    emptyReadonly: "你的智能体尚无要点。",
    generate: "生成要点",
    generating: "生成中…",
    today: "今天",
    generatedAt: "生成于 {time} UTC",
    "toast.ok": "已生成要点",
    "toast.fail": "无法生成要点",
  },
};

interface LatestHighlights {
  date: string | null;
  narrative: string | null;
  generated_at: string | null;
  narrative_model?: string | null;
  today: string;
}

interface HighlightsResult {
  narrative: string | null;
  narrative_error?: string;
}

export function HighlightsSection() {
  const t = useT(STR);
  const toast = useToast();
  // The digest is scoped server-side to the viewer's agents; generation is a
  // paid fleet-wide LLM pass, so the button is admin-only.
  const isAdmin = !!useAuth().user?.is_admin;
  const [data, setData] = useState<LatestHighlights | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api
      .get<LatestHighlights>("/api/analytics/highlights/latest")
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => load(), [load]);

  async function generate() {
    // Always (re)generate for the current day so operators get a live view of
    // today's work; the morning job covers the just-completed day on its own.
    const day = data?.today ?? new Date().toISOString().slice(0, 10);
    setGenerating(true);
    try {
      const updated = await api.post<HighlightsResult>(`/api/analytics/journal/${day}/highlights`);
      if (updated.narrative && !updated.narrative_error) {
        toast.push(t("toast.ok"), "info");
        load();
      } else {
        // Surface the backend's reason (missing model, provider auth, …).
        const reason = updated.narrative_error;
        toast.push(reason ? `${t("toast.fail")}: ${reason}` : t("toast.fail"), "error");
      }
    } catch {
      toast.push(t("toast.fail"), "error");
    } finally {
      setGenerating(false);
    }
  }

  // The badge names the digest's day — only meaningful once a narrative exists.
  const dateLabel =
    data?.narrative && data.date
      ? data.date === data.today
        ? t("today")
        : fmtDay(data.date)
      : "";

  return (
    <section>
      <div className="row" style={{ justifyContent: "space-between", marginTop: 28, flexWrap: "wrap", gap: 8 }}>
        <div className="row" style={{ gap: 8 }}>
          <h2 style={{ fontSize: 16, margin: 0 }}>{t("title")}</h2>
          {dateLabel && <span className="badge">{dateLabel}</span>}
        </div>
        {isAdmin && (
          <button
            className="btn"
            disabled={generating}
            onClick={() => void generate()}
            style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 10px", fontSize: 12.5 }}
          >
            {generating ? <span className="spinner" /> : <Sparkles size={14} strokeWidth={1.75} />}
            {generating ? t("generating") : t("generate")}
          </button>
        )}
      </div>
      <p className="muted" style={{ margin: "4px 0 0", fontSize: 12.5 }}>{t("subtitle")}</p>

      {loading && !data ? (
        <div className="spinner" style={{ marginTop: 14 }} />
      ) : data?.narrative ? (
        <div className="card" style={{ marginTop: 12, padding: 16, maxWidth: 820 }}>
          <div style={{ fontSize: 13.5, lineHeight: 1.6 }}>
            <Markdown text={data.narrative} />
          </div>
          {data.generated_at && (
            <p className="muted" style={{ margin: "10px 0 0", fontSize: 11 }}>
              {t("generatedAt", { time: data.generated_at.slice(11, 16) })}
            </p>
          )}
        </div>
      ) : (
        <div
          className="muted"
          style={{ display: "flex", alignItems: "center", gap: 8, padding: "16px 0" }}
        >
          <Sparkles size={15} /> {isAdmin ? t("empty") : t("emptyReadonly")}
        </div>
      )}
    </section>
  );
}
