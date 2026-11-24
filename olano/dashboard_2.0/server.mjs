/**
 * Olano Dashboard — Node.js server
 *
 * Serves the SPA frontend and proxies all /api/* requests to the
 * Python runtime's FastAPI server.
 *
 * Environment variables:
 *   LIONCLAW_RUNTIME_URL    — Python runtime base URL (default: http://localhost:8765)
 *   LIONCLAW_DASHBOARD_PORT — Port to serve dashboard on (default: 3200)
 *   LIONCLAW_DASHBOARD_TOKEN — Optional Bearer token for API auth
 */

import http from 'http';
import https from 'https';
import net from 'net';
import fs from 'fs';
import path from 'path';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const RUNTIME_URL = process.env.LIONCLAW_RUNTIME_URL || 'http://localhost:8765';
const DASHBOARD_PORT = parseInt(process.env.LIONCLAW_DASHBOARD_PORT || '3200', 10);
const AUTH_TOKEN = process.env.LIONCLAW_DASHBOARD_TOKEN || '';

const PUBLIC_DIR = process.env.LIONCLAW_DASHBOARD_PUBLIC_DIR || path.join(__dirname, 'public');

// ── MIME types ─────────────────────────────────────────────────────────────────

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.json': 'application/json',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

// ── Proxy helper ───────────────────────────────────────────────────────────────

function proxyRequest(req, res) {
  const targetUrl = new URL(RUNTIME_URL + req.url);
  const isHttps = targetUrl.protocol === 'https:';
  const lib = isHttps ? https : http;

  const options = {
    hostname: targetUrl.hostname,
    port: targetUrl.port || (isHttps ? 443 : 80),
    path: targetUrl.pathname + targetUrl.search,
    method: req.method,
    headers: {
      ...req.headers,
      host: targetUrl.host,
    },
  };

  if (AUTH_TOKEN) {
    options.headers['Authorization'] = `Bearer ${AUTH_TOKEN}`;
  }

  const proxyReq = lib.request(options, (proxyRes) => {
    // SSE: flush immediately
    const isSSE = proxyRes.headers['content-type']?.includes('text/event-stream');

    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res, { end: true });

    if (isSSE) {
      proxyRes.on('data', () => {
        if (res.flushHeaders) res.flushHeaders();
      });
    }
  });

  proxyReq.on('error', (err) => {
    console.error('[proxy error]', err.message);
    if (!res.headersSent) {
      res.writeHead(502);
      res.end(JSON.stringify({ error: 'Runtime unavailable', detail: err.message }));
    }
  });

  req.pipe(proxyReq, { end: true });
}

// ── Static file server ─────────────────────────────────────────────────────────

// Vite emits content-hashed bundles under /assets/ (<name>-<hash>.js), so their
// name changes whenever the bytes do — cache them permanently instead of making
// the browser re-validate a dozen chunks on every navigation. theme1/ is
// hand-managed and NOT hashed, and the HTML shell must be re-fetched or a
// deploy's new asset hashes are never picked up.
function cacheControlFor(urlPath) {
  if (urlPath.startsWith('/assets/theme1/')) return 'public, max-age=300, must-revalidate';
  if (urlPath.startsWith('/assets/')) return 'public, max-age=31536000, immutable';
  return 'no-cache';
}

function serveStatic(req, res) {
  let urlPath = req.url.split('?')[0];
  if (urlPath === '/' || !urlPath.includes('.')) {
    urlPath = '/index.html';
  }

  const filePath = path.join(PUBLIC_DIR, urlPath);

  // Security: prevent directory traversal
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      // A missing build asset is a 404, never the HTML shell. /assets/ holds
      // content-hashed chunks, so a request for one that isn't on disk means
      // the caller is running a build we no longer serve (a tab left open
      // across a deploy). Answering it with index.html hands the browser HTML
      // where a module was expected — an opaque "Failed to fetch dynamically
      // imported module" — and risks that HTML being cached under the .js URL
      // by the immutable Cache-Control above. The dashboard recovers on its
      // own (src/core/chunkRecovery.ts) once the failure is unambiguous.
      if (urlPath.startsWith('/assets/')) {
        res.writeHead(404, {
          'Content-Type': 'text/plain; charset=utf-8',
          'Cache-Control': 'no-store',
        });
        res.end('Asset not found — this build is no longer deployed. Reload the page.');
        return;
      }
      // SPA fallback
      fs.readFile(path.join(PUBLIC_DIR, 'index.html'), (err2, data2) => {
        if (err2) {
          res.writeHead(404);
          res.end('Not found');
          return;
        }
        res.writeHead(200, {
          'Content-Type': 'text/html; charset=utf-8',
          'Cache-Control': 'no-cache',
        });
        res.end(data2);
      });
      return;
    }

    const ext = path.extname(filePath);
    const mimeType = MIME[ext] || 'application/octet-stream';
    res.writeHead(200, {
      'Content-Type': mimeType,
      'Cache-Control': cacheControlFor(urlPath),
    });
    res.end(data);
  });
}

// ── Local control plane (restart the runtime when it's down) ─────────────────
//
// This dashboard process is independent of the Python runtime (:8765), so it
// stays up when the runtime is stopped/restarted. The dashboard UI's
// connectivity overlay calls these to offer a "Restart server now" button — the
// browser hits the dashboard's OWN origin (not the dead /api proxy). Restricted
// to localhost: it's a local control plane, not a remote-exposed surface.

function isLocalRequest(req) {
  const addr = req.socket?.remoteAddress || '';
  return addr === '127.0.0.1' || addr === '::1' || addr === '::ffff:127.0.0.1';
}

function handleControl(req, res) {
  if (!isLocalRequest(req)) {
    res.writeHead(403, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'forbidden' }));
    return;
  }
  if (req.url === '/__control/ping' && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true }));
    return;
  }
  if (req.url === '/__control/restart' && req.method === 'POST') {
    // `start` is idempotent — boots the runtime if it's down, no-ops if up.
    let failed = false;
    try {
      const child = spawn('olano', ['server', 'start'], { detached: true, stdio: 'ignore' });
      child.on('error', () => { failed = true; });
      child.unref();
    } catch {
      failed = true;
    }
    // Defer the reply briefly so an immediate ENOENT (olano not on PATH) is
    // reported as a failure rather than a false success.
    setTimeout(() => {
      res.writeHead(failed ? 500 : 202, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: !failed }));
    }, 200);
    return;
  }
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ ok: false, error: 'not_found' }));
}

// ── HTTP server ────────────────────────────────────────────────────────────────

const server = http.createServer((req, res) => {
  // CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    });
    res.end();
    return;
  }

  res.setHeader('Access-Control-Allow-Origin', '*');

  // Local control plane (restart) — served by THIS process, never proxied.
  if (req.url.startsWith('/__control/')) {
    handleControl(req, res);
    return;
  }

  // Proxy API and SSE requests to the Python runtime
  if (req.url.startsWith('/api/')) {
    proxyRequest(req, res);
    return;
  }

  // Serve static dashboard files
  serveStatic(req, res);
});

// ── WebSocket proxy (terminal) ──────────────────────────────────────────────
//
// http.createServer's request handler never sees WS upgrades — they arrive as
// an 'upgrade' event we must tunnel manually. We open a raw TCP connection to
// the runtime, replay the upgrade request line + headers (injecting the bearer
// token like proxyRequest does), then pipe the two sockets together. Only /api/
// paths are proxied; anything else is rejected.

server.on('upgrade', (req, clientSocket, head) => {
  if (!req.url.startsWith('/api/')) {
    clientSocket.destroy();
    return;
  }
  const targetUrl = new URL(RUNTIME_URL);
  if (targetUrl.protocol === 'https:') {
    // wss upstream would need a TLS socket; the runtime is plain http today.
    console.error('[ws proxy] https runtime upstream not supported');
    clientSocket.destroy();
    return;
  }

  const headers = { ...req.headers, host: targetUrl.host };
  if (AUTH_TOKEN) headers['authorization'] = `Bearer ${AUTH_TOKEN}`;

  const upstream = net.connect(
    targetUrl.port || 80,
    targetUrl.hostname,
    () => {
      let lines = `GET ${req.url} HTTP/1.1\r\n`;
      for (const [k, v] of Object.entries(headers)) {
        const vals = Array.isArray(v) ? v : [v];
        for (const one of vals) lines += `${k}: ${one}\r\n`;
      }
      lines += '\r\n';
      upstream.write(lines);
      if (head && head.length) upstream.write(head);
      upstream.pipe(clientSocket);
      clientSocket.pipe(upstream);
    },
  );

  upstream.on('error', (err) => {
    console.error('[ws proxy error]', err.message);
    clientSocket.destroy();
  });
  clientSocket.on('error', () => upstream.destroy());
});

server.listen(DASHBOARD_PORT, () => {
  console.log(`\n🔶 Olano`);
  console.log(`   Dashboard: http://localhost:${DASHBOARD_PORT}`);
  console.log(`   Runtime:   ${RUNTIME_URL}`);
  if (AUTH_TOKEN) {
    console.log(`   Auth:      token protected`);
  }
  console.log('');
});

server.on('error', (err) => {
  console.error('Dashboard server error:', err);
  process.exit(1);
});
