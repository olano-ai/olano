import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export function StatCard({
  label,
  icon,
  value,
  sub,
  to,
  linkLabel,
}: {
  label: string;
  icon: ReactNode;
  value: string | number;
  sub: ReactNode;
  /** Route to open when the card is clicked or activated from the keyboard.
   *  Omit for a card that only reports a number. */
  to?: string;
  /** Accessible name for the link — the visible text is three fragments
   *  ("Agents", "2", "0 running · 2 idle") that read as a poor link name. */
  linkLabel?: string;
}) {
  const body = (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {label}
        </span>
        <span aria-hidden style={{ display: "inline-flex", color: "var(--text-3)" }}>
          {icon}
        </span>
      </div>
      <div style={{ fontSize: 26, fontWeight: 700, marginTop: 6, lineHeight: 1.1 }}>{value}</div>
      <div className="muted" style={{ fontSize: 12, marginTop: 4, minHeight: 16 }}>
        {sub}
      </div>
    </>
  );

  if (!to) {
    return (
      <div className="card" style={{ padding: 16 }}>
        {body}
      </div>
    );
  }

  // A real <Link>, not a click handler on the <div>: that keeps keyboard
  // activation, middle-click / cmd-click into a new tab and the browser's own
  // hover URL preview working, none of which a synthetic onClick provides.
  //
  // The anchor reset is inline rather than in `lc-ov-stat-link` because that
  // sheet is injected from an effect — one frame later, which is long enough to
  // flash a blue underlined tile. The class carries only hover/focus, which
  // cannot fire before it lands.
  return (
    <Link
      className="card lc-ov-stat-link"
      to={to}
      aria-label={linkLabel}
      title={linkLabel}
      style={{ padding: 16, display: "block", color: "inherit", textDecoration: "none" }}
    >
      {body}
    </Link>
  );
}
