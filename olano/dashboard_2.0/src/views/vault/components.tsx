import { useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useT } from "../../core/i18n";
import { useDialogA11y } from "../../ui/dialogA11y";
import { STR } from "./strings";

// Small pulsing status dot — the Aurora take on the original badge's `.dot`.
// `tone` is a CSS variable name (e.g. "success" / "danger") used for its colour.
export function StatusDot({ tone, style }: { tone: string; style?: CSSProperties }) {
  return (
    <span
      aria-hidden
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        background: `var(--${tone})`,
        boxShadow: `0 0 0 3px color-mix(in srgb, var(--${tone}) 22%, transparent)`,
        flexShrink: 0,
        ...style,
      }}
    />
  );
}

// Overlay shell shared by every modal + side drawer in the Vault view. `side`
// renders a right-hand drawer (used by add/edit + rekey); otherwise a centered
// modal (used by unlock / reveal / confirm-delete).
export function Overlay({
  children,
  onClose,
  side,
  width,
  kind,
}: {
  children: ReactNode;
  onClose: () => void;
  side?: boolean;
  width?: number;
  kind?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  // Escape-to-close + focus trap/restore — these overlays were the only
  // dialogs in the app that ignored Escape entirely.
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
        // Aurora scrim: deep indigo wash + blur, matching .modal-overlay / .cmdk-overlay.
        background: "rgba(5, 4, 12, 0.55)",
        backdropFilter: "blur(3px)",
        WebkitBackdropFilter: "blur(3px)",
        // Same layer as every other modal (ties resolve by DOM order — the
        // app-level unlock prompt mounts last and wins). A hardcoded lower
        // value here once left the unlock modal buried under open dialogs.
        zIndex: "var(--z-modal)" as unknown as number,
        display: "flex",
        alignItems: side ? "stretch" : "center",
        justifyContent: side ? "flex-end" : "center",
        animation: "vault-fade var(--t) var(--ease)",
      }}
    >
      <div
        className="card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        data-kind={kind}
        onClick={(e) => e.stopPropagation()}
        style={{
          padding: 22,
          width: width ?? (side ? 460 : 400),
          maxWidth: "100%",
          animation: `${side ? "vault-slide-in" : "vault-pop-in"} var(--t) var(--ease)`,
          ...(side
            ? {
                height: "100%",
                borderRadius: 0,
                overflowY: "auto",
                borderLeft: "1px solid var(--border-strong)",
                boxShadow: "var(--shadow-lg)",
              }
            : { boxShadow: "var(--shadow-lg)", border: "1px solid var(--border-strong)" }),
        }}
      >
        {children}
      </div>
      {/* Scoped keyframes — globals.css is off-limits, so co-locate the overlay
          entrance animations here. Names are vault-prefixed to avoid clashes. */}
      <style>{OVERLAY_KEYFRAMES}</style>
    </div>
  );
}

const OVERLAY_KEYFRAMES = `
@keyframes vault-fade { from { opacity: 0 } to { opacity: 1 } }
@keyframes vault-slide-in { from { transform: translateX(16px); opacity: 0 } to { transform: none; opacity: 1 } }
@keyframes vault-pop-in { from { transform: translateY(8px) scale(0.98); opacity: 0 } to { transform: none; opacity: 1 } }
`;

/**
 * Off-screen username field, rendered inside a password form.
 *
 * A browser (or extension) password manager only offers to *generate* a
 * password — "Suggest password" — for a form it parses as a sign-up or
 * change-password form, and that parse needs an account the new password would
 * belong to. A form holding nothing but `type=password` inputs gives it no such
 * anchor, so the offer never appears. Chromium's guidance for exactly this case
 * is to carry the identifier in the same form and hide it with CSS.
 *
 * `display: none` rather than `type="hidden"`: managers skip hidden inputs when
 * looking for the username, but do read a CSS-hidden text input. `readOnly`
 * plus removal from the tab order and the a11y tree keeps it inert for people.
 */
export function UsernameAnchor({ value }: { value: string }) {
  return (
    <input
      type="text"
      name="username"
      autoComplete="username"
      value={value}
      readOnly
      tabIndex={-1}
      aria-hidden
      style={{ display: "none" }}
    />
  );
}

// Password input with an inline show/hide toggle. Optional Enter handler so the
// unlock modal can submit on Enter (parity with the original drawer behaviour).
//
// `name` is not decoration: it is the strongest text signal a password manager
// has for classifying a field ("new-password" vs "current-password"), and every
// input here is otherwise anonymous — no name, no id, and a `<label>` that is a
// sibling rather than an association. Pass one on any account/passphrase form.
export function PasswordField({
  value,
  onChange,
  placeholder,
  autoComplete,
  autoFocus,
  onEnter,
  name,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoComplete?: string;
  autoFocus?: boolean;
  onEnter?: () => void;
  name?: string;
}) {
  const t = useT(STR);
  const [show, setShow] = useState(false);
  return (
    <div className="row" style={{ gap: 6 }}>
      <input
        className="input"
        type={show ? "text" : "password"}
        value={value}
        placeholder={placeholder}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        name={name}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && onEnter) {
            e.preventDefault();
            onEnter();
          }
        }}
      />
      <button
        type="button"
        className="btn"
        style={{ flexShrink: 0 }}
        onClick={() => setShow((s) => !s)}
        title={show ? t("field.hide") : t("field.show")}
      >
        {show ? t("field.hide") : t("field.show")}
      </button>
    </div>
  );
}
