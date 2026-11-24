"""Block-page detection and memory, proxy parsing, identity, the timezone
setting: everything about how an agent's browser meets a site that needs no
browser to check. The live half is ``test_browser_identity_proxy_blocks_live.py``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from olano.browser import blockpage, launch
from olano.browser.blockpage import detect, site_of

# ── detection ───────────────────────────────────────────────────────────────

_ARTICLE = "A long, ordinary article. " * 120  # well past SHORT_PAGE_CHARS


@pytest.mark.parametrize(
    ("url", "title", "text", "status", "who"),
    [
        (
            "https://www.reddit.com/r/python/",
            "Blocked",
            "You've been blocked by network security. To continue, log in to your Reddit "
            "account or use your developer token",
            403,
            "Reddit",
        ),
        (
            "https://old.reddit.com/",
            "",
            "whoa there, pardner! your request has been blocked",
            None,
            "Reddit",
        ),
        (
            "https://x.com/i/flow/login",
            "Log in to X",
            "Could not log you in now. Please try again later.",
            None,
            "X",
        ),
        ("https://x.com/home", "X", "Something went wrong. Try reloading.\nRetry", None, "X"),
        (
            "https://x.com/account/access",
            "",
            "This request looks like it might be automated. To protect our users from spam...",
            None,
            "X",
        ),
        (
            "https://shop.example/",
            "Just a moment...",
            "Enable JavaScript and cookies to continue",
            403,
            "Cloudflare",
        ),
        (
            "https://shop.example/",
            "Attention Required! | Cloudflare",
            "Sorry, you have been blocked",
            403,
            "Cloudflare",
        ),
        (
            "https://www.google.com/sorry/",
            "",
            "Our systems have detected unusual traffic from your computer network.",
            429,
            "Google",
        ),
        ("https://shop.example/", "", "Access to this page has been denied.", 403, "HUMAN"),
        ("https://shop.example/", "", "Pardon Our Interruption", None, "Imperva"),
        (
            "https://www.wsj.com/",
            "WSJ",
            "Access is temporarily restricted. We detected unusual activity.",
            None,
            "",
        ),
        (
            "https://shop.example/",
            "Access Denied",
            "You don't have permission to access this server.",
            403,
            "",
        ),
        ("https://shop.example/", "", "Please verify you are a human", None, ""),
    ],
)
def test_refusal_pages_are_recognised(url, title, text, status, who):
    block = detect(url, title, text, status)
    assert block is not None
    assert block.who == who
    assert block.site == site_of(url)
    assert ("HTTP 403" in block.signal) is (status == 403)


@pytest.mark.parametrize(
    ("url", "title", "text", "status"),
    [
        # Sign-in forms carry this footer; it is not a challenge.
        (
            "https://accounts.example/",
            "Sign in",
            "Email\nContinue\nThis site is protected by reCAPTCHA and the Google Privacy "
            "Policy and Terms of Service apply.",
            200,
        ),
        # Ambiguous words on a long page are an article, not a block.
        (
            "https://blog.example/captcha",
            "How CAPTCHAs work",
            _ARTICLE + " captcha access denied",
            200,
        ),
        # A status alone is never a refusal: a signed-out API answers 403 too.
        ("https://docs.example/private", "Private", _ARTICLE, 403),
        ("https://app.example.com/api/me", "Forbidden", '{"error":"forbidden"}', 403),
        ("https://shop.example/", "", "Rate limit", 429),
        # Distinctive wording quoted inside a long page is a page about it.
        (
            "https://support.google.com/accounts/answer/40039",
            "Fix a locked account",
            "If your account has been locked because of an unusual sign-in... " + _ARTICLE,
            200,
        ),
        (
            "https://blog.example/cf",
            "Cloudflare challenges",
            _ARTICLE + " Sorry, you have been blocked",
            200,
        ),
        # X's retry text is a block on X, not on any site that words an error the same.
        ("https://app.example/", "App", "Something went wrong. Try reloading.", 200),
        ("https://www.linkedin.com/in/someone", "Someone | LinkedIn", "Experience\nEducation", 200),
    ],
)
def test_ordinary_pages_are_not_blocks(url, title, text, status):
    assert detect(url, title, text, status) is None


@pytest.mark.parametrize(
    ("given", "site"),
    [
        ("https://www.reddit.com/r/x", "reddit.com"),
        ("https://old.reddit.com/", "reddit.com"),
        ("https://mobile.twitter.com/someone", "x.com"),
        ("https://x.com/home", "x.com"),
        ("https://www.bbc.co.uk/news", "bbc.co.uk"),
        ("https://m.facebook.com/x", "facebook.com"),
        # Hosts, not registrable domains: one tenant's refusal is not the domain's.
        ("https://bucket.s3.amazonaws.com/key", "bucket.s3.amazonaws.com"),
        ("https://someone.github.io/", "someone.github.io"),
        ("https://docs.google.com/", "docs.google.com"),
        ("https://www.google.com/search", "google.com"),
        ("https://m.example.com/", "example.com"),
        ("http://127.0.0.1:8000/a", "127.0.0.1"),
        ("localhost:8000", "localhost"),
        ("linkedin.com", "linkedin.com"),
        ("about:blank", ""),
    ],
)
def test_site_of(given, site):
    assert site_of(given) == site


# ── messages ────────────────────────────────────────────────────────────────


def test_messages_say_stop_and_suggest_no_other_route():
    block = detect("https://www.reddit.com/", "", "You've been blocked by network security.", 403)
    assert block is not None
    for live in (False, True):
        first = blockpage.block_message(block, live=live)
        later = blockpage.refusal_message(
            "reddit.com", {"at": 0, "signal": block.signal}, live=live
        )
        for text in (first, later):
            assert text.startswith(blockpage.MARKER)
            assert not re.search(r"\bAPI\b|developer token|old\.reddit", text), text
            assert chr(0x2014) not in text  # no em dash in what the agent reads
        assert "do not open it again" in first
        assert ("browser_request_user" in first) is live
    unsolvable = detect("https://shop.example/", "", "Sorry, you have been blocked", 403)
    assert unsolvable is not None
    assert "browser_request_user" not in blockpage.block_message(unsolvable, live=True)


# ── memory ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_memory():
    blockpage.forget()
    yield
    blockpage.forget()


def _reddit():
    block = detect("https://www.reddit.com/", "", "blocked by network security", 403)
    assert block is not None
    return block


def test_a_block_is_remembered_for_the_whole_site_then_lapses(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(blockpage.time, "time", lambda: now[0])
    blockpage.remember("alice", _reddit())
    assert blockpage.recent("alice", "https://old.reddit.com/r/x") is not None
    assert blockpage.recent("bob", "https://www.reddit.com/") is None
    now[0] += blockpage.COOLDOWN_S + 1
    assert blockpage.recent("alice", "https://www.reddit.com/") is None


def test_a_new_proxy_is_a_new_chance():
    blockpage.remember("alice", _reddit(), proxy_key="exit-a")
    assert blockpage.recent("alice", "reddit.com", proxy_key="exit-a") is not None
    assert blockpage.recent("alice", "reddit.com", proxy_key="exit-b") is None
    assert blockpage.recent("alice", "reddit.com") is None


def test_capture_and_session_scopes_are_separate():
    blockpage.remember("alice", _reddit(), scope="capture")
    assert blockpage.recent("alice", "reddit.com") is None
    assert blockpage.recent("alice", "reddit.com", scopes=("session", "capture")) is not None


def test_navigation_guard_refuses_only_the_remembered_site():
    blockpage.remember("alice", _reddit(), proxy_key="k")
    refuse = blockpage.navigation_guard("alice", proxy_key="k", live=True)
    text = refuse("https://old.reddit.com/r/python")
    assert text and text.startswith(f"{blockpage.MARKER}: Not opened.") and "live browser" in text
    assert refuse("https://news.ycombinator.com/") is None
    assert blockpage.navigation_guard("alice", proxy_key="other")("https://reddit.com/") is None
    assert blockpage.navigation_guard("bob", proxy_key="k")("https://reddit.com/") is None


def test_a_real_page_or_a_hand_back_clears_it():
    blockpage.remember("alice", _reddit())
    blockpage.clear("alice", "https://www.reddit.com/r/python")
    assert blockpage.recent("alice", "reddit.com") is None
    blockpage.remember("alice", _reddit())
    blockpage.forget("alice")
    assert blockpage.recent("alice", "reddit.com") is None


# ── proxy ───────────────────────────────────────────────────────────────────


def test_parse_proxy_accepts_the_usual_shapes():
    assert launch.parse_proxy("") is None
    assert launch.parse_proxy(None) is None
    assert launch.parse_proxy("http://gw.example:8080") == {"server": "http://gw.example:8080"}
    assert launch.parse_proxy("gw.example:3128") == {"server": "http://gw.example:3128"}
    assert launch.parse_proxy("socks5://10.0.0.2:1080") == {"server": "socks5://10.0.0.2:1080"}
    assert launch.parse_proxy("http://us%40er:p%3Ass@gw.example:8080") == {
        "server": "http://gw.example:8080",
        "username": "us@er",
        "password": "p:ss",
    }


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        ("ftp://gw.example:21", "ftp"),
        ("http://gw.example", "no port"),
        ("http://:8080", "no host"),
        ("socks5://u:p@gw.example:1080", "SOCKS"),
        ("http://169.254.169.254:80", "metadata"),
        ("http://gw.example:notaport", "not a valid"),
    ],
)
def test_parse_proxy_refuses_what_the_browser_cannot_use(raw, words):
    with pytest.raises(launch.ProxyError) as err:
        launch.parse_proxy(raw)
    assert words in str(err.value)
    assert "p@" not in str(err.value)


def test_proxy_fingerprint_changes_with_the_exit_and_hides_the_password():
    a = launch.proxy_fingerprint(launch.parse_proxy("http://u:one@gw.example:1"))
    b = launch.proxy_fingerprint(launch.parse_proxy("http://u:two@gw.example:1"))
    assert a and b and a != b
    assert "one" not in a
    assert launch.proxy_fingerprint(None) == ""


def test_an_unusable_proxy_refuses_the_browser():
    from olano.browser import sessions

    with pytest.raises(sessions.BrowserUnavailable) as err:
        sessions._proxy("ftp://gw.example:21")
    assert "BROWSER_PROXY_URL" in str(err.value)


# ── identity ────────────────────────────────────────────────────────────────

_HEADLESS_PROBE = {
    "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/141.0.7390.37 Safari/537.36",
    "platform": "Linux x86_64",
    "brands": [
        {"brand": "HeadlessChrome", "version": "141"},
        {"brand": "Not?A_Brand", "version": "8"},
        {"brand": "Chromium", "version": "141"},
    ],
    "uaPlatform": "Linux",
    "fullVersionList": [
        {"brand": "HeadlessChrome", "version": "141.0.7390.37"},
        {"brand": "Not?A_Brand", "version": "8.0.0.0"},
        {"brand": "Chromium", "version": "141.0.7390.37"},
    ],
    "fullVersion": "141.0.7390.37",
    "architecture": "x86",
    "bitness": "64",
}


def test_identity_removes_only_the_headless_marker():
    ident = launch.identity_from_probe(_HEADLESS_PROBE)
    assert "Headless" not in ident.user_agent
    assert "Chrome/141.0.0.0 Safari" in ident.user_agent
    assert "X11; Linux x86_64" in ident.user_agent
    assert ident.sec_ch_ua == '"Not?A_Brand";v="8", "Chromium";v="141"'
    assert ident.metadata["platform"] == "Linux"
    assert [b["brand"] for b in ident.metadata["fullVersionList"]] == ["Not?A_Brand", "Chromium"]
    assert ident.override()["acceptLanguage"] == "en-US,en"


def test_identity_without_client_hints_still_names_its_platform():
    ident = launch.identity_from_probe(
        {
            "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) HeadlessChrome/141.0.1.2",
            "platform": "MacIntel",
        }
    )
    assert ident.metadata["platform"] == "macOS"
    assert ident.sec_ch_ua == '"Chromium";v="141"'


def test_context_kwargs():
    ident = launch.identity_from_probe(_HEADLESS_PROBE)
    proxy = {"server": "http://gw.example:1"}
    kw = launch.context_kwargs(headless=True, identity=ident, timezone="Europe/Madrid", proxy=proxy)
    assert kw["user_agent"] == ident.user_agent
    assert kw["extra_http_headers"] == {"sec-ch-ua": ident.sec_ch_ua}
    assert kw["timezone_id"] == "Europe/Madrid"
    assert kw["proxy"] == proxy and kw["proxy"] is not proxy
    live = launch.context_kwargs(headless=False, identity=ident)
    assert "user_agent" not in live and "timezone_id" not in live and "proxy" not in live
    # The pinned New York clock and Mac user agent are gone.
    assert "timezone_id" not in launch.context_kwargs(headless=True)


def test_agent_browsers_launch_without_the_automation_switch():
    assert launch.agent_launch_kwargs()["ignore_default_args"] == ["--enable-automation"]
    args = launch.agent_launch_args()
    assert "--disable-blink-features=AutomationControlled" in args
    assert any(a.startswith("--host-resolver-rules=") and "169.254.*" in a for a in args)


def test_the_live_browser_carries_the_same_metadata_rules():
    """browserd.py runs in its own image and cannot import the runtime."""
    source = (Path(__file__).parents[1] / "docker" / "olano-browser" / "browserd.py").read_text()
    for rule in launch.METADATA_RESOLVER_RULES.split(", "):
        assert rule in source, rule
    assert "--disable-blink-features=AutomationControlled" in source
    # WebRTC stays on the proxy only for a proxied agent: without one the flag
    # would just break calls for a person who takes control.
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in source
    assert 'os.environ.get("OLANO_BROWSER_PROXIED"' in source


# ── timezone setting ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("Europe/Madrid", "Europe/Madrid"),
        ("  America/New_York ", "America/New_York"),
        ("UTC", "UTC"),
        ("", ""),
        (None, ""),
        ("Mars/Olympus", ""),
        ("../../etc/passwd", ""),
    ],
)
def test_browser_timezone_setting(given, kept):
    from olano.config.system_settings import BrowserSettings

    assert BrowserSettings.model_validate({"timezone": given}).timezone == kept


def test_availability_carries_the_timezone():
    from olano.browser.policy import browser_availability
    from olano.config.system_settings import BrowserSettings

    assert browser_availability(BrowserSettings(timezone="Asia/Tokyo")).timezone == "Asia/Tokyo"
