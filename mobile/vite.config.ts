/**
 * Builds the app's screens from src/ into www/ (Capacitor's webDir).
 *
 * The app is a regular single-page React app with Ionic's components. It is
 * served by the native shell from the app itself (capacitor://localhost on
 * iOS, https://localhost on Android) and never loads a deployment's pages:
 * it calls the deployment's API through the OlanoHttp native plugin.
 *
 * A few pure modules come from the dashboard's source tree (src/shared/
 * dashboard.ts lists them), so the app and the dashboard render replies and
 * fold a turn the same way. `highlight.js`, which the dashboard's lazy code
 * highlighter imports, resolves to this project's own copy, because CI never
 * installs the dashboard's node_modules for the mobile build.
 *
 * `npm run dev` serves the app in a desktop browser and proxies /api to a
 * runtime (OLANO_DEV_RUNTIME, default http://127.0.0.1:8765); `npm run
 * preview` serves the built app the same way. In a browser
 * the app talks to the API with fetch() on its own origin instead of the
 * native plugin; see src/net/transport.ts.
 */

import type { IncomingMessage } from "node:http";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const root = fileURLToPath(new URL("./src/", import.meta.url));
const outDir = fileURLToPath(new URL("./www/", import.meta.url));
const dashboardSrc = fileURLToPath(new URL("../olano/dashboard_2.0/src/", import.meta.url));
const highlightJs = fileURLToPath(new URL("./node_modules/highlight.js/", import.meta.url));

/**
 * The app runs only its own code. `style-src 'unsafe-inline'` is for Ionic's
 * components, which put their styles in <style> elements; images and media
 * from the deployment arrive as data: and blob: URLs made by native code.
 */
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "media-src 'self' data: blob:",
  // A PDF from a reply is drawn from a blob: URL (ui/FileViewer.tsx).
  "frame-src blob:",
  "font-src 'self' data:",
  // The app's public remote config (src/appConfig.ts) when the screens run in
  // a browser; inside the app it goes through OlanoHttp and CSP never sees it.
  // A path source: that one URL, nothing else on the host.
  "connect-src 'self' https://cloud.olano.ai/api/v1/mobile/config",
  "base-uri 'none'",
  "form-action 'none'",
  "object-src 'none'",
].join("; ");

/** `<!-- olano:csp -->` becomes the policy above. Build only: it would block Vite's dev client. */
function contentSecurityPolicy(): Plugin {
  return {
    name: "olano-csp",
    apply: "build",
    transformIndexHtml: (html) =>
      html.replace("<!-- olano:csp -->", () => `<meta http-equiv="Content-Security-Policy" content="${CSP}" />`),
  };
}

const proxy = {
  "/api": {
    target: process.env.OLANO_DEV_RUNTIME || "http://127.0.0.1:8765",
    changeOrigin: false,
    // The live browser view is a WebSocket under /api.
    ws: true,
    // Streams (chat, /api/stream) must reach the page as they arrive.
    configure: (proxy: { on: (event: "proxyRes", listener: (res: IncomingMessage) => void) => void }) => {
      proxy.on("proxyRes", (res) => {
        if (String(res.headers["content-type"] || "").includes("text/event-stream")) {
          res.headers["x-accel-buffering"] = "no";
        }
      });
    },
  },
};

export default defineConfig({
  root,
  base: "./",
  publicDir: false,
  plugins: [react(), contentSecurityPolicy()],
  resolve: {
    alias: [{ find: /^highlight\.js\//, replacement: highlightJs }],
  },
  server: {
    port: Number(process.env.OLANO_DEV_PORT || 5199),
    strictPort: true,
    fs: { allow: [root, dashboardSrc, fileURLToPath(new URL("./node_modules/", import.meta.url))] },
    proxy,
  },
  // `vite preview` serves the built app (what ships in the native projects)
  // the same way; the end-to-end tests run against it.
  preview: {
    port: Number(process.env.OLANO_DEV_PORT || 5199),
    strictPort: true,
    proxy,
  },
  build: {
    outDir,
    emptyOutDir: true,
    // iOS 15 WKWebView is the oldest engine the app supports.
    target: ["es2020", "safari15", "chrome90"],
    reportCompressedSize: false,
    chunkSizeWarningLimit: 2000,
  },
});
