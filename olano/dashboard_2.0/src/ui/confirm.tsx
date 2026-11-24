import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useDialogA11y } from "./dialogA11y";
import { useI18n } from "../core/i18n";

// App-wide themed confirm dialog — a promise-based replacement for the native
// window.confirm(). Mirrors the toast provider: wrap the app once, then call
// `const confirm = useConfirm()` and `if (await confirm({...})) …`.

export interface ConfirmOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

interface PendingState extends ConfirmOptions {
  resolve: (value: boolean) => void;
}

const Ctx = createContext<ConfirmFn | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<PendingState | null>(null);

  const confirm = useCallback<ConfirmFn>((opts) => {
    return new Promise<boolean>((resolve) => {
      setPending((cur) => {
        // A second confirm while one is pending replaces the dialog — settle
        // the displaced caller as "cancelled" so its `await` never strands.
        cur?.resolve(false);
        return { ...opts, resolve };
      });
    });
  }, []);

  const settle = useCallback(
    (value: boolean) => {
      setPending((cur) => {
        cur?.resolve(value);
        return null;
      });
    },
    [],
  );

  return (
    <Ctx.Provider value={confirm}>
      {children}
      {pending && <ConfirmDialog pending={pending} onSettle={settle} />}
    </Ctx.Provider>
  );
}

function ConfirmDialog({ pending, onSettle }: { pending: PendingState; onSettle: (v: boolean) => void }) {
  const { t } = useI18n();
  const okRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // Escape + focus trap/restore via the shared dialog hook; this dialog seeds
  // its own focus (the confirm button) and adds Enter-to-confirm on top.
  useDialogA11y(panelRef, () => onSettle(false), { initialFocus: false });

  // Focus the confirm button so Enter activates it natively. No global Enter
  // shortcut: the focus trap makes Cancel reachable via Tab, and a window-level
  // Enter listener would confirm a danger action even with Cancel focused.
  useEffect(() => {
    okRef.current?.focus();
  }, []);

  return (
    <div className="modal-overlay" onClick={() => onSettle(false)} style={{ zIndex: 80 }}>
      <div
        className="card"
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
        style={{ width: 420, maxWidth: "92vw", padding: 22, boxShadow: "var(--shadow-lg)" }}
        role="alertdialog"
        aria-modal="true"
      >
        {pending.title && <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>{pending.title}</h2>}
        <p className="muted" style={{ margin: 0, fontSize: 13, whiteSpace: "pre-wrap", lineHeight: 1.5 }}>
          {pending.message}
        </p>
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 18, gap: 8 }}>
          <button className="btn" onClick={() => onSettle(false)}>
            {pending.cancelLabel || t("common.cancel")}
          </button>
          <button
            ref={okRef}
            className={pending.danger ? "btn danger" : "btn primary"}
            onClick={() => onSettle(true)}
          >
            {pending.confirmLabel || t("common.ok")}
          </button>
        </div>
      </div>
    </div>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useConfirm must be used within <ConfirmProvider>");
  return ctx;
}
