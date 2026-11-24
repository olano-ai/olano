"""Recognising a site that refused the browser, and stopping the agent there.

A bot check is not a loading error, and an agent that reads it as one retries:
it reloads, tries ``old.reddit.com``, signs in again, and each attempt makes
the next refusal likelier and can get the user's account locked (X locks an
account that keeps failing to sign in from an unfamiliar browser). Two pieces
stop that:

- :func:`detect` reads a page (its address, title and visible text) and says
  whether it is a refusal, from whom and whether a person could get past it
  in the browser (a sign-in, a check to complete). Signals are phrases a
  refusal page uses and an ordinary page does not, and every one of them
  counts only on a page as short as a refusal page is
  (:data:`SHORT_PAGE_CHARS`): an article about CAPTCHAs or locked accounts
  reads normally, and a sign-in form "protected by reCAPTCHA" is scrubbed
  before matching. An HTTP status alone is never a refusal: a signed-out
  ``403`` from an app's API means "sign in first", and calling it a block
  would lock the agent out of the sign-in page too.
- The **memory** (:func:`remember` / :func:`recent`): a refusal is remembered
  per agent and site for :data:`COOLDOWN_S`, and the browser refuses to open
  that site again (a typed address, a clicked link and a script's navigation
  alike, at the request chokepoint through :func:`navigation_guard`) or to
  act on its page, until it lapses, until a person hands the live browser
  back (they may have signed in: ``sessions`` forgets the agent's blocks
  then), or until the agent's proxy changes (a new exit is a new chance; the
  memory keys on the proxy fingerprint). It stops the agent only: while a
  person holds the live browser, their navigations to the site go through
  (``sessions._agent_site_refusal``), since completing a check navigates.
  Telling the model to stop is not
  enough on its own: a model that is told a site is blocked still tries "one
  more time", so the second time costs nothing and sends nothing.
- A **site** is a host, not a registrable domain (:func:`site_of`): a
  refusal on ``bucket.s3.amazonaws.com`` or ``docs.google.com`` must not stop
  every other service on that domain. The sites this exists for serve one
  property from several hosts, so those collapse (:data:`_FAMILIES`:
  ``old.reddit.com`` is ``reddit.com``, ``mobile.twitter.com`` is ``x.com``),
  and a ``www.``/``m.``/``mobile.`` prefix is dropped everywhere.

The messages say what happened and what to do next, and nothing else: they do
not suggest another way to reach the site.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

COOLDOWN_S = 15 * 60.0
# A refusal page is short; no signal counts above this many characters of
# visible text (a block page measured here runs 200 to 900; an article runs
# into the tens of thousands).
SHORT_PAGE_CHARS = 3000
# How long a check that clears itself is given to do so (:attr:`BlockPage.transient`).
# Cloudflare's measured here in 5 to 15 seconds; a slow box takes longer.
INTERSTITIAL_WAIT_S = 25.0
_SCAN_CHARS = 4000
_MEMORY_MAX = 512

# Marker the agent-facing result starts with. Kept from the earlier wording:
# archetype prompts and skills quote it.
MARKER = "Automation blocked by site security"


@dataclass(frozen=True)
class _Signal:
    pattern: re.Pattern[str]
    who: str
    # Where it counts: "text" (title or text, on a short page), "title" (the
    # title alone, on a short page).
    where: str = "text"
    # Whether a person in the browser could get past it (sign in, complete a check).
    solvable: bool = True
    # Only on these sites (empty: any site).
    sites: tuple[str, ...] = ()
    # An interstitial that lets an ordinary browser through on its own after a
    # few seconds (Cloudflare's "Just a moment..."): waited out before it counts.
    transient: bool = False
    # A check a person passes in the page (a box to tick, a puzzle, press and
    # hold), as opposed to a refusal or a sign-in: ``human_check`` decides
    # what happens next.
    check: bool = False


def _p(text: str) -> re.Pattern[str]:
    return re.compile(text, re.IGNORECASE)


_X = ("x.com",)
_SIGNALS: tuple[_Signal, ...] = (
    # Reddit
    _Signal(_p(r"blocked by network security"), "Reddit"),
    _Signal(_p(r"whoa there,? pardner"), "Reddit"),
    # X
    _Signal(_p(r"this request looks like it might be automated"), "X"),
    _Signal(_p(r"could not log you in now"), "X"),
    _Signal(_p(r"temporarily limited some of your account features"), "X"),
    _Signal(_p(r"something went wrong\.? try reloading"), "X", sites=_X),
    # Accounts locked for a suspicious sign-in (X, and many others word it the same)
    _Signal(_p(r"your account (?:has been|is|was) (?:temporarily )?(?:locked|suspended)"), ""),
    # Cloudflare
    _Signal(_p(r"^just a moment\.*$"), "Cloudflare", where="title", transient=True, check=True),
    _Signal(_p(r"attention required!? \| cloudflare"), "Cloudflare", check=True),
    _Signal(
        _p(r"checking (?:if the site connection is secure|your browser before accessing)"),
        "Cloudflare",
        transient=True,
        check=True,
    ),
    _Signal(_p(r"sorry, you have been blocked"), "Cloudflare", solvable=False),
    _Signal(_p(r"error (?:code:? )?10(?:0[0-9]|1[0-9]|20)\b"), "Cloudflare", solvable=False),
    # Google
    _Signal(_p(r"unusual traffic from your computer network"), "Google", check=True),
    # DataDome
    _Signal(_p(r"please enable js and disable any ad ?blocker"), "DataDome", check=True),
    # HUMAN (PerimeterX)
    _Signal(_p(r"access to this page has been denied"), "HUMAN", check=True),
    _Signal(_p(r"press (?:&|and) hold"), "HUMAN", check=True),
    # Imperva / Distil
    _Signal(_p(r"incapsula incident id"), "Imperva", solvable=False),
    _Signal(_p(r"pardon our interruption"), "Imperva", check=True),
    # Generic wording
    _Signal(_p(r"verify(?:ing)? (?:that )?you(?: are|'re) (?:a )?human"), "", check=True),
    _Signal(_p(r"are you a robot\??"), "", check=True),
    _Signal(_p(r"access is temporarily restricted"), ""),
    _Signal(_p(r"you (?:are being|have been) rate[- ]limited"), "", solvable=False),
    _Signal(_p(r"automated (?:access|queries|requests|traffic)"), ""),
    _Signal(_p(r"unusual activity"), ""),
    _Signal(_p(r"bot (?:activity|detected|detection|protection)"), ""),
    _Signal(_p(r"captcha"), "", check=True),
    _Signal(_p(r"^access denied$"), "", where="title", solvable=False),
    _Signal(_p(r"access denied"), "", solvable=False),
    _Signal(_p(r"too many requests"), "", solvable=False),
)

# Boilerplate an ordinary form carries; never a block on its own.
_BOILERPLATE = _p(
    r"(?:protected by (?:re|h)?captcha(?: and the google)?|"
    r"(?:re|h)?captcha (?:privacy policy|terms(?: of service)?))"
)

# Properties served from several hosts: any host under one of these is the
# family. ``twitter.com`` is X's old name and still answers.
_FAMILIES = {
    "reddit.com": "reddit.com",
    "x.com": "x.com",
    "twitter.com": "x.com",
    "linkedin.com": "linkedin.com",
    "facebook.com": "facebook.com",
    "instagram.com": "instagram.com",
    "tiktok.com": "tiktok.com",
}
_MIRROR_PREFIXES = ("www.", "m.", "mobile.")


def site_of(url_or_host: str) -> str:
    """The site a URL or host belongs to (module docstring): ``old.reddit.com``
    is ``reddit.com``, ``mobile.twitter.com`` is ``x.com``, ``www.example.com``
    is ``example.com``, ``docs.google.com`` is itself. ``""`` without a host."""
    text = (url_or_host or "").strip()
    host = text
    # A URL (``https://…``, ``about:blank``), as opposed to ``host`` or ``host:port``.
    if "://" in text or re.match(r"^[a-z][a-z0-9+.-]*:(?!\d+$)", text, re.IGNORECASE):
        try:
            host = urlsplit(text).hostname or ""
        except ValueError:
            host = ""
    else:
        host = text.rsplit(":", 1)[0] if re.search(r":\d+$", text) else text
    host = host.lower().strip("[]").rstrip(".")
    if not host or ":" in host or re.fullmatch(r"[\d.]+", host):
        return host
    for family, canonical in _FAMILIES.items():
        if host == family or host.endswith("." + family):
            return canonical
    for prefix in _MIRROR_PREFIXES:
        if host.startswith(prefix) and host.count(".") >= 2:
            return host[len(prefix) :]
    return host


@dataclass(frozen=True)
class BlockPage:
    site: str
    signal: str  # what the page said, or the HTTP status
    who: str = ""  # the protection that refused, when it is recognisable
    solvable: bool = True
    transient: bool = False  # it may still let the browser through (:data:`INTERSTITIAL_WAIT_S`)
    human_check: bool = False  # a person passes it in the page (olano.browser.human_check)


def detect(url: str, title: str, text: str, status: int | None = None) -> BlockPage | None:
    """Whether the page at *url* is a site refusing the browser.

    *status* (the navigation's HTTP status, when known) is reported with the
    signal; it never decides on its own.
    """
    site = site_of(url)
    title = (title or "").strip()
    if len((text or "").strip()) >= SHORT_PAGE_CHARS:
        return None
    scrubbed = _BOILERPLATE.sub(" ", (text or "")[:_SCAN_CHARS])
    for sig in _SIGNALS:
        if sig.sites and site not in sig.sites:
            continue
        haystacks = (title,) if sig.where == "title" else (title, scrubbed)
        for hay in haystacks:
            match = sig.pattern.search(hay)
            if match:
                signal = _quote(match.group(0))
                if status is not None and status >= 400:
                    signal = f"{signal} (HTTP {status})"
                return BlockPage(
                    site=site,
                    signal=signal,
                    who=sig.who,
                    solvable=sig.solvable,
                    transient=sig.transient,
                    human_check=sig.check,
                )
    return None


def _quote(text: str) -> str:
    return " ".join(text.split())[:120]


# ── messages ────────────────────────────────────────────────────────────────


def block_message(block: BlockPage, *, live: bool) -> str:
    """The first time: what happened, and that the agent must stop."""
    by = f" ({block.who})" if block.who else ""
    lines = [
        f'{MARKER}: {block.site} refused this browser{by}. The page says: "{block.signal}".',
        "This is the site's own protection turning the browser away, not a loading "
        f"problem. Reloading, retrying, signing in again or opening another address on "
        f"{block.site} will not get past it, and repeated attempts can get the user's "
        f"account locked. Stop using {block.site} for this task: do not open it again. "
        f"Tell the user that {block.site} blocked the browser.",
    ]
    if live and block.solvable:
        lines.append(
            "If the user wants to get past it themselves (for example by signing in or "
            "completing the check), call browser_request_user once and wait for them to "
            "hand the browser back."
        )
    return "\n".join(lines)


def refusal_message(site: str, entry: dict, *, live: bool, acting: bool = False) -> str:
    """Every later attempt: refused without touching the site."""
    ago = max(0, int((time.time() - float(entry.get("at", 0))) // 60))
    when = "just now" if ago < 1 else f"{ago} minute{'s' if ago != 1 else ''} ago"
    what = "Not done" if acting else "Not opened"
    text = (
        f"{MARKER}: {what}. {site} refused this browser {when} "
        f'("{entry.get("signal", "")}") and would refuse it again; trying again only '
        f"risks getting the user's account locked. Do not retry {site} in this task. "
        f"Tell the user that {site} blocked the browser."
    )
    if live:
        text += (
            " It can be tried again after a person takes control of the live browser "
            "and hands it back (for example once they have signed in)."
        )
    return text


# ── memory ──────────────────────────────────────────────────────────────────

_memory: dict[tuple[str, str, str], dict] = {}
_lock = threading.Lock()


def remember(agent: str, block: BlockPage, *, scope: str = "session", proxy_key: str = "") -> None:
    """Record that *block.site* refused *agent*'s browser.

    *scope* is ``session`` (the agent's own, possibly signed-in browser) or
    ``capture`` (a one-shot, signed-out tab): a signed-out tab being turned
    away says nothing about the signed-in one, so a capture's block only stops
    further captures.
    """
    if not block.site:
        return
    with _lock:
        if len(_memory) >= _MEMORY_MAX:
            _memory.clear()
        key = (agent, block.site, scope)
        count = int(_memory.get(key, {}).get("count", 0)) + 1
        _memory[key] = {
            "at": time.time(),
            "signal": block.signal,
            "who": block.who,
            "proxy": proxy_key,
            "count": count,
        }


def recent(
    agent: str, url_or_site: str, *, scopes: tuple[str, ...] = ("session",), proxy_key: str = ""
) -> dict | None:
    """The live block for *agent* on that site, or ``None``.

    A block recorded under another proxy does not count: a new exit is a new chance.
    """
    site = site_of(url_or_site)
    if not site:
        return None
    now = time.time()
    with _lock:
        for scope in scopes:
            entry = _memory.get((agent, site, scope))
            if entry is None:
                continue
            if now - float(entry["at"]) > COOLDOWN_S:
                _memory.pop((agent, site, scope), None)
                continue
            if entry.get("proxy", "") != proxy_key:
                continue
            return dict(entry)
    return None


def navigation_guard(
    agent: str,
    *,
    proxy_key: str = "",
    live: bool = False,
    scopes: tuple[str, ...] = ("session",),
) -> Callable[[str], str | None]:
    """``GuardState.site_refusal`` for *agent*'s browser: the refusal for a
    top-level navigation to a remembered-blocked site, else ``None``."""

    def _refusal(url: str) -> str | None:
        hit = recent(agent, url, scopes=scopes, proxy_key=proxy_key)
        if hit is None:
            return None
        return refusal_message(site_of(url), hit, live=live)

    return _refusal


def clear(agent: str, url_or_site: str, *, scope: str = "session") -> None:
    """The site served *agent* a real page: forget its block."""
    site = site_of(url_or_site)
    with _lock:
        _memory.pop((agent, site, scope), None)


def forget(agent: str | None = None) -> None:
    """Forget *agent*'s blocks (every agent's when ``None``)."""
    with _lock:
        if agent is None:
            _memory.clear()
            return
        for key in [k for k in _memory if k[0] == agent]:
            _memory.pop(key, None)
