// Modal shell + labelled field for the Issues view. There is no shared modal
// library in dashboard 2.0 (see scheduler/Modal.tsx, the original of this
// pattern) — each view carries its own thin copy bound to its strings.

import { useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { useT } from "../../core/i18n";
import { useDialogA11y } from "../../ui/dialogA11y";
import { STR } from "./strings";

export function Modal({
  title,
  subtitle,
  onClose,
  children,
  width = 560,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  width?: number;
}) {
  const t = useT(STR);
  const panelRef = useRef<HTMLDivElement>(null);
  useDialogA11y(panelRef, onClose);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="card modal-card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{
          width,
          maxWidth: "100%",
          overflow: "auto",
          overscrollBehavior: "contain",
          padding: 22,
        }}
      >
        <div
          className="row"
          style={{ justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}
        >
          <div style={{ minWidth: 0 }}>
            <strong style={{ fontSize: 16 }}>{title}</strong>
            {subtitle ? (
              <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                {subtitle}
              </div>
            ) : null}
          </div>
          <button className="btn" onClick={onClose} aria-label={t("btn.close")} style={{ padding: "4px 9px" }}>
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field" style={{ marginBottom: 12 }}>
      <label>{label}</label>
      {children}
      {hint ? (
        <span className="muted" style={{ fontSize: 11 }}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}
