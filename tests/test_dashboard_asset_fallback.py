"""A missing dashboard asset must 404 — never answer with the HTML shell.

The dashboard is a SPA, so every host that serves it falls back to
``index.html`` for unknown paths (that is what makes ``/agents/foo`` deep-link).
Applied to ``/assets/*`` that fallback is a trap: those files are Vite's
content-hashed chunks, and a request for one that is no longer on disk means
the caller is a browser tab still running a build that was deployed over.
Answering *200 + HTML* to a ``<script type=module>`` fetch produces the opaque

    Failed to fetch dynamically imported module:
    https://<host>/assets/AgentDetail-DmZEsNLP.js

instead of a plain 404 — and since ``/assets/*`` is served
``Cache-Control: immutable``, a cache can pin that HTML under the ``.js`` URL.

Three hosts serve this bundle and each has its own fallback rule, so each is
checked here: the Node server (``server.mjs``, exercised for real) and the two
Caddy configs: ``deploy/Caddyfile.allinone`` (the all-in-one boxes behind
``*.olano.run``) and the host-native one written by ``deploy/tc-manual-host.sh``.
Recovery on the client side lives in ``src/core/chunkRecovery.ts``.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "olano" / "dashboard_2.0"


# ── server.mjs — spawn it and ask ─────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def node_dashboard(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run ``server.mjs`` over a minimal public dir; yield its base URL."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")

    public = tmp_path_factory.mktemp("public")
    (public / "index.html").write_text("<!doctype html><title>Olano</title>", encoding="utf-8")
    (public / "assets").mkdir()
    (public / "assets" / "index-AAAAAAAA.js").write_text("export const x = 1;\n", encoding="utf-8")

    port = _free_port()
    proc = subprocess.Popen(
        [node, str(DASHBOARD / "server.mjs")],
        env={
            "PATH": "/usr/bin:/bin",
            "LIONCLAW_DASHBOARD_PORT": str(port),
            "LIONCLAW_DASHBOARD_PUBLIC_DIR": str(public),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.skip("server.mjs exited on start")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.skip("server.mjs did not come up")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get(url: str) -> tuple[int, str, str]:
    """(status, content-type, body) — HTTP errors included, not raised."""
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            return res.status, res.headers.get("Content-Type", ""), res.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode()


def test_node_server_404s_a_missing_hashed_chunk(node_dashboard: str) -> None:
    status, ctype, body = _get(f"{node_dashboard}/assets/AgentDetail-DmZEsNLP.js")
    assert status == 404, "a purged chunk must 404, not resolve to the SPA shell"
    assert "text/html" not in ctype, f"served HTML for a JS module request: {ctype}"
    assert "<!doctype" not in body.lower()


def test_node_server_still_serves_a_present_asset(node_dashboard: str) -> None:
    status, ctype, body = _get(f"{node_dashboard}/assets/index-AAAAAAAA.js")
    assert status == 200
    assert "javascript" in ctype
    assert "export const x" in body


def test_node_server_still_falls_back_for_spa_routes(node_dashboard: str) -> None:
    """The 404 is scoped to /assets/ — deep links must keep booting the app."""
    for route in ("/", "/agents", "/agents/agentfather"):
        status, ctype, body = _get(f"{node_dashboard}{route}")
        assert status == 200, route
        assert "text/html" in ctype, route
        assert "<!doctype" in body.lower(), route


# ── Caddy — config drift guards ───────────────────────────────────────────────


def test_caddy_allinone_serves_assets_without_the_index_fallback() -> None:
    text = (REPO_ROOT / "deploy" / "Caddyfile.allinone").read_text(encoding="utf-8")
    assert "@assets path /assets/*" in text, (
        "the all-in-one Caddyfile must route /assets/* to its own handle; "
        "without it a purged chunk answers 200 with index.html"
    )
    # The @assets handle must NOT carry a try_files fallback of its own.
    block = text.split("handle @assets", 1)[1].split("}", 1)[0]
    assert "file_server" in block
    assert "try_files" not in block, "an /assets/* try_files re-creates the HTML-for-JS bug"


def test_manual_host_caddy_serves_assets_without_the_index_fallback() -> None:
    """The host-native Caddyfile (written by tc-manual-host.sh) needs the same
    split as the all-in-one one. It did not have it: a single ``handle`` block
    ran ``try_files {path} /index.html`` over ``/assets/*`` too, so a purged
    chunk answered 200 + HTML on that host — the exact bug this module exists
    to prevent, on the one host it never covered."""
    text = (REPO_ROOT / "deploy" / "tc-manual-host.sh").read_text(encoding="utf-8")
    assert "@assets path /assets/*" in text, (
        "the host-native Caddyfile must route /assets/* to its own handle; "
        "without it a purged chunk answers 200 with index.html"
    )
    block = text.split("handle @assets", 1)[1].split("\n\thandle {", 1)[0]
    assert "file_server" in block
    assert "try_files" not in block, "an /assets/* try_files re-creates the HTML-for-JS bug"


def test_both_caddy_configs_send_no_cache_on_the_spa_shell() -> None:
    """The SPA handle must set ``Cache-Control: no-cache`` for everything it
    serves — index.html above all.

    This is the origin of the "Importing a module script failed" reports.
    ``Caddyfile.allinone`` set the header on ``/sw.js`` and ``/manifest.json``
    only, leaving the HTML shell with nothing but ETag/Last-Modified. Browsers
    then apply *heuristic freshness* (~10% of the document's age), so a phone
    whose last successful load was weeks old kept serving that index.html from
    its own HTTP cache for days after a deploy — naming chunk hashes that
    ``/srv`` no longer had. ``tc-manual-host.sh`` always did this correctly;
    the two configs had drifted, and the all-in-one is the one customers run.

    Asserted on the SPA handle specifically: ``/assets/*`` is content-hashed
    and served by its own earlier handle, where caching permanently is right.
    """
    for name, marker in (
        ("Caddyfile.allinone", "try_files {path} /index.html"),
        ("tc-manual-host.sh", "try_files {path} /index.html"),
    ):
        text = (REPO_ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert marker in text, f"{name}: no SPA fallback found — did the handle move?"
        # The header has to be in the same block as the fallback, and before it.
        block = text.rsplit("handle {", 1)[1].split(marker, 1)[0]
        assert 'Cache-Control "no-cache"' in block, (
            f"{name}: the SPA handle must send Cache-Control: no-cache so a deploy "
            "is picked up on the next load instead of after a heuristic TTL"
        )
