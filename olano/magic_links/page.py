"""The magic-link page — one self-contained HTML document.

Server-rendered, no build step, no external assets: inline CSS and inline JS
only, so it works under the strict CSP the route sets (``default-src 'none'``)
and needs nothing from the dashboard's SPA bundle. This is the grown-up form of
the inline OAuth-callback cards in ``mcp_setup._OAUTH_CALLBACK_HTML`` and
``api.auth_service_callback``.

The page renders whatever the flow **descriptor** says (see
:mod:`olano.magic_links.flows`), so a new service never touches this file. It
is deliberately dumb: it polls ``/state``, POSTs to ``/unlock`` and ``/action``,
and draws a few shapes — a device code, a hand-off button, a form, a QR, and
the MCP credentials page (an OAuth-app form with the redirect URL to register
and the Composio alternative, before the same hand-off).

Design constraints worth keeping:

* **mobile first** — the reader is on a phone, one-handed, in a chat app;
* **no provider content is embedded** — we link out, never frame or proxy;
* **nothing sensitive before the PIN** — the locked shell shows only who asked,
  from where, and how long ago;
* **no secret ever comes back down** — form values travel up only.
"""

from __future__ import annotations

import html
import json
from typing import Any


def _script_json(obj: Any) -> str:
    """JSON safe to drop inside an inline ``<script>``.

    ``json.dumps`` does not escape ``<``/``>``/``&``, so a state field carrying
    a literal ``</script>`` (e.g. a provider-supplied ``error_description`` shown
    on a terminal-error page) would otherwise close the script element and let
    the rest execute. Escaping these — plus the two Unicode line terminators JS
    treats as newlines — closes that hole even under the page's already-strict
    CSP. Self-XSS at worst (the victim holds their own token + PIN), but a
    magic-link surface should not hand out a script-injection primitive at all.
    """
    return (
        json.dumps(obj, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_STYLE = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
padding:20px;font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0b0d12;color:#e8eaed;-webkit-font-smoothing:antialiased}
.card{width:100%;max-width:420px;background:#13161d;border:1px solid rgba(255,255,255,.08);
border-radius:16px;padding:28px 24px;box-shadow:0 18px 48px rgba(0,0,0,.45)}
.brand{display:flex;align-items:center;gap:9px;font-size:13px;letter-spacing:.14em;
text-transform:uppercase;color:#8b93a3;margin:0 0 20px}
.brand b{color:#e8eaed;font-weight:600;letter-spacing:normal;text-transform:none;font-size:15px}
.mark{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,#f4785f,#e0457b);
flex:none}
h1{font-size:20px;line-height:1.3;margin:0 0 8px;font-weight:650}
p{margin:0 0 14px;color:#a9b0bd}
p.lead{color:#c6ccd8}
.meta{font-size:13px;color:#8b93a3;background:#0e1116;border:1px solid rgba(255,255,255,.06);
border-radius:10px;padding:11px 13px;margin:0 0 18px}
.meta b{color:#c6ccd8;font-weight:600}
label{display:block;font-size:13px;color:#c6ccd8;margin:0 0 6px;font-weight:550}
.hint{font-size:12px;color:#767e8d;margin:-8px 0 14px}
input,select,textarea{width:100%;padding:12px 13px;font:inherit;color:#e8eaed;background:#0e1116;
border:1px solid rgba(255,255,255,.12);border-radius:10px;margin:0 0 14px;outline:none}
input:focus,select:focus,textarea:focus{border-color:#f4785f;box-shadow:0 0 0 3px rgba(244,120,95,.16)}
textarea{min-height:220px;resize:vertical;line-height:1.5}
input.pin{font-size:26px;letter-spacing:.34em;text-align:center;font-variant-numeric:tabular-nums}
button{width:100%;padding:13px 16px;font:inherit;font-weight:600;color:#fff;cursor:pointer;
background:linear-gradient(135deg,#f4785f,#e0457b);border:0;border-radius:10px}
button:disabled{opacity:.55;cursor:default}
button.ghost{background:none;color:#a9b0bd;border:1px solid rgba(255,255,255,.14);margin-top:10px}
a.cta{display:block;text-align:center;padding:13px 16px;font-weight:600;color:#fff;
text-decoration:none;background:linear-gradient(135deg,#f4785f,#e0457b);border-radius:10px}
.code{display:flex;align-items:center;justify-content:center;gap:10px;margin:0 0 16px;
padding:16px;background:#0e1116;border:1px dashed rgba(255,255,255,.18);border-radius:12px;
font-size:29px;font-weight:700;letter-spacing:.12em;font-variant-numeric:tabular-nums;
word-break:break-all}
.copy{width:auto;padding:7px 11px;font-size:12px;background:rgba(255,255,255,.08);color:#c6ccd8}
.err{color:#ff8f8f;font-size:13.5px;margin:0 0 14px;min-height:1.2em}
.status{display:flex;align-items:center;gap:9px;font-size:14px;color:#a9b0bd;margin:16px 0 0}
.dot{width:9px;height:9px;border-radius:50%;background:#f0b429;flex:none;
animation:pulse 1.6s ease-in-out infinite}
.dot.ok{background:#48d389;animation:none}.dot.bad{background:#f46363;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.qr{display:block;margin:0 auto 16px;image-rendering:pixelated;width:210px;height:210px;
background:#fff;padding:9px;border-radius:12px}
button.plat{display:block;background:#0e1116;border:1px solid rgba(255,255,255,.14);
color:#e8eaed;text-align:left;margin:0 0 10px}
button.plat.done{opacity:.65;cursor:default}
button.plat .st{float:right;color:#48d389;font-weight:700}
.foot{margin:20px 0 0;font-size:12px;color:#6d7481;text-align:center}
.warn{font-size:13.5px;color:#ffc46b;background:rgba(240,180,41,.10);border:1px solid rgba(240,180,41,.35);
border-radius:10px;padding:10px 12px;margin:0 0 14px}
.guide{font-size:13.5px;color:#c6ccd8;background:#0e1116;border:1px solid rgba(255,255,255,.06);
border-radius:12px;padding:13px 14px;margin:0 0 18px}
.guide b{display:block;color:#e8eaed;margin:0 0 6px}
.steps{margin:0;padding:0 0 0 18px}
.steps li{margin:0 0 8px}
.steps li:last-child{margin:0}
.code.sm{font-size:12.5px;font-weight:500;letter-spacing:0;padding:9px 10px;margin:8px 0 2px;
justify-content:space-between;text-align:left;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.code.sm span{flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.code .copy{flex:none;white-space:nowrap}
a.link{color:#f4a08c;text-decoration:none;font-weight:550}
a.link:hover{text-decoration:underline}
details.alt{margin:18px 0 0;padding:12px 0 0;border-top:1px solid rgba(255,255,255,.08)}
details.alt summary{cursor:pointer;font-size:12.5px;color:#8b93a3;user-select:none}
details.alt summary:hover{color:#c6ccd8}
details.alt[open] summary{color:#c6ccd8;margin:0 0 10px}
details.alt p{font-size:13px;margin:0 0 10px}
.small{font-size:12px;color:#767e8d}
@media(prefers-color-scheme:light){
body{background:#f4f5f8;color:#171a21}
.card{background:#fff;border-color:rgba(0,0,0,.08);box-shadow:0 14px 40px rgba(20,24,35,.10)}
h1{color:#171a21}p{color:#5a6270}p.lead{color:#3d444f}
.meta,input,select,textarea,.code{background:#f7f8fa;border-color:rgba(0,0,0,.10);color:#171a21}
.meta,.brand{color:#6b7280}.meta b,.brand b,label{color:#333a45}
input,select,textarea{color:#171a21}.foot{color:#8b919c}
/* The dark palette's translucent-white chip vanishes on a light card. */
.copy{background:rgba(0,0,0,.06);color:#3d444f}
button.ghost{color:#5a6270;border-color:rgba(0,0,0,.16)}
button.plat{background:#f7f8fa;border-color:rgba(0,0,0,.12);color:#171a21}
.status{color:#5a6270}.hint{color:#6b7280}
.warn{color:#8a5a00;background:rgba(240,180,41,.14);border-color:rgba(240,180,41,.5)}
.guide{background:#f7f8fa;border-color:rgba(0,0,0,.08);color:#3d444f}.guide b{color:#171a21}
a.link{color:#d9445f}
details.alt{border-color:rgba(0,0,0,.08)}details.alt summary{color:#6b7280}
details.alt summary:hover,details.alt[open] summary{color:#3d444f}.small{color:#6b7280}
}
"""

# The whole client. Kept small and dependency-free on purpose: it must run under
# `default-src 'none'` with only an inline-script allowance, on an old phone
# browser, with no bundler in the loop.
_SCRIPT = r"""
var S = window.__HS__, busy = false;
var base = S.base;
S.copies = [];

function el(id){ return document.getElementById(id); }
function esc(s){ var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

function post(path, body){
  return fetch(base + path, {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  }).then(function(r){ return r.json().then(function(j){ return {ok: r.ok, body: j}; }); });
}

function unlock(){
  if (busy) return;
  var pin = el('pin').value.trim();
  if (!pin) return;
  busy = true; el('go').disabled = true; el('err').textContent = '';
  post('/unlock', {pin: pin}).then(function(res){
    busy = false; el('go').disabled = false;
    if (res.ok && res.body.unlocked) { render(res.body.state); poll(); }
    else { el('err').textContent = res.body.detail || 'Incorrect PIN.'; el('pin').value = ''; el('pin').focus(); }
  }).catch(function(){ busy = false; el('go').disabled = false; el('err').textContent = 'Network error — try again.'; });
}

function act(op, fields){
  if (busy) return;
  busy = true;
  var btn = el('act'); if (btn) btn.disabled = true;
  post('/action', {op: op, fields: fields || {}}).then(function(res){
    busy = false;
    if (res.ok) { render(res.body); }
    else { var e = el('err'); if (e) e.textContent = res.body.detail || 'Something went wrong.'; var b = el('act'); if (b) b.disabled = false; }
  }).catch(function(){ busy = false; var b = el('act'); if (b) b.disabled = false; });
}

function submitForm(ev){
  if (ev) ev.preventDefault();
  var out = {}, inputs = document.querySelectorAll('[data-field]');
  for (var i = 0; i < inputs.length; i++) out[inputs[i].getAttribute('data-field')] = inputs[i].value;
  act('submit', out);
}

function copyText(text, btn){
  var done = function(){ if (btn) { btn.textContent = 'Copied'; setTimeout(function(){ btn.textContent = 'Copy'; }, 1600); } };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function(){ legacyCopy(text, done); });
  } else { legacyCopy(text, done); }
}
// Older phone browsers (and any page not in a secure context) have no
// navigator.clipboard; a selected off-screen textarea + execCommand still works.
function legacyCopy(text, done){
  try {
    var ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '-1000px';
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    var ok = document.execCommand('copy'); document.body.removeChild(ta);
    if (ok) done();
  } catch (e) {}
}
function copyCode(code){ copyText(code, el('copy')); }
// Texts behind the render's [data-copy] buttons, by index — kept out of the
// markup so a value with quotes or angle brackets can never break an attribute.
function copyIdx(text){ S.copies.push(text); return S.copies.length - 1; }

function fieldHtml(f){
  var h = '<label for="f_' + esc(f.name) + '">' + esc(f.label) + '</label>';
  if (f.options && f.options.length){
    h += '<select id="f_' + esc(f.name) + '" data-field="' + esc(f.name) + '">';
    f.options.forEach(function(o){ h += '<option value="' + esc(o.value) + '">' + esc(o.label) + '</option>'; });
    h += '</select>';
  } else if (f.multiline){
    h += '<textarea id="f_' + esc(f.name) + '" data-field="' + esc(f.name) + '"' +
         ' spellcheck="false" placeholder="' + esc(f.placeholder) + '">' + esc(f.value || '') + '</textarea>';
  } else {
    h += '<input id="f_' + esc(f.name) + '" data-field="' + esc(f.name) + '"' +
         (f.secret ? ' type="password" autocomplete="off" spellcheck="false"' :
          (f.name === 'email' ? ' type="email" autocomplete="email" spellcheck="false"' : ' type="text" autocomplete="off" spellcheck="false"')) +
         ' placeholder="' + esc(f.placeholder) + '"' +
         (f.value && !f.secret ? ' value="' + esc(f.value) + '"' : '') + '>';
  }
  if (f.help) h += '<div class="hint">' + esc(f.help) + '</div>';
  return h;
}

// The credentials phase of an MCP sign-in: where to create the OAuth app, the
// redirect URL it must carry, the two fields, and the Composio alternative.
function mcpCredentials(s){
  var name = (s.service && s.service.name) || 'the provider';
  var g = s.guide || {}, h = '';
  if (s.retry_error) h += '<div class="warn">' + esc(name) + ' said: ' + esc(s.retry_error) + '</div>';
  h += '<div class="guide"><b>Where to get them</b><ol class="steps">';
  h += '<li>Open ' + esc(name) + (g.console_label ? ' → ' + esc(g.console_label) : "'s developer settings");
  if (g.console_url) h += ' — <a class="link" href="' + esc(g.console_url) + '" target="_blank" rel="noopener noreferrer">open ' + esc(name) + '</a>';
  h += '.';
  if (g.notes) h += '<div class="hint" style="margin:4px 0 0">' + esc(g.notes) + '</div>';
  h += '</li>';
  h += '<li>Create the OAuth app there and register this redirect URL on it:';
  if (s.redirect_uri){
    h += '<div class="code sm"><span>' + esc(s.redirect_uri) + '</span>' +
         '<button class="copy" type="button" data-copy="' + copyIdx(s.redirect_uri) + '">Copy</button></div>';
  } else {
    h += '<div class="hint">No public address is configured on this deployment yet — ask the operator, then use a fresh link.</div>';
  }
  h += '</li>';
  h += '<li>Copy the Client ID and Client secret it shows you into the fields below.</li></ol>';
  if (g.docs_url) h += '<p class="small" style="margin:10px 0 0"><a class="link" href="' + esc(g.docs_url) + '" target="_blank" rel="noopener noreferrer">Read ' + esc(name) + "'s guide" + '</a></p>';
  h += '</div>';
  h += '<form id="form">';
  (s.fields || []).forEach(function(f){ h += fieldHtml(f); });
  h += '<button type="submit" id="act">' + esc(s.cta || 'Save and continue') + '</button></form>';
  var c = s.composio;
  if (c){
    var calls = c.free_calls_per_month ? Number(c.free_calls_per_month).toLocaleString() : '';
    h += '<details class="alt"><summary>Or try Composio for free to connect to hundreds of tools</summary>';
    h += '<p>Composio is a service that connects agents to hundreds of platforms, ' + esc(name) +
         ' included, through one sign-in — no OAuth app of your own. It is free to start' +
         (calls ? ' (up to ' + esc(calls) + ' tool calls a month)' : '') + '.</p>';
    if (c.connected){
      h += '<p>' + esc(c.agent || 'Your agent') + ' already has Composio connected. Copy this message and send it to ' + esc(c.agent || 'your agent') + ':</p>';
    } else {
      h += '<p>1. Create a free Composio account: <a class="link" href="' + esc(c.signup_url) + '" target="_blank" rel="noopener noreferrer">' + esc(hostOnly(c.signup_url)) + '</a></p>';
      h += '<p>2. Copy this message and send it to ' + esc(c.agent || 'your agent') + ':</p>';
    }
    h += '<div class="code sm"><span>' + esc(c.message) + '</span>' +
         '<button class="copy" type="button" data-copy="' + copyIdx(c.message) + '">Copy</button></div>';
    h += '</details>';
  }
  return h;
}
function hostOnly(url){ try { return new URL(url).host; } catch (e) { return url; } }

function terminal(st){ return st === 'done' || st === 'error' || st === 'expired' || st === 'revoked'; }

function render(s){
  if (!s) return;
  // Re-rendering identical state on every poll tick would wipe focus and any
  // half-typed form value; only redraw when something actually changed.
  var key = JSON.stringify(s);
  if (key === S.lastKey) return;
  S.lastKey = key;
  S.state = s;
  S.copies = [];
  // owner-login success hands back a redirect — sign-in is done, go to the app.
  if (s.redirect){ window.location.assign(s.redirect); return; }
  var h = '';
  if (s.status === 'done'){
    // A one-time secret (e.g. a freshly minted access token) arrives only on
    // the submit response; keep it so a later poll cannot wipe it off screen.
    if (s.reveal) S.reveal = s.reveal;
    h += '<h1>All set</h1><p class="lead">' + esc(s.result || 'You can close this tab and go back to the chat.') + '</p>';
    if (S.reveal){
      h += '<p class="hint">Copy it now — it is shown only here, only once, and cannot be recovered later.</p>';
      h += '<div class="code"><span id="reveal">' + esc(S.reveal) + '</span>' +
           '<button class="copy" id="copyreveal" type="button">Copy</button></div>';
    }
    h += '<div class="status"><span class="dot ok"></span>Finished — nothing else to do here.</div>';
    el('body').innerHTML = h;
    var cr = el('copyreveal');
    if (cr) cr.onclick = function(){
      if (navigator.clipboard) navigator.clipboard.writeText(S.reveal).then(function(){ cr.textContent = 'Copied'; });
    };
    return;
  }
  if (terminal(s.status)){
    h += '<h1>This link is no longer usable</h1><p>' + esc(s.error || 'It expired or was already used.') + '</p>';
    h += '<p>Ask your agent for a fresh link.</p>';
    el('body').innerHTML = h; return;
  }

  h += '<h1>' + esc(s.title || 'Connect') + '</h1>';
  if (s.instructions) h += '<p class="lead">' + esc(s.instructions) + '</p>';
  h += '<div class="err" id="err"></div>';

  if (s.kind === 'device_code' && s.user_code){
    h += '<div class="code"><span>' + esc(s.user_code) + '</span>' +
         '<button class="copy" id="copy" type="button">Copy</button></div>';
    h += '<a class="cta" id="cta" href="' + esc(s.verification_url) + '" target="_blank" rel="noopener noreferrer">Continue to ' + esc((s.service && s.service.name) || 'the provider') + '</a>';
  } else if (s.kind === 'redirect' && s.authorization_url){
    h += '<a class="cta" id="cta" href="' + esc(s.authorization_url) + '" target="_blank" rel="noopener noreferrer">Continue to ' + esc((s.service && s.service.name) || 'the provider') + '</a>';
  } else if (s.kind === 'qr' && s.qr_data){
    if (s.qr_image) h += '<img class="qr" alt="Scan this code" src="' + esc(s.qr_image) + '">';
    h += '<div class="code"><span>' + esc(s.qr_data) + '</span>' +
         '<button class="copy" id="copy" type="button">Copy</button></div>';
    h += '<p class="hint">Scan the code with the app on your phone' +
         (s.qr_image ? '.' : ', or paste the value above into it.') + '</p>';
  } else if (s.kind === 'connect'){
    var a = s.active;
    if (a){
      if (a.instructions) h += '<p class="lead">' + esc(a.instructions) + '</p>';
      if (a.mode === 'token'){
        h += '<form id="form">';
        (a.fields || []).forEach(function(f){
          h += '<label for="f_' + esc(f.name) + '">' + esc(f.label) + '</label>';
          h += '<input id="f_' + esc(f.name) + '" data-field="' + esc(f.name) + '"' +
               (f.secret ? ' type="password" autocomplete="off" spellcheck="false"' : ' type="text"') +
               ' placeholder="' + esc(f.placeholder) + '">';
          if (f.help) h += '<div class="hint">' + esc(f.help) + '</div>';
        });
        h += '<button type="submit" id="act">' + esc(a.cta || 'Connect') + '</button></form>';
      } else {
        if (a.qr_image) h += '<img class="qr" alt="Scan this code" src="' + esc(a.qr_image) + '">';
        else if (a.qr_data) h += '<div class="code"><span>' + esc(a.qr_data) + '</span></div>';
        h += '<div class="status"><span class="dot' + (a.status === 'error' ? ' bad' : '') + '"></span>' +
             esc(a.detail || 'Preparing…') + '</div>';
        if (a.pair_code){
          h += '<div class="code"><span>' + esc(a.pair_code) + '</span></div>' +
               '<p class="hint">In WhatsApp: Linked devices → Link with phone number, then enter this code.</p>';
        } else if (a.pair_code_supported){
          h += '<label for="pcnum">Only have your phone? Get a pairing code instead</label>';
          h += '<input id="pcnum" type="tel" inputmode="tel" autocomplete="tel" placeholder="Phone number incl. country code, e.g. +1 555 123 4567">';
          h += '<button class="ghost" id="pc" type="button">Get pairing code</button>';
        }
      }
      h += '<button class="ghost" id="back" type="button">Back</button>';
    } else {
      (s.platforms || []).forEach(function(p){
        var done = p.status === 'done';
        h += '<button class="plat' + (done ? ' done' : '') + '" type="button"' +
             (done ? ' disabled' : ' data-open="' + esc(p.platform) + '"') + '>' +
             esc(p.label) + (done ? '<span class="st">✓</span>' : '') + '</button>';
        if (p.detail) h += '<div class="hint">' + esc(p.detail) + '</div>';
      });
      if (s.any_completed) h += '<button id="finish" type="button">Finish setup</button>';
    }
  } else if (s.kind === 'mcp'){
    if (s.mode === 'credentials'){
      h += mcpCredentials(s);
    } else if (s.authorization_url){
      h += '<a class="cta" id="cta" href="' + esc(s.authorization_url) + '" target="_blank" rel="noopener noreferrer">Continue to ' + esc((s.service && s.service.name) || 'the provider') + '</a>';
    } else if (!s.started){
      h += '<button id="act" type="button">Start</button>';
    }
  } else if (s.kind === 'form' || (s.kind === 'session' && s.fields && s.fields.length)){
    h += '<form id="form">';
    (s.fields || []).forEach(function(f){ h += fieldHtml(f); });
    h += '<button type="submit" id="act">' + esc(s.cta || 'Save') + '</button></form>';
  } else if (s.kind === 'session'){
    // grant mode: one tap, no fields. (unavailable mode shows only its message.)
    if (s.mode !== 'unavailable') h += '<button id="act" type="button">' + esc(s.cta || 'Continue') + '</button>';
  } else if (!s.started){
    h += '<button id="act" type="button">Start</button>';
  }

  var handoff = s.kind === 'device_code' || s.kind === 'redirect' || (s.kind === 'mcp' && s.mode !== 'credentials');
  if (handoff && s.started){
    h += '<div class="status"><span class="dot"></span>Waiting for you to finish at ' +
         esc((s.service && s.service.name) || 'the provider') + '…</div>';
  }
  el('body').innerHTML = h;

  var copy = el('copy'); if (copy) copy.onclick = function(){ copyCode(s.user_code || s.qr_data); };
  var copies = document.querySelectorAll('[data-copy]');
  for (var ci = 0; ci < copies.length; ci++){
    (function(btn){ btn.onclick = function(){ copyText(S.copies[+btn.getAttribute('data-copy')] || '', btn); }; })(copies[ci]);
  }
  var form = el('form'); if (form) form.onsubmit = submitForm;
  var act_btn = el('act');
  if (act_btn && !form){
    // A standalone button (no form): owner-login grant submits; hand-off flows start.
    var op = (s.kind === 'session') ? 'submit' : 'start';
    act_btn.onclick = function(){ if (s.kind === 'session') act_btn.textContent = 'Signing in…'; act(op, {}); };
  }
  // Opening the provider IS the start for hand-off flows: fire it on the tap so
  // the session exists by the time they come back, without an extra screen.
  var cta = el('cta'); if (cta && !s.started) cta.addEventListener('click', function(){ act('start'); });

  if (s.kind === 'connect'){
    // Connect pages route every interaction through submit-with-_action; the
    // generic form handler above is overridden so credentials carry their
    // platform along.
    var opens = document.querySelectorAll('[data-open]');
    for (var k = 0; k < opens.length; k++){
      (function(btn){ btn.onclick = function(){ act('submit', {_action: 'open', platform: btn.getAttribute('data-open')}); }; })(opens[k]);
    }
    var back = el('back'); if (back) back.onclick = function(){ act('submit', {_action: 'back'}); };
    var fin = el('finish'); if (fin) fin.onclick = function(){ act('submit', {_action: 'finish'}); };
    var pc = el('pc'); if (pc) pc.onclick = function(){
      act('submit', {_action: 'paircode', platform: (s.active || {}).platform || '', number: (el('pcnum') || {}).value || ''});
    };
    var cform = el('form');
    if (cform) cform.onsubmit = function(ev){
      ev.preventDefault();
      var out = {_action: 'save', platform: (s.active || {}).platform || ''};
      var inputs = document.querySelectorAll('[data-field]');
      for (var m = 0; m < inputs.length; m++) out[inputs[m].getAttribute('data-field')] = inputs[m].value;
      act('submit', out);
    };
  }
}

var timer = null;
function poll(){
  if (timer) return;
  timer = setInterval(function(){
    if (S.state && terminal(S.state.status)) { clearInterval(timer); timer = null; return; }
    fetch(base + '/state', {credentials: 'same-origin'}).then(function(r){
      return r.ok ? r.json() : null;
    }).then(function(j){ if (j) render(j); }).catch(function(){});
  }, 2500);
}

if (S.state) { render(S.state); poll(); }
else {
  var go = el('go'); if (go) go.onclick = unlock;
  var pinForm = el('pinform'); if (pinForm) pinForm.onsubmit = function(e){ e.preventDefault(); unlock(); };
  var pin = el('pin'); if (pin) pin.focus();
}
"""


def _requester_line(requested_by: dict[str, Any]) -> str:
    """Who asked, from where — so the reader can verify this was them."""
    who = str(requested_by.get("username") or requested_by.get("user_id") or "").strip()
    channel = str(requested_by.get("channel") or "").strip()
    agent = str(requested_by.get("agent") or "").strip()
    bits: list[str] = []
    if agent:
        bits.append(f"Requested by <b>{html.escape(agent)}</b>")
    else:
        bits.append("Requested")
    if who:
        bits.append(f"for <b>{html.escape(who)}</b>")
    if channel:
        bits.append(f"via <b>{html.escape(channel)}</b>")
    return " ".join(bits)


def render_page(
    *,
    base_path: str,
    requested_by: dict[str, Any],
    state: dict[str, Any] | None,
    locked: bool,
) -> str:
    """The full HTML document.

    ``state`` is ``None`` while the link is locked — the shell then shows only
    the PIN prompt and the requester line, and no descriptor field (service
    name, device code, form) has been sent to the browser yet.
    """
    boot = _script_json({"base": base_path, "state": None if locked else (state or {})})
    if locked:
        body = (
            "<h1>Confirm it's you</h1>"
            "<p class='lead'>Enter the 6-digit PIN your agent sent with this link.</p>"
            "<div class='err' id='err'></div>"
            "<form id='pinform'>"
            "<input class='pin' id='pin' inputmode='numeric' autocomplete='one-time-code' "
            "maxlength='6' placeholder='------' aria-label='PIN'>"
            "<button id='go' type='submit'>Continue</button>"
            "</form>"
        )
    else:
        body = "<p>Loading…</p>"

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<meta name='referrer' content='no-referrer'>"
        "<title>Connect · olano</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<div class='card'>"
        "<div class='brand'><span class='mark'></span><b>olano</b></div>"
        f"<div class='meta'>{_requester_line(requested_by)}</div>"
        f"<div id='body'>{body}</div>"
        "<p class='foot'>This link is single-use and expires shortly. "
        "Never share it or the PIN.</p>"
        "</div>"
        f"<script>window.__HS__={boot};</script>"
        f"<script>{_SCRIPT}</script>"
        "</body></html>"
    )


def render_notice(title: str, detail: str) -> str:
    """A minimal standalone card (unknown link, disabled feature, …)."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<title>Connect · olano</title>"
        f"<style>{_STYLE}</style></head><body><div class='card'>"
        "<div class='brand'><span class='mark'></span><b>olano</b></div>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>"
        "</div></body></html>"
    )
