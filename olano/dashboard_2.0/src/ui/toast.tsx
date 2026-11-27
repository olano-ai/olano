import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useI18n } from "../core/i18n";

interface ToastItem {
  id: number;
  msg: string;
  kind: "info" | "error" | "success";
}
interface ToastApi {
  push(msg: string, kind?: "info" | "error" | "success"): void;
}

const Ctx = createContext<ToastApi | null>(null);
let seq = 0;

// When the server is unreachable, every polling view's fetch fails at once and
// would spam dozens of identical "Could not load X" error toasts. The
// ServerMonitorProvider flips this flag on while offline so those error toasts
// are dropped centrally (the single connectivity overlay explains what's wrong),
// then flips it off on reconnect. Info toasts always pass.
let suppressErrorToasts = false;
export function setErrorToastSuppression(suppress: boolean): void {
  suppressErrorToasts = suppress;
}

// Between a deliberate sign-out and the next sign-in, the signed-out person's
// views still have requests in flight; each comes back 401 and would leave a
// "Could not load ..." on screen for whoever signs in next. Error toasts are
// dropped for that window, and the toasts already showing are cleared when
// the next person is in (core/useSignInLanding.ts).
let suppressWhileSignedOut = false;
let clearAll: (() => void) | null = null;
export function setSignedOutToastSuppression(suppress: boolean): void {
  suppressWhileSignedOut = suppress;
}
export function clearToasts(): void {
  clearAll?.();
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [items, setItems] = useState<ToastItem[]>([]);
  clearAll = () => setItems([]);

  const push = useCallback((msg: string, kind: "info" | "error" | "success" = "info") => {
    if (kind === "error" && (suppressErrorToasts || suppressWhileSignedOut)) return;
    const id = ++seq;
    setItems((x) => [...x, { id, msg, kind }]);
    // Errors linger longer — they're the primary failure channel and 4s was
    // easy to miss; any toast can also be clicked away immediately.
    window.setTimeout(
      () => setItems((x) => x.filter((t) => t.id !== id)),
      kind === "error" ? 8000 : 4200,
    );
  }, []);

  const dismiss = useCallback((id: number) => {
    setItems((x) => x.filter((t) => t.id !== id));
  }, []);

  // Stable context identity: consumers put `toast` in effect dep arrays, so a
  // fresh object per render would re-run those effects (e.g. re-fetch a view's
  // data) every time a toast appears or expires.
  const api = useMemo<ToastApi>(() => ({ push }), [push]);

  return (
    <Ctx.Provider value={api}>
      {children}
      {/* aria-live region: without it screen readers never hear a toast, and
          toasts are the app's primary success/error channel. Errors use
          role="alert" (assertive) so failures interrupt; the rest are polite. */}
      <div className="toast-host" role="status" aria-live="polite">
        {items.map((item) => (
          <div
            key={item.id}
            className={`toast ${item.kind}`}
            role={item.kind === "error" ? "alert" : undefined}
            onClick={() => dismiss(item.id)}
            title={t("common.dismiss")}
          >
            {item.msg}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}
