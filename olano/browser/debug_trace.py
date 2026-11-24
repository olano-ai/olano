"""A record of what an agent's browser and a site said to each other around a check.

Built to answer one question a screenshot cannot: a person ticks Cloudflare's
"Verify you are human" box in the live browser, the tick turns green, and the
page comes back with a fresh box. Cloudflare's pass is the ``cf_clearance``
cookie, issued once the box is ticked and checked on every later request
against the address and browser it was issued to. A loop therefore has one of
a few causes, and each leaves a different trace:

- the pass was never issued (no ``cf_clearance`` in any response);
- it was issued and the next page load did not send it back (the browser
  dropped or never stored it). Which cookies a request really carried comes
  from Chromium itself (``Network.requestWillBeSentExtraInfo``, with the
  reason for each one left out): Playwright's header list omits partitioned
  cookies, and ``cf_clearance`` is one, so read alone it reports a pass as
  missing that was in fact sent;
- it was sent and Cloudflare answered with another challenge
  (``cf-mitigated: challenge``): the pass was refused, most often because the
  address changed since it was issued (a rotating proxy hands out a new exit
  per connection), or the browser no longer looked like the one it was
  issued to;
- our own browser refused a request the check needed (the egress guard).

So a trace keeps, for the site's page loads and Cloudflare's own requests
(``/cdn-cgi/``, ``challenges.cloudflare.com``): the status, whether Cloudflare
re-challenged, the names of the cookies the request carried and the response
set (never their values), and every request that failed. It samples the exit
address the way Cloudflare reports it (``/cdn-cgi/trace`` on the site,
fetched by the runtime through the context's proxy: a rotating proxy shows as
samples that disagree), takes one snapshot of what the page reports about the
browser, and notes control changes and the start and end of a wait for a
person. :func:`analyze` reads the record and names the cause it points to.

Nothing personal is kept: URLs lose their query values and long path
segments, cookies are names and attributes, no body is read. A trace starts
by itself when the browser starts waiting for a person on a check
(:func:`start`, from ``sessions.begin_human_check``) and stops a few minutes
after the wait ends, or is started and stopped by hand from the agent's
Browser tab; the last one per agent outlives its session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any
from urllib.parse import parse_qsl, urlsplit

logger = logging.getLogger(__name__)

MAX_EVENTS = 3000
MAX_EXIT_SAMPLES = 40
EXIT_SAMPLE_EVERY_S = 15.0
# An automatic trace keeps recording this long after the wait it was started for.
AFTER_CHECK_S = 300.0
CLEARANCE = "cf_clearance"

_last: dict[str, dict[str, Any]] = {}

_FINGERPRINT_JS = """() => ({
  userAgent: navigator.userAgent,
  webdriver: navigator.webdriver,
  languages: (navigator.languages || []).join(','),
  timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  platform: navigator.platform,
  screen: screen.width + 'x' + screen.height,
  inner: innerWidth + 'x' + innerHeight,
  outer: outerWidth + 'x' + outerHeight,
  cookieEnabled: navigator.cookieEnabled,
})"""


def clean_url(url: str) -> str:
    """``scheme://host/path?names`` with query values and token-like path
    segments removed (challenge paths carry one-use tokens)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:80]
    if parts.scheme not in ("http", "https"):
        return f"{parts.scheme}:" if parts.scheme else url[:80]
    segments = ["…" if len(s) > 24 else s for s in parts.path.split("/")]
    out = f"{parts.scheme}://{parts.hostname or ''}{'/'.join(segments)}"
    names = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    return out + ("?" + "&".join(names) if names else "")


def cookie_names(header: str) -> list[str]:
    """Names in a ``Cookie`` request header."""
    out = []
    for part in (header or "").split(";"):
        name = part.split("=", 1)[0].strip()
        if name:
            out.append(name)
    return out


def set_cookies(header: str) -> list[dict[str, Any]]:
    """``Set-Cookie`` lines (Playwright joins them with newlines) as name and
    attributes, never the value."""
    out = []
    for line in (header or "").splitlines():
        first, _, attrs = line.partition(";")
        name = first.split("=", 1)[0].strip()
        if not name:
            continue
        flags: dict[str, Any] = {"name": name}
        for attr in attrs.split(";"):
            key, _, value = attr.strip().partition("=")
            key = key.lower()
            if key in ("domain", "path", "samesite", "max-age"):
                flags[key] = value
            elif key == "expires":
                flags["expires"] = value
            elif key in ("secure", "httponly", "partitioned"):
                flags[key] = True
        out.append(flags)
    return out


def _interesting(url: str, resource_type: str) -> bool:
    if resource_type == "document":
        return True
    return "/cdn-cgi/" in url or "challenges.cloudflare.com" in url


class Trace:
    """One recording on one session's browser context."""

    def __init__(self, sess: Any, reason: str) -> None:
        self.agent = sess.agent
        self.kind = sess.kind
        self.proxy = bool(sess.proxy_key)
        self.reason = reason
        self.started_at = time.time()
        self.stopped_at: float | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.exit_samples: list[dict[str, Any]] = []
        self.fingerprint: dict[str, Any] | None = None
        self._sess = sess
        self._last_sample = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._stop_handle: asyncio.TimerHandle | None = None
        self._handlers: list[tuple[str, Any]] = []
        self._cdp: list[Any] = []

    # ── recording ───────────────────────────────────────────────────────────

    def note(self, kind: str, *, at: float | None = None, **data: Any) -> None:
        """Record an event; *at* is when it happened, if earlier than now (the
        network events are stamped when the browser fires them, before their
        headers are read, so the record keeps the browser's order)."""
        if self.stopped_at is not None:
            return
        when = round((at or time.time()) - self.started_at, 3)
        self.events.append({"t": when, "kind": kind, **data})

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def attach(self) -> None:
        context = self._sess.context

        def on_request(request: Any) -> None:
            url = str(request.url)
            if _interesting(url, request.resource_type):
                self._spawn(self._on_request(request, time.time()))

        def on_response(response: Any) -> None:
            request = response.request
            if _interesting(str(response.url), request.resource_type):
                self._spawn(self._on_response(response, time.time()))

        def on_failed(request: Any) -> None:
            self.note(
                "request_failed",
                url=clean_url(str(request.url)),
                type=request.resource_type,
                method=request.method,
                error=str(request.failure or "")[:200],
            )

        def on_page(page: Any) -> None:
            self._spawn(self._watch_cookies(page))

        for event, handler in (
            ("request", on_request),
            ("response", on_response),
            ("requestfailed", on_failed),
            ("page", on_page),
        ):
            context.on(event, handler)
            self._handlers.append((event, handler))
        for page in list(context.pages):
            if not page.is_closed():
                self._spawn(self._watch_cookies(page))
        page = self._sess.page
        if page is not None and not page.is_closed():
            self._spawn(self._snapshot(page))
            self._spawn(self._sample_exit(str(page.url), force=True))

    async def _watch_cookies(self, page: Any) -> None:
        """Chromium's own account of each cookie: which ones a request carried
        and which it left out and why (``Network.requestWillBeSentExtraInfo``),
        and which ``Set-Cookie`` lines it refused to store
        (``responseReceivedExtraInfo``). The ground truth for "the pass was
        issued, and then?": a header list cannot say why a cookie is missing."""
        try:
            cdp = await self._sess.context.new_cdp_session(page)
            await cdp.send("Network.enable")
        except Exception as exc:
            self.note("cookie_watch_unavailable", error=str(exc)[:200])
            return
        self._cdp.append(cdp)

        def on_sent(event: dict[str, Any]) -> None:
            cookies = event.get("associatedCookies") or []
            names = {(c.get("cookie") or {}).get("name") for c in cookies}
            if CLEARANCE not in names and not any(c.get("blockedReasons") for c in cookies):
                return
            self.note(
                "cookies_considered",
                request=str(event.get("requestId", ""))[:40],
                cookies=[
                    {
                        "name": (c.get("cookie") or {}).get("name"),
                        "sent": not c.get("blockedReasons"),
                        "blocked": c.get("blockedReasons") or [],
                        "partition": (c.get("cookie") or {}).get("partitionKey"),
                    }
                    for c in cookies
                ],
                site_has_partitioned=event.get("siteHasCookieInOtherPartition"),
            )

        def on_received(event: dict[str, Any]) -> None:
            blocked = event.get("blockedCookies") or []
            headers = event.get("headers") or {}
            raw = next((v for k, v in headers.items() if k.lower() == "set-cookie"), "")
            if not blocked and CLEARANCE not in str(raw):
                return
            self.note(
                "cookies_received",
                request=str(event.get("requestId", ""))[:40],
                partition=event.get("cookiePartitionKey"),
                partition_opaque=event.get("cookiePartitionKeyOpaque"),
                refused=[
                    {
                        "name": (b.get("cookie") or {}).get("name")
                        or str(b.get("cookieLine", "")).split("=", 1)[0],
                        "reasons": b.get("blockedReasons") or [],
                    }
                    for b in blocked
                ],
            )

        cdp.on("Network.requestWillBeSentExtraInfo", on_sent)
        cdp.on("Network.responseReceivedExtraInfo", on_received)

    def detach(self) -> None:
        for cdp in self._cdp:
            self._spawn(_quietly_detach(cdp))
        self._cdp.clear()
        context = self._sess.context
        for event, handler in self._handlers:
            try:
                context.remove_listener(event, handler)
            except Exception:
                pass
        self._handlers.clear()
        if self._stop_handle is not None:
            self._stop_handle.cancel()
            self._stop_handle = None

    async def _on_request(self, request: Any, at: float) -> None:
        try:
            headers = await request.all_headers()
        except Exception:
            headers = {}
        main = False
        try:
            main = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:
            pass
        self.note(
            "request",
            at=at,
            url=clean_url(str(request.url)),
            type=request.resource_type,
            method=request.method,
            main_frame=main,
            cookies_sent=cookie_names(headers.get("cookie", "")),
            user_agent=headers.get("user-agent", "")[:200] if main else None,
        )

    async def _on_response(self, response: Any, at: float) -> None:
        try:
            headers = await response.all_headers()
        except Exception:
            headers = {}
        request = response.request
        main = False
        try:
            main = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:
            pass
        self.note(
            "response",
            at=at,
            url=clean_url(str(response.url)),
            type=request.resource_type,
            main_frame=main,
            status=response.status,
            mitigated=headers.get("cf-mitigated", ""),
            cookies_set=set_cookies(headers.get("set-cookie", "")),
            ray=headers.get("cf-ray", "")[:40],
        )
        if request.resource_type == "document":
            self._spawn(self._cookies(str(response.url)))
            self._spawn(self._sample_exit(str(response.url)))

    async def _cookies(self, url: str) -> None:
        try:
            jar = await self._sess.context.cookies([url])
        except Exception as exc:
            self.note("cookies", url=clean_url(url), error=str(exc)[:200])
            return
        self.note(
            "cookies",
            url=clean_url(url),
            jar=[
                {
                    "name": c.get("name"),
                    "domain": c.get("domain"),
                    "expires": c.get("expires"),
                    "sameSite": c.get("sameSite"),
                    "secure": c.get("secure"),
                }
                for c in jar
            ],
        )

    async def _snapshot(self, page: Any) -> None:
        """What the page reports about the browser. A page that is navigating
        cannot answer (its context is being replaced), so it is asked again
        once the next document is in."""
        for attempt in range(3):
            try:
                self.fingerprint = await page.evaluate(_FINGERPRINT_JS)
                return
            except Exception as exc:
                self.fingerprint = {"error": str(exc)[:200]}
                if attempt < 2:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                    except Exception:
                        await asyncio.sleep(0.5)

    async def _sample_exit(self, url: str, *, force: bool = False) -> None:
        """The exit address as Cloudflare reports it to the site's origin."""
        now = time.time()
        if len(self.exit_samples) >= MAX_EXIT_SAMPLES:
            return
        if not force and now - self._last_sample < EXIT_SAMPLE_EVERY_S:
            return
        try:
            parts = urlsplit(url)
        except ValueError:
            return
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return
        self._last_sample = now
        origin = f"{parts.scheme}://{parts.netloc}"
        from olano.browser.guard import url_refusal

        target = origin + "/cdn-cgi/trace"
        if await url_refusal(target, self._sess.guard.policy):
            return
        sample: dict[str, Any] = {"t": round(now - self.started_at, 3), "origin": origin}
        try:
            resp = await self._sess.context.request.get(
                target, timeout=8_000, fail_on_status_code=False, max_redirects=0
            )
            text = (await resp.text())[:2000] if resp.ok else ""
            sample["status"] = resp.status
            for line in text.splitlines():
                key, _, value = line.partition("=")
                if key in ("ip", "loc", "colo", "http", "tls", "warp"):
                    sample[key] = value
        except Exception as exc:
            sample["error"] = str(exc)[:200]
        self.exit_samples.append(sample)

    # ── output ──────────────────────────────────────────────────────────────

    def export(self) -> dict[str, Any]:
        blocked = [
            {**b, "url": clean_url(str(b.get("url", ""))), "t": round(b["at"] - self.started_at, 3)}
            for b in list(self._sess.guard.blocked)
            if float(b.get("at", 0)) >= self.started_at
        ]
        out = {
            "agent": self.agent,
            "browser": self.kind,
            "through_proxy": self.proxy,
            "reason": self.reason,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "fingerprint": self.fingerprint,
            "exit_samples": list(self.exit_samples),
            "guard_refusals": blocked,
            "events": sorted(self.events, key=lambda e: e["t"]),
        }
        out["analysis"] = analyze(out)
        return out


async def _quietly_detach(cdp: Any) -> None:
    try:
        await cdp.detach()
    except Exception:
        pass


# ── reading a trace ───────────────────────────────────────────────────────────

_ADVICE = {
    "exit_changes": (
        "The exit address changed while the check was running ({ips}). Cloudflare ties "
        "its pass to the address it was issued to, so every new address is checked again: "
        "a proxy that hands out a new address per connection loops forever. Use a proxy "
        "with a fixed (sticky) address, or none."
    ),
    "clearance_not_issued": (
        "Cloudflare never issued its pass (no cf_clearance cookie in any response), so the "
        "tick was not accepted on Cloudflare's side. If the box turned green, the request "
        "that should carry the result may have failed: see the failed requests."
    ),
    "clearance_not_sent": (
        "Cloudflare issued its pass (cf_clearance) but the next page load did not send it "
        "back, so Cloudflare checked again.{why}"
    ),
    "clearance_rejected": (
        "The browser sent Cloudflare's pass (cf_clearance) and Cloudflare challenged again "
        "anyway. It no longer trusts the pass for this visitor: the address or the browser "
        "it was issued to changed, or it still scores this browser as automated."
    ),
    "guard_refused": (
        "This browser's own guard refused {count} request(s) during the check ({urls}). A "
        "check whose requests are refused cannot finish."
    ),
    "challenge_requests_failed": ("{count} request(s) to Cloudflare's check failed ({urls})."),
    "passed": "The site served a page without a challenge after the check: it passed.",
}


# Hosts Cloudflare's check asks for that no browser can reach: measured from a
# droplet, ``brunhild.challenges.cloudflare.com`` has no address on 1.1.1.1 or
# 8.8.8.8, so every browser fails it and its failure says nothing about ours.
_PROBE_HOSTS = ("://brunhild.challenges.cloudflare.com/",)


def _expected_failure(event: dict[str, Any]) -> bool:
    url = str(event.get("url", ""))
    return any(h in url for h in _PROBE_HOSTS) and "NAME_NOT_RESOLVED" in str(event.get("error"))


def analyze(trace: dict[str, Any]) -> dict[str, Any]:
    """The causes the record points to, most likely first, with plain advice."""
    events = trace.get("events") or []
    findings: list[dict[str, str]] = []

    def add(code: str, **fmt: Any) -> None:
        findings.append({"code": code, "text": _ADVICE[code].format(**fmt)})

    ips = [s["ip"] for s in trace.get("exit_samples") or [] if s.get("ip")]
    if len(set(ips)) > 1:
        add("exit_changes", ips=", ".join(dict.fromkeys(ips)))

    refused = trace.get("guard_refusals") or []
    if refused:
        urls = ", ".join(sorted({str(b.get("url", "")) for b in refused})[:5])
        add("guard_refused", count=len(refused), urls=urls)

    failed = [
        e
        for e in events
        if e["kind"] == "request_failed"
        and ("/cdn-cgi/" in e.get("url", "") or "challenges.cloudflare.com" in e.get("url", ""))
        and not _expected_failure(e)
    ]
    if failed:
        add(
            "challenge_requests_failed",
            count=len(failed),
            urls=", ".join(sorted({f"{e['url']} ({e.get('error', '')})" for e in failed})[:4]),
        )

    issued_at: float | None = None
    outcome = ""
    not_sent = False
    for e in events:
        if e["kind"] == "response" and any(
            c.get("name") == CLEARANCE for c in e.get("cookies_set") or []
        ):
            issued_at = e["t"] if issued_at is None else issued_at
        if issued_at is None or e["t"] <= issued_at:
            continue
        # Playwright's header list leaves partitioned cookies out (cf_clearance
        # is one), so Chromium's own account, when there is one, decides.
        if e["kind"] == "request" and e.get("main_frame"):
            not_sent = CLEARANCE not in (e.get("cookies_sent") or [])
        if e["kind"] == "cookies_considered":
            for c in e.get("cookies") or []:
                if c.get("name") == CLEARANCE:
                    not_sent = not c.get("sent")
        # Only the site's own page counts: Cloudflare's box is a document too.
        if e["kind"] == "response" and e.get("main_frame") and not outcome:
            if e.get("mitigated") == "challenge":
                outcome = "clearance_not_sent" if not_sent else "clearance_rejected"
            elif int(e.get("status") or 0) < 400:
                outcome = "passed"
    challenged = any(e["kind"] == "response" and e.get("mitigated") == "challenge" for e in events)
    if outcome == "clearance_not_sent":
        reasons = sorted(
            {
                reason
                for e in events
                if e["kind"] == "cookies_considered"
                for c in e.get("cookies") or []
                if c.get("name") == CLEARANCE
                for reason in c.get("blocked") or []
            }
        )
        why = f" Chromium left it out because: {', '.join(reasons)}." if reasons else ""
        add(outcome, why=why)
    elif outcome:
        add(outcome)
    elif challenged and issued_at is None:
        add("clearance_not_issued")

    return {
        "challenged": challenged,
        "clearance_issued": issued_at is not None,
        "exit_addresses": list(dict.fromkeys(ips)),
        "findings": findings,
    }


# ── the session's recording ───────────────────────────────────────────────────


def start(sess: Any, reason: str) -> Trace:
    """Start recording *sess*'s browser (a running one keeps going)."""
    current: Trace | None = getattr(sess, "trace", None)
    if current is not None and current.stopped_at is None:
        if current._stop_handle is not None:
            current._stop_handle.cancel()
            current._stop_handle = None
        return current
    trace = Trace(sess, reason)
    sess.trace = trace
    trace.attach()
    logger.info("browser: debug trace started for %s (%s)", sess.agent, reason)
    return trace


def stop(sess: Any) -> dict[str, Any] | None:
    """Stop recording; the result stays readable (:func:`latest`)."""
    trace = getattr(sess, "trace", None)
    if trace is None:
        return _last.get(sess.agent)
    if trace.stopped_at is None:
        trace.note("stopped")
        trace.stopped_at = time.time()
        trace.detach()
    _last[sess.agent] = trace.export()
    return _last[sess.agent]


def stop_later(sess: Any, delay_s: float = AFTER_CHECK_S) -> None:
    """Stop an automatic trace a while after its check is over, to see what
    the site did next."""
    trace = getattr(sess, "trace", None)
    if trace is None or trace.stopped_at is not None or trace.reason != "human check":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if trace._stop_handle is not None:
        trace._stop_handle.cancel()
    trace._stop_handle = loop.call_later(delay_s, stop, sess)


def note(sess: Any, kind: str, **data: Any) -> None:
    trace = getattr(sess, "trace", None)
    if trace is not None:
        trace.note(kind, **data)


def latest(sess: Any | None, agent: str) -> dict[str, Any] | None:
    """The running trace's current state, else the last one recorded."""
    trace: Trace | None = getattr(sess, "trace", None) if sess is not None else None
    if trace is not None and trace.stopped_at is None:
        return trace.export()
    return _last.get(agent)
