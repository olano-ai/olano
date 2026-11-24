import { useT } from "../../core/i18n";
import { logLevelColor, parseLog, VISIBLE_LOGS } from "./helpers";
import { STR } from "./strings";

export function ActivityLog({ logs }: { logs: string[] }) {
  const t = useT(STR);
  if (logs.length === 0) {
    return (
      <div className="card" style={{ marginTop: 12, padding: 0 }}>
        <div
          className="muted"
          style={{ padding: 24, textAlign: "center", fontSize: 13 }}
        >
          {t("activity.empty")}
        </div>
      </div>
    );
  }

  // Newest first, capped at VISIBLE_LOGS (mirrors slice(-30).reverse()).
  const rows = logs.slice(-VISIBLE_LOGS).reverse();

  return (
    <div
      className="card"
      style={{
        marginTop: 12,
        padding: "8px 4px",
        maxHeight: 240,
        overflow: "auto",
        fontFamily: "var(--font-mono)",
        fontSize: 12,
      }}
    >
      {rows.map((line, i) => {
        const p = parseLog(line);
        return (
          <div
            key={logs.length - i}
            style={{
              display: "flex",
              gap: 8,
              padding: "2px 8px",
              alignItems: "baseline",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
            title={p.raw}
          >
            {p.ts && (
              <span style={{ color: "var(--text-4)", flex: "0 0 auto" }}>{p.ts}</span>
            )}
            <span
              style={{
                color: logLevelColor(p.level),
                fontWeight: 600,
                textTransform: "uppercase",
                fontSize: 10,
                flex: "0 0 auto",
                minWidth: 38,
              }}
            >
              {p.level}
            </span>
            <span style={{ color: "var(--text-2)", minWidth: 0 }}>{p.msg}</span>
          </div>
        );
      })}
    </div>
  );
}
