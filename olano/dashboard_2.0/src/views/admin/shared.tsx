// Small presentational pieces shared by the admin sections (team roster and
// agent access). Kept in one place so both tables render a human identically.
import type { CSSProperties, ReactNode } from "react";

// Two-line section header: title (with optional trailing slot) + subtitle.
export function SectionHead({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle: string;
  right?: ReactNode;
}) {
  return (
    <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
      <div>
        <h2 style={{ margin: 0, fontSize: 15 }}>{title}</h2>
        <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5 }}>
          {subtitle}
        </p>
      </div>
      {right}
    </div>
  );
}

// Labeled form field: an uppercase micro-label (matched to the table column
// headers above it) stacked over the control. `style` carries the flex sizing.
export function Field({
  label,
  hint,
  children,
  style,
}: {
  label: string;
  /** Short inline suffix on the label, e.g. "optional". Keep it to a word or
   *  two: it sits on the label's baseline row, and a sentence here wraps into
   *  a narrow column that pushes the rest of the form off the screen. A full
   *  field explanation goes in an `.admin-form-note` under the row, which is
   *  full-width and can actually hold a paragraph. */
  hint?: string;
  children: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <div className="admin-field" style={style}>
      <label className="admin-field-label">
        {label}
        {hint && <span className="admin-field-hint">{hint}</span>}
      </label>
      {children}
    </div>
  );
}

// Initials for the avatar chip — name first ("Emily Chacon" → EC), else the
// email local part ("emily.chaconlee@…" → EC).
export function initials(u: { name?: string; email?: string }): string {
  const base = (u.name || u.email || "?").trim();
  const parts = base.split(/[\s@._-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}

export function Person({
  user,
  showTitle = true,
}: {
  user: { name?: string; email: string; username?: string; title?: string };
  /** Off where the caller renders the title in its own (editable) cell. */
  showTitle?: boolean;
}) {
  return (
    <div className="row" style={{ gap: 11, minWidth: 0 }}>
      <span className="avatar-sm">{initials(user)}</span>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 500, whiteSpace: "nowrap" }}>
          {user.name || "—"}
          {/* The permanent handle. Shown next to the editable name because it
              is what identifies this person in the issue tracker, where a
              display name never appears. */}
          {user.username && (
            <span className="muted" style={{ fontWeight: 400, marginLeft: 6, fontSize: 12.5 }}>
              @{user.username}
            </span>
          )}
        </div>
        {/* The position, under the name — the same line an agent card draws. */}
        {showTitle && !!(user.title || "").trim() && (
          <div style={{ fontSize: 12.5, overflow: "hidden", textOverflow: "ellipsis" }}>{user.title}</div>
        )}
        <div className="muted" style={{ fontSize: 12.5, overflow: "hidden", textOverflow: "ellipsis" }}>
          {user.email}
        </div>
      </div>
    </div>
  );
}
