import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/globals.css";
import { App } from "./app";
import { AuthProvider } from "./core/auth";
import { I18nProvider } from "./core/i18n";
import { ToastProvider } from "./ui/toast";
import { ConfirmProvider } from "./ui/confirm";
import { installViewportInset } from "./ui/viewportInset";
import { installPwa, stripReloadBustParam } from "./core/pwa";

// Register the service worker and start listening for the install prompt.
// A no-op under `vite dev` and wherever service workers are unavailable.
// First, because it marks the root element as an installed shell — the
// measurement below reads that marker to decide whether the window or the
// visual viewport is the honest one.
installPwa();

// Keep `--app-vh` (the shell's height) and `--modal-vh` (the visible box for a
// fixed overlay) in step with the window — browser chrome, iOS keyboard,
// rotation — before the first frame paints and the first dialog can open.
installViewportInset();

// Clear the one-shot cache-buster `hardReset()` appends, before the router
// reads the URL — it is a transport detail, not app state, and it must not end
// up in a copied link or a history entry.
stripReloadBustParam();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <ToastProvider>
        <ConfirmProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ConfirmProvider>
      </ToastProvider>
    </I18nProvider>
  </StrictMode>,
);
