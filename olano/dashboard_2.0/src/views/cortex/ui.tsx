import { useRef, type ButtonHTMLAttributes, type CSSProperties, type ReactNode } from "react";
import { Markdown as SharedMarkdown } from "../../ui/Markdown";
import { useT } from "../../core/i18n";
import { useDialogA11y } from "../../ui/dialogA11y";
import { STR } from "./strings";

// ── Markdown ─────────────────────────────────────────────────────────────────
// Thin wrapper around the app-wide <Markdown> (the shared, XSS-safe `.md`
// renderer) that keeps this view's `source`/`style` call sites unchanged while
// adopting the Aurora markdown styling from globals.css.

export function Markdown({ source, style }: { source: string; style?: CSSProperties }) {
  // SharedMarkdown already renders a `.md` container; this wrapper only carries
  // the per-call-site sizing/margins the cortex view passes via `style`.
  return (
    <div style={{ fontSize: 13, lineHeight: 1.55, ...style }}>
      <SharedMarkdown text={source} />
    </div>
  );
}

// ── Busy button ──────────────────────────────────────────────────────────────
// A button that answers the click itself: while `busy` it keeps its colour,
// cannot be clicked again, and shows a spinner with what it is doing
// ("Discarding…"). A plain `disabled` dims the button to 0.45, which reads as
// "not available", and the row's far-right spinner was the only other sign
// that anything had happened. Every Cortex/Pulse action button goes through
// this so they all say it the same way.

export function BusyButton({
  busy,
  busyLabel,
  className = "btn",
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { busy: boolean; busyLabel?: ReactNode }) {
  return (
    <button
      {...rest}
      type={rest.type ?? "button"}
      className={busy ? `${className} busy` : className}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
    >
      {busy ? <span className="spinner btn-spinner" aria-hidden /> : null}
      {busy ? (busyLabel ?? children) : children}
    </button>
  );
}

// ── Confirm modal (replaces window.confirm with a themed inline dialog) ──────

export function ConfirmModal({
  title,
  message,
  confirmLabel,
  danger,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const t = useT(STR);
  const panelRef = useRef<HTMLDivElement>(null);
  // Escape, focus trap + restore — shared dialog behavior (ui/dialogA11y).
  useDialogA11y(panelRef, onCancel);

  return (
    <div className="modal-overlay" onClick={onCancel} style={{ zIndex: 60 }}>
      <div
        className="card"
        ref={panelRef}
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{ width: 440, maxWidth: "92vw", padding: 22, boxShadow: "var(--shadow-lg)" }}
      >
        <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>{title}</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 13, whiteSpace: "pre-wrap" }}>
          {message}
        </p>
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 18, gap: 8 }}>
          <button className="btn" onClick={onCancel}>
            {t("ui.cancel")}
          </button>
          <button className={danger ? "btn danger" : "btn primary"} onClick={onConfirm}>
            {confirmLabel || t("ui.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Right-side drawer shell ──────────────────────────────────────────────────

export function Drawer({
  title,
  sub,
  onClose,
  footer,
  children,
  width = 480,
}: {
  title: string;
  sub?: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
  width?: number;
}) {
  const t = useT(STR);
  const panelRef = useRef<HTMLDivElement>(null);
  // Escape, focus trap + restore — shared dialog behavior (ui/dialogA11y).
  useDialogA11y(panelRef, onClose);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        // Installed to the home screen the app runs edge to edge, so a
        // full-height side panel reaches under the status bar and the home
        // indicator. Padding the OVERLAY keeps the panel's `height: 100%`
        // resolving inside the safe box. Both are 0 in a browser tab.
        paddingTop: "var(--safe-top)",
        paddingBottom: "var(--safe-bottom)",
        background: "rgba(5, 4, 12, 0.55)",
        backdropFilter: "blur(3px)",
        display: "flex",
        justifyContent: "flex-end",
        zIndex: 50,
      }}
    >
      <div
        className="card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{
          width,
          maxWidth: "100%",
          height: "100%",
          borderRadius: 0,
          borderRight: "none",
          borderTop: "none",
          borderBottom: "none",
          boxShadow: "var(--shadow-lg)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          className="row"
          style={{
            justifyContent: "space-between",
            alignItems: "flex-start",
            padding: "16px 20px",
            borderBottom: "1px solid var(--border)",
          }}
        >
          <div style={{ minWidth: 0 }}>
            <strong style={{ fontSize: 16, display: "block" }}>{title}</strong>
            {sub ? (
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                {sub}
              </div>
            ) : null}
          </div>
          <button className="btn" onClick={onClose}>
            {t("ui.close")}
          </button>
        </div>
        <div style={{ flex: 1, overflow: "auto", padding: 20 }}>{children}</div>
        {footer ? (
          <div
            className="row"
            style={{ justifyContent: "flex-end", flexWrap: "wrap", gap: 8, padding: "14px 20px", borderTop: "1px solid var(--border)" }}
          >
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ── Empty state ──────────────────────────────────────────────────────────────

export function Empty({ title, sub }: { title: string; sub?: string }) {
  return (
    <div
      style={{
        background: "var(--surface)",
        borderRadius: "var(--r)",
        padding: "40px 24px",
        textAlign: "center",
      }}
    >
      <div style={{ fontWeight: 600, fontSize: 14 }}>{title}</div>
      {sub ? (
        <div className="muted" style={{ fontSize: 13, marginTop: 6 }}>
          {sub}
        </div>
      ) : null}
    </div>
  );
}

// ── Inline progress bar ──────────────────────────────────────────────────────

export function ProgressBar({ pct, danger }: { pct: number; danger?: boolean }) {
  return (
    <div style={{ height: 8, borderRadius: 999, background: "var(--surface-3)", overflow: "hidden" }}>
      <div
        style={{
          height: "100%",
          width: `${Math.max(0, Math.min(100, pct))}%`,
          background: danger ? "var(--danger)" : "var(--accent)",
          transition: "width 200ms ease",
        }}
      />
    </div>
  );
}
