import { AlertTriangle, RotateCcw } from "lucide-react";
import { useT, type LocalDict } from "../core/i18n";

// Shared load-failure card: an honest error with an inline retry, so a failed
// fetch never masquerades as an empty view ("No agents yet" after a 500) whose
// only signal was a toast that vanished 4 seconds ago. Mirrors the pattern
// MemoryView/ConnectionsView already used; use this instead of hand-rolling.

const STR: LocalDict = {
  en: {
    "title": "Couldn't load this view.",
    "retry": "Try again",
  },
  zh: {
    "title": "无法加载此视图。",
    "retry": "重试",
  },
  es: {
    "title": "No se pudo cargar esta vista.",
    "retry": "Reintentar",
  },
};

export function ErrorState({
  message,
  detail,
  onRetry,
}: {
  // Override the generic headline with a view-specific message (translated by
  // the caller). `detail` is the raw error string, shown smaller underneath.
  message?: string;
  detail?: string;
  onRetry?: () => void;
}) {
  const t = useT(STR);
  return (
    <div
      className="card"
      role="alert"
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-start",
        gap: 10,
        padding: 18,
        marginTop: 16,
        maxWidth: 520,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--warn)" }}>
        <AlertTriangle size={16} />
        <span style={{ fontWeight: 600, fontSize: 14 }}>{message || t("title")}</span>
      </div>
      {detail && (
        <div className="muted" style={{ fontSize: 12.5, wordBreak: "break-word" }}>
          {detail}
        </div>
      )}
      {onRetry && (
        <button className="btn" onClick={onRetry}>
          <RotateCcw size={13} /> {t("retry")}
        </button>
      )}
    </div>
  );
}
