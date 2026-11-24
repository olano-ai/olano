"""The page an admin drives the plain browser test from (browser/plain_check.py).

Served by the runtime itself, not the dashboard: it is a debugging tool for
one question, and it has to work on a box running a released dashboard.
"""

from __future__ import annotations

import html
import json

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plain browser test</title>
<style>
:root { color-scheme: light dark; --fg: #1d1d1f; --bg: #fafafa; --muted: #6e6e73;
  --line: #d2d2d7; --accent: #0a66d8; --ok: #1b7f3b; --bad: #b3261e; }
@media (prefers-color-scheme: dark) { :root { --fg: #f5f5f7; --bg: #161618; --muted: #a1a1a6;
  --line: #3a3a3c; --accent: #4c9bff; --ok: #4cc36b; --bad: #ff6b60; } }
body { margin: 0; padding: 16px; font: 14px/1.45 system-ui, sans-serif; color: var(--fg);
  background: var(--bg); }
h1 { font-size: 18px; margin: 0 0 4px; }
p { margin: 4px 0 12px; color: var(--muted); max-width: 60em; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
input, select, button { font: inherit; padding: 6px 10px; border: 1px solid var(--line);
  border-radius: 8px; background: transparent; color: var(--fg); }
input[type=url] { flex: 1 1 220px; min-width: 0; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
#status { white-space: pre-wrap; font: 12px/1.5 ui-monospace, monospace; padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 8px; margin-top: 10px; }
#verdict { font-weight: 600; }
#verdict.ok { color: var(--ok); } #verdict.bad { color: var(--bad); }
#screen { display: block; width: 100%; max-width: 1280px; height: auto; border: 1px solid var(--line);
  border-radius: 6px; background: #000; touch-action: pinch-zoom; outline: none; }
#screen:focus { border-color: var(--accent); }
.hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
[hidden] { display: none !important; }
</style></head><body>
<h1>Plain browser test: __AGENT__</h1>
<p>Opens the page in a separate browser beside this agent's, on the same machine and exit,
then shows you that browser's screen. Pass the check yourself, then read the verdict.
<b>Plain</b>: no automation connection at all, your clicks come from the screen.
<b>Attached</b>: the same, with an automation connection on the tab, as the agent has.
<b>DevTools input</b>: attached, and your clicks are injected through that connection, as the
live view sends them today. <b>Agent-like context</b>: attached, in a separate context as the
agent's page is, with the parts of the agent's setup you tick.</p>
<div class="row">
  <select id="variant">
    <option value="plain">Plain (no automation)</option>
    <option value="attached">Attached (automation connected)</option>
    <option value="devtools-input">DevTools input (as the live view)</option>
    <option value="context">Agent-like context (pick its parts)</option>
  </select>
  <input id="url" type="url" value="https://www.propertyguru.com.sg/">
  <input id="then" type="url" placeholder="Then open (optional, 20 s later)">
  <button id="start" class="primary">Start</button>
  <button id="stop">Stop</button>
</div>
<canvas id="screen" width="1280" height="800" tabindex="0"></canvas>
<div class="hint">Click the screen before typing: keys go to the browser while it has focus.
On a phone you can pinch to zoom the page in to reach the box.</div>
<div class="row" id="parts" hidden>
  <label><input type="checkbox" value="viewport"> Window size</label>
  <label><input type="checkbox" value="locale"> Language</label>
  <label><input type="checkbox" value="timezone"> Time zone</label>
  <label><input type="checkbox" value="color_scheme"> Light theme</label>
  <label><input type="checkbox" value="service_workers"> No service workers</label>
  <label><input type="checkbox" value="state"> Saved sign-ins</label>
  <label><input type="checkbox" value="route_guard"> Request guard</label>
  <label><input type="checkbox" value="route_requests"> Request guard: HTTP only</label>
  <label><input type="checkbox" value="route_websockets"> Old WebSocket routing (binding)</label>
  <label><input type="checkbox" value="egress_proxy"> Egress proxy</label>
  <label><input type="checkbox" value="page_guard"> Redirect guard</label>
  <label><input type="checkbox" value="passkeys"> Passkey refusal</label>
  <label><input type="checkbox" value="copy_hook"> Copy hook</label>
  <label><input type="checkbox" value="copy_binding"> Old copy binding</label>
  <label><input type="checkbox" value="copy_script"> Copy hook: script only</label>
  <label><input type="checkbox" value="permission_hook"> Permission hook</label>
  <label><input type="checkbox" value="permission_binding"> Old permission binding</label>
  <label><input type="checkbox" value="permission_script"> Permission hook: script only</label>
  <label><input type="checkbox" value="probe_binding"> An unused binding</label>
  <label><input type="checkbox" value="page_events"> Dialog and file listeners</label>
</div>
<div id="status">Not running.</div>
<script>
const AGENT = __AGENT_JSON__;
const base = "/api/agents/" + encodeURIComponent(AGENT) + "/browser/plain-check";
const $ = (id) => document.getElementById(id);
const canvas = $("screen"), ctx = canvas.getContext("2d");
let ws = null, running = false;

async function api(method, body) {
  const res = await fetch(base, { method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function verdict(page) {
  if (!page) return ["", "The tab the test opened is closed."];
  if (/just a moment|attention required|verify|security/i.test(page.title))
    return ["bad", "Check showing (" + page.title + ")"];
  if (!page.title || page.title === page.url) return ["", "Loading..."];
  return ["ok", "Past the check: " + page.title];
}

async function refresh() {
  try {
    const s = await api("GET");
    running = true;
    const [cls, text] = verdict(s.page);
    const lines = [
      "Variant: " + s.variant + (s.attached ? " (automation attached)" : "") +
        (s.parts && s.parts.length ? " + " + s.parts.join(", ") : ""),
      "Exit: " + (s.exit ? s.exit.ip + " " + s.exit.colo : s.exit_error || "?"),
      "Started: " + new Date(s.started_at * 1000).toLocaleTimeString(),
      ...(s.then_url ? ["Second page: " + s.then_url + " (" + s.then_state + ")",
        "First page was: " + (s.first_page ? s.first_page.title : "(still on it)")] : []),
      "Tabs:",
      ...(s.tabs || []).map((t) => "  " + (t.title || "(untitled)") + "  " + t.url),
    ];
    $("status").innerHTML = '<span id="verdict" class="' + cls + '"></span>\\n';
    $("verdict").textContent = text;
    $("status").appendChild(document.createTextNode(lines.join("\\n")));
    if (!ws) connect();
  } catch (e) {
    running = false;
    $("status").textContent = "Not running." + (e.message.includes("No plain") ? "" : " " + e.message);
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + base + "/screen");
  ws.binaryType = "blob";
  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      if (msg.type === "error") $("status").append("\\nInput: " + msg.message);
      return;
    }
    const bmp = await createImageBitmap(ev.data);
    if (canvas.width !== bmp.width) { canvas.width = bmp.width; canvas.height = bmp.height; }
    ctx.drawImage(bmp, 0, 0);
  };
  ws.onclose = () => { ws = null; };
}

function send(ev) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(ev)); }
function pos(e) {
  const r = canvas.getBoundingClientRect();
  return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
}
const BUTTONS = ["left", "middle", "right"];
canvas.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse") send({ t: "move", ...pos(e) }); });
canvas.addEventListener("pointerdown", (e) => {
  // Measured before focusing: a focus that scrolled would move the screen
  // under the pointer.
  const at = pos(e);
  canvas.focus({ preventScroll: true }); e.preventDefault();
  send({ t: "move", ...at });
  send({ t: "down", button: BUTTONS[e.button] || "left", ...at });
});
canvas.addEventListener("pointerup", (e) => { send({ t: "up", button: BUTTONS[e.button] || "left", ...pos(e) }); });
canvas.addEventListener("contextmenu", (e) => e.preventDefault());
canvas.addEventListener("wheel", (e) => { e.preventDefault(); send({ t: "wheel", dx: e.deltaX, dy: e.deltaY }); }, { passive: false });
canvas.addEventListener("keydown", (e) => { e.preventDefault(); send({ t: "keydown", key: e.key }); });
canvas.addEventListener("keyup", (e) => { e.preventDefault(); send({ t: "keyup", key: e.key }); });
canvas.addEventListener("paste", (e) => { send({ t: "text", text: e.clipboardData.getData("text") }); });

$("start").onclick = async () => {
  $("status").textContent = "Starting a fresh browser (about 10 seconds)...";
  if (ws) { ws.close(); ws = null; }
  try {
    const parts = [...document.querySelectorAll("#parts input:checked")].map((i) => i.value);
    await api("POST", { action: "start", url: $("url").value, variant: $("variant").value, parts,
      then_url: $("then").value });
  } catch (e) { $("status").textContent = "Could not start: " + e.message; return; }
  refresh();
};
$("stop").onclick = async () => { if (ws) ws.close(); await api("POST", { action: "stop" }).catch(() => {}); refresh(); };
$("variant").onchange = () => { $("parts").hidden = $("variant").value !== "context"; };
refresh();
setInterval(refresh, 3000);
</script></body></html>
"""


def page_html(agent: str) -> str:
    return _PAGE.replace("__AGENT_JSON__", json.dumps(agent).replace("<", "\\u003c")).replace(
        "__AGENT__", html.escape(agent)
    )
