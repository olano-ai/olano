"""Pure rules behind deployed pages: the deploy spec, the proxy, releases, policy."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from olano.pages import policy, proxy, releases
from olano.pages.spec import (
    KIND_SERVICE,
    KIND_STATIC,
    DeploySpec,
    SpecError,
    build_spec,
)

# ── deploy spec ──────────────────────────────────────────────────────────────


def test_service_and_static_kinds():
    assert build_spec(start="node server.js").kind == KIND_SERVICE
    assert build_spec(build="npm run build", output="dist").kind == KIND_STATIC
    both = build_spec(build="npm ci", start="node server.js")
    assert both.kind == KIND_SERVICE and both.build == "npm ci"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "build command, a start command"),
        ({"build": "npm run build"}, "needs output"),
        ({"start": "node s.js", "output": "dist"}, "static builds only"),
        ({"build": "x", "output": "../up"}, "without '..'"),
        ({"build": "x", "output": "/abs"}, "without '..'"),
        ({"start": "a\nb"}, "one line"),
        ({"start": "a\x00"}, "NUL"),
        ({"start": "x" * 4001}, "longer than"),
        ({"start": "node s.js", "port": 80}, "between 1024"),
        ({"build": "x", "output": "dist", "port": 3000}, "services only"),
        ({"start": "s", "healthcheck": "health"}, "healthcheck"),
        ({"start": "s", "healthcheck": "//evil"}, "healthcheck"),
        ({"start": "s", "build_timeout": 5}, "between 30"),
        ({"start": "s", "startup_timeout": 9999}, "between 5"),
        ({"start": "s", "env": {"PATH": "/x"}}, "set by the runtime"),
        ({"start": "s", "env": {"OLANO_X": "1"}}, "set by the runtime"),
        ({"start": "s", "env": {"1BAD": "1"}}, "not a valid"),
        ({"start": "s", "env": {"NODE_OPTIONS": "--require /x"}}, "set by the runtime"),
    ],
)
def test_invalid_specs_are_refused_with_a_reason(kwargs, message):
    with pytest.raises(SpecError, match=message):
        build_spec(**kwargs)


def test_defaults_and_digest():
    spec = build_spec(start="node s.js", env={"NODE_ENV": "production"})
    assert spec.port == 0 and spec.healthcheck == "/" and spec.strip_prefix is True
    assert spec.build_timeout == 600 and spec.startup_timeout == 60
    same = build_spec(start="node s.js", env={"NODE_ENV": "production"})
    assert spec.digest() == same.digest()
    assert spec.digest() != build_spec(start="node s2.js").digest()


def test_from_dict_is_lenient_about_unknown_keys():
    spec = build_spec(build="b", start="s")
    stored = {**spec.to_dict(), "future_field": 1}
    assert DeploySpec.from_dict(stored) == spec
    assert DeploySpec.from_dict(None) is None
    assert DeploySpec.from_dict({"port": "not-a-number", "unexpected": object()}) is not None


# ── proxy: requests ──────────────────────────────────────────────────────────


def _req(headers, **kw):
    out = proxy.request_headers(
        headers,
        slug=kw.get("slug", "crm"),
        port=kw.get("port", 4100),
        viewer=kw.get("viewer", "owner"),
        client_ip=kw.get("client_ip", "203.0.113.9"),
        scheme="https",
        host="box.example",
    )
    return {k.decode().lower(): v.decode() for k, v in out}, out


def test_request_never_carries_credentials_or_spoofed_identity():
    headers, raw = _req(
        [
            ("Authorization", "Basic b3duZXI6cGFzcw=="),
            ("Cookie", "lc_session=abc; theme=dark; __Host-lc_session=xyz"),
            ("X-Olano-Viewer", "admin"),
            ("X-Forwarded-For", "1.2.3.4"),
            ("Host", "box.example"),
            ("Connection", "keep-alive, X-Secret"),
            ("X-Secret", "hop"),
            ("Accept", "text/html"),
        ]
    )
    assert "authorization" not in headers
    assert headers["cookie"] == "theme=dark"
    assert headers["x-olano-viewer"] == "owner"
    assert headers["x-forwarded-for"] == "203.0.113.9"
    assert headers["host"] == "127.0.0.1:4100"
    assert headers["x-forwarded-prefix"] == "/pages/crm"
    assert headers["x-forwarded-host"] == "box.example"
    assert "x-secret" not in headers and "connection" not in headers
    assert headers["accept"] == "text/html"
    assert sum(1 for k, _ in raw if k.lower() == b"x-olano-viewer") == 1


def test_request_drops_cookie_header_when_only_dashboard_cookies():
    headers, _ = _req([("Cookie", "lc_session=abc")])
    assert "cookie" not in headers


def test_chunked_request_stays_chunked():
    headers, _ = _req([("Transfer-Encoding", "chunked")])
    assert headers["transfer-encoding"] == "chunked"
    headers, _ = _req([("Transfer-Encoding", "chunked"), ("Content-Length", "5")])
    assert "transfer-encoding" not in headers


def test_reserved_cookie_names_match_the_dashboard():
    from olano.runtime import auth

    assert auth.SESSION_COOKIE.lower() in proxy.RESERVED_COOKIES
    assert auth.SESSION_COOKIE_SECURE.lower() in proxy.RESERVED_COOKIES


# ── proxy: responses ─────────────────────────────────────────────────────────


def _resp(headers, slug="crm", port=4100):
    raw = [(k.encode(), v.encode()) for k, v in headers]
    return proxy.response_headers(raw, slug=slug, port=port)


def test_response_drops_origin_wide_headers():
    out = dict(
        (k.lower(), v)
        for k, v in _resp(
            [
                ("Clear-Site-Data", '"*"'),
                ("Service-Worker-Allowed", "/"),
                ("WWW-Authenticate", 'Basic realm="x"'),
                ("Strict-Transport-Security", "max-age=1"),
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Credentials", "true"),
                ("X-Olano-Anything", "1"),
                ("Transfer-Encoding", "chunked"),
                ("Content-Type", "text/html"),
            ]
        )
    )
    for name in (
        "clear-site-data",
        "service-worker-allowed",
        "www-authenticate",
        "strict-transport-security",
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "x-olano-anything",
        "transfer-encoding",
    ):
        assert name not in out
    assert out["content-type"] == "text/html"
    assert out["cache-control"] == "private"
    assert out["x-content-type-options"] == "nosniff"


def test_cache_control_never_public():
    out = dict((k, v) for k, v in _resp([("Cache-Control", "public, max-age=60, s-maxage=600")]))
    assert out["cache-control"] == "private, max-age=60"


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        ("sid=1; Path=/; HttpOnly", "sid=1; Path=/pages/crm/; HttpOnly"),
        ("sid=1", "sid=1; Path=/pages/crm/"),
        ("sid=1; Path=/api", "sid=1; Path=/pages/crm/api"),
        ("sid=1; Path=/pages/crm/x", "sid=1; Path=/pages/crm/x"),
        ("sid=1; Domain=example.com; Path=/", "sid=1; Path=/pages/crm/"),
        ("sid=1; Path=/pages/other/", "sid=1; Path=/pages/crm/pages/other/"),
        ("lc_session=evil; Path=/", None),
        ("__Host-lc_session=evil; Path=/", None),
        ("__Host-anything=1; Path=/; Secure", None),
        ("garbage", None),
    ],
)
def test_set_cookie_is_confined_to_the_page(cookie, expected):
    assert proxy.rewrite_set_cookie(cookie, slug="crm") == expected


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/login", "/pages/crm/login"),
        ("/pages/crm/login", "/pages/crm/login"),
        ("login", "login"),
        ("../x", "../x"),
        ("http://127.0.0.1:4100/after?x=1", "/pages/crm/after?x=1"),
        ("http://localhost:4100/", "/pages/crm/"),
        ("https://example.com/out", "https://example.com/out"),
        ("http://127.0.0.1:9999/other", "http://127.0.0.1:9999/other"),
        ("//evil.example/x", "//evil.example/x"),
    ],
)
def test_location_stays_under_the_page(location, expected):
    assert proxy.rewrite_location(location, slug="crm", port=4100) == expected


# ── proxy: CSRF, targets, SPA ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "headers", "blocked"),
    [
        ("GET", {"sec-fetch-site": "cross-site"}, False),
        ("POST", {"sec-fetch-site": "same-origin"}, False),
        ("POST", {"sec-fetch-site": "none"}, False),
        ("POST", {"sec-fetch-site": "cross-site"}, True),
        ("POST", {"sec-fetch-site": "same-site"}, True),
        ("DELETE", {"origin": "https://box.example"}, False),
        ("DELETE", {"origin": "https://evil.example"}, True),
        ("PUT", {"origin": "null"}, True),
        ("POST", {}, False),
    ],
)
def test_cross_site_unsafe(method, headers, blocked):
    assert proxy.cross_site_unsafe(method, headers, "box.example") is blocked


def test_upstream_target():
    assert (
        proxy.upstream_target(b"/pages/crm/api/x", b"a=1", slug="crm", strip_prefix=True)
        == b"/api/x?a=1"
    )
    assert proxy.upstream_target(b"/pages/crm/", b"", slug="crm", strip_prefix=True) == b"/"
    assert (
        proxy.upstream_target(b"/pages/crm/api", b"", slug="crm", strip_prefix=False)
        == b"/pages/crm/api"
    )
    assert proxy.upstream_target(b"/pages/crmx/", b"", slug="crm", strip_prefix=True) is None
    assert proxy.upstream_target(b"/pages/%63rm/", b"", slug="crm", strip_prefix=True) is None


def test_is_navigation():
    assert proxy.is_navigation("settings/profile", "text/html")
    assert proxy.is_navigation("settings", "*/*")
    assert not proxy.is_navigation("assets/main.js", "*/*")
    assert proxy.is_navigation("report.v2", "text/html,application/xhtml+xml")


def test_status_page_escapes():
    page = proxy.status_page("<b>", "<script>x</script>", refresh=3)
    assert "<script>x" not in page and "&lt;script&gt;" in page
    assert 'content="3"' in page


# ── releases ─────────────────────────────────────────────────────────────────


def test_release_snapshot_skips_symlinks_and_switches_atomically(tmp_path):
    out = tmp_path / "dist"
    (out / "assets").mkdir(parents=True)
    (out / "index.html").write_text("v1")
    (out / "assets" / "a.js").write_text("js")
    secret = tmp_path / "secret.txt"
    secret.write_text("vault")
    os.symlink(secret, out / "leak.txt")
    os.symlink(tmp_path, out / "up")
    info = releases.snapshot_release("crm", out)
    assert info.files == 2 and info.skipped_symlinks == 2 and info.has_index
    assert releases.current_release_dir("crm") is None  # not active until switched
    releases.activate_release("crm", info.id)
    live = releases.current_release_dir("crm")
    assert (live / "index.html").read_text() == "v1"
    assert not (live / "leak.txt").exists() and not (live / "up").exists()
    # The release is a copy: rebuilding dist does not change what is served.
    (out / "index.html").write_text("v2-half-written")
    assert (live / "index.html").read_text() == "v1"


def test_release_prune_keeps_active_and_newest(tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    (out / "index.html").write_text("x")
    ids = []
    for _ in range(5):
        info = releases.snapshot_release("crm", out)
        ids.append(info.id)
    releases.activate_release("crm", ids[0])
    removed = releases.prune_releases("crm", keep=2)
    kept = releases.list_releases("crm")
    assert ids[0] in kept and ids[-1] in kept and ids[-2] in kept
    assert set(removed) == {ids[1], ids[2]}


def test_release_refuses_empty_and_oversized(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(releases.ReleaseError, match="empty"):
        releases.snapshot_release("crm", empty)
    with pytest.raises(releases.ReleaseError, match="not a directory"):
        releases.snapshot_release("crm", tmp_path / "missing")
    big = tmp_path / "big"
    big.mkdir()
    for i in range(5):
        (big / f"f{i}").write_text("x")
    monkeypatch.setattr(releases, "MAX_RELEASE_FILES", 3)
    with pytest.raises(releases.ReleaseError, match="more than 3 files"):
        releases.snapshot_release("crm", big)
    assert releases.list_releases("crm") == []


def test_current_release_rejects_a_tampered_pointer(tmp_path):
    from olano.pages.paths import current_release_file

    path = current_release_file("crm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("../../etc\n")
    assert releases.current_release_dir("crm") is None


# ── policy ───────────────────────────────────────────────────────────────────


def _cfg(tools=(), backend="filesystem"):
    return SimpleNamespace(tools=list(tools), backend=SimpleNamespace(type=backend))


def test_policy_requires_shell_access(monkeypatch):
    monkeypatch.setattr(policy, "live_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "_registered_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "host_execution_forbidden", lambda: False)
    assert policy.command_denial("a", _cfg()) == policy.NO_SHELL
    assert policy.command_denial("a", _cfg(tools=["execute"])) is None
    assert policy.command_denial("a", _cfg(backend="filesystem+shell")) is None


def test_policy_managed_box_needs_a_sandbox(monkeypatch):
    monkeypatch.setattr(policy, "host_execution_forbidden", lambda: True)
    monkeypatch.setattr(policy, "_registered_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "live_sandbox", lambda agent: None)
    assert policy.command_denial("a", _cfg(tools=["execute"])) == policy.NO_SANDBOX_MANAGED
    assert policy.execution_blocker("a") == policy.NO_SANDBOX_MANAGED
    # A configured sandbox that is down: accepted, but nothing runs until it is back.
    monkeypatch.setattr(policy, "_registered_sandbox", lambda agent: object())
    assert policy.command_denial("a", _cfg(tools=["execute"])) is None
    assert policy.execution_blocker("a") == policy.SANDBOX_DOWN
    # A live sandbox grants shell by itself.
    monkeypatch.setattr(policy, "live_sandbox", lambda agent: object())
    assert policy.command_denial("a", _cfg()) is None
    assert policy.execution_blocker("a") is None


def test_policy_open_box_without_sandbox_runs_on_host(monkeypatch):
    monkeypatch.setattr(policy, "host_execution_forbidden", lambda: False)
    monkeypatch.setattr(policy, "_registered_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "live_sandbox", lambda agent: None)
    assert policy.execution_blocker("a") is None


def test_policy_switch(monkeypatch):
    from olano.config.system_settings import set_pages_settings

    set_pages_settings({"services_enabled": False})
    assert policy.command_denial("a", _cfg(tools=["execute"])) == policy.SERVICES_DISABLED
