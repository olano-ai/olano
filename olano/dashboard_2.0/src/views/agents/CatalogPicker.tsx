import { useState } from "react";
import { RefreshCw, Search } from "lucide-react";

// A searchable, selectable card list used by the archetype / integration
// pickers. Flat, borderless, on-theme — no emoji (a coloured initial stands in
// for an avatar, matching the agent cards).

// Small segmented control used to pick a source (custom / team / expert / …).
export function Segmented<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="seg" style={{ marginBottom: 14 }}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={`seg-btn ${value === o.value ? "active" : ""}`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export interface PickItem {
  name: string;
  label?: string;
  description?: string;
  color?: string;
  badge?: string;
}

// One skeleton row, sized to the real card so the list doesn't jump on load.
function SkeletonRow() {
  return (
    <div className="catalog-card" aria-hidden style={{ pointerEvents: "none" }}>
      <span className="catalog-avatar skeleton-block" />
      <span className="catalog-body">
        <span className="catalog-name">
          <span className="skeleton-block" style={{ width: "38%", height: 11, borderRadius: 4 }} />
        </span>
        <span className="catalog-desc">
          <span className="skeleton-block" style={{ width: "76%", height: 9, borderRadius: 4 }} />
        </span>
      </span>
    </div>
  );
}

export function CatalogPicker({
  items,
  selected,
  onSelect,
  searchPlaceholder,
  emptyText,
  // Load state of the backing catalog. Without it an in-flight or failed fetch
  // renders as `emptyText`, which reads as "this box has no experts" — the
  // exact confusion that hid a missing archetype pack in production.
  status = "ready",
  errorText,
  retryText,
  onRetry,
}: {
  items: PickItem[];
  selected: string | null;
  onSelect: (name: string) => void;
  searchPlaceholder: string;
  emptyText: string;
  status?: "loading" | "ready" | "error";
  /** Translated failure line — callers pass it so this stays presentational. */
  errorText?: string;
  retryText?: string;
  onRetry?: () => void;
}) {
  const [q, setQ] = useState("");
  const query = q.trim().toLowerCase();
  const filtered = query
    ? items.filter((i) => `${i.label || i.name} ${i.description || ""}`.toLowerCase().includes(query))
    : items;
  const loading = status === "loading";
  const failed = status === "error";

  return (
    <div>
      <div style={{ position: "relative", marginBottom: 10 }}>
        <input
          className="input"
          placeholder={searchPlaceholder}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ paddingLeft: 30 }}
          disabled={loading}
        />
        <span style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "var(--text-4)", display: "inline-flex" }}>
          <Search size={14} strokeWidth={1.75} />
        </span>
      </div>
      <div className="catalog-list" aria-busy={loading || undefined}>
        {loading ? (
          <>
            <SkeletonRow />
            <SkeletonRow />
            <SkeletonRow />
            <SkeletonRow />
          </>
        ) : failed ? (
          <div style={{ textAlign: "center", padding: "24px 0", fontSize: 13 }}>
            <div className="error-text" style={{ marginBottom: 8 }}>
              {errorText}
            </div>
            {onRetry && (
              <button type="button" className="btn" onClick={onRetry}>
                <RefreshCw size={13} strokeWidth={1.75} style={{ marginRight: 6 }} />
                {retryText}
              </button>
            )}
          </div>
        ) : filtered.length === 0 ? (
          <div className="muted" style={{ textAlign: "center", padding: "24px 0", fontSize: 13 }}>
            {emptyText}
          </div>
        ) : (
          filtered.map((i) => {
            const color = i.color || "var(--accent)";
            return (
              <button
                key={i.name}
                type="button"
                className={`catalog-card ${selected === i.name ? "selected" : ""}`}
                onClick={() => onSelect(i.name)}
              >
                <span
                  className="catalog-avatar"
                  style={{ background: i.color ? `${color}22` : "var(--surface-3)", color: i.color ? color : "var(--text-2)" }}
                >
                  {(i.label || i.name).charAt(0).toUpperCase()}
                </span>
                <span className="catalog-body">
                  <span className="catalog-name">
                    {i.label || i.name}
                    {i.badge && <span className="badge catalog-badge">{i.badge}</span>}
                  </span>
                  {i.description && <span className="catalog-desc">{i.description}</span>}
                </span>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
