import { spawn } from "node:child_process";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the Lionclaw FastAPI runtime (default :8765),
// mirroring the production Node proxy (server.mjs). Session cookies ride
// through unchanged. Build output goes to dist/ for static serving.
const RUNTIME = process.env.LIONCLAW_RUNTIME_URL || "http://localhost:8765";

// Dev mirror of server.mjs's local control plane so the dashboard's
// connectivity overlay's "Restart server now" button works under `npm run dev`.
// The Vite process is independent of the runtime, so it can relaunch it.
function controlPlugin(): Plugin {
  return {
    name: "olano-control",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url === "/__control/ping" && req.method === "GET") {
          res.statusCode = 200;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ ok: true }));
          return;
        }
        if (req.url === "/__control/restart" && req.method === "POST") {
          // `start` is idempotent — boots the runtime if down, no-ops if up.
          let failed = false;
          try {
            const child = spawn("olano", ["server", "start"], {
              detached: true,
              stdio: "ignore",
            });
            child.on("error", () => {
              failed = true;
            });
            child.unref();
          } catch {
            failed = true;
          }
          setTimeout(() => {
            res.statusCode = failed ? 500 : 202;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ ok: !failed }));
          }, 200);
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), controlPlugin()],
  server: {
    port: 3201,
    proxy: {
      "/api": {
        target: RUNTIME,
        changeOrigin: true,
        // Forward WebSocket upgrades too (the /api/terminal/ws PTY bridge).
        ws: true,
        // SSE endpoints (/api/chat/stream, /api/stream) must not be buffered.
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => proxyReq.setHeader("Accept-Encoding", "identity"));
        },
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
