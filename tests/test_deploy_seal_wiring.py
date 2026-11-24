"""The sealed volume's ceremony path through the all-in-one deploy files.

``deploy/Caddyfile.allinone`` and ``deploy/docker-compose.allinone.yml`` are
text the runtime never parses, so nothing else in the suite would notice a line
going missing from them. The lines pinned here are the whole boundary between a
browser and the sealer's unix socket on a managed box: Caddy strips the two
headers the sealer trusts, asks the
runtime's ``GET /api/system/seal-gate`` (``tests/runtime/test_seal_gate.py``)
and proxies to the socket only on a 2xx. Each test's docstring says what breaks
when its line is lost, because every one of these has a plausible "tidy-up"
that removes it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CADDYFILE = REPO / "deploy" / "Caddyfile.allinone"
COMPOSE = REPO / "deploy" / "docker-compose.allinone.yml"

SOCKET_DIR = "/run/olano-sealer"
SOCKET = f"{SOCKET_DIR}/serve.sock"


def _caddyfile() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


def _seal_block(text: str) -> str:
    """The ``handle @seal`` block: from its matcher to the ``@api`` matcher after it."""
    start = text.index("@seal path /api/seal/*")
    end = text.index("@api {", start)
    return text[start:end]


def test_the_seal_handle_precedes_the_api_handle() -> None:
    """Caddy evaluates handle blocks in file order when their matchers are not
    single paths (``@api`` has three), so ``/api/seal/*`` must be declared first
    or the runtime's catch-all takes it and the socket is never reached."""
    text = _caddyfile()
    assert text.index("@seal path /api/seal/*") < text.index("@api {") < text.index("handle @api")


def test_the_api_matcher_excludes_the_seal_prefix() -> None:
    """The second belt: whatever order Caddy sorts the handles into, the plain
    reverse_proxy to the runtime can never see a ceremony request."""
    text = _caddyfile()
    start = text.index("@api {")
    matcher = text[start : text.index("}", start)]
    assert "path /api/* /openapi.json /.well-known/*" in matcher
    assert "not path /api/seal/*" in matcher


def test_the_seal_block_strips_then_gates_then_proxies_in_a_route() -> None:
    """Order is everything here and only ``route`` guarantees it: Caddy's own
    directive order runs forward_auth BEFORE request_header, so in a plain
    ``handle`` the strip would run after the auth step copied the gate header
    in, and every gated request would reach the sealer without its header."""
    block = _seal_block(_caddyfile())
    assert "route {" in block
    strip_gate = block.index("request_header -X-Olano-Seal-Gate")
    strip_user = block.index("request_header -X-Olano-Seal-User")
    auth = block.index("forward_auth olano:8765")
    proxy = block.index(f"reverse_proxy unix/{SOCKET}")
    assert strip_gate < strip_user < auth < proxy


def test_the_gate_is_the_runtime_route_and_copies_exactly_the_two_headers() -> None:
    block = _seal_block(_caddyfile())
    auth = block[block.index("forward_auth olano:8765") :]
    auth = auth[: auth.index("}")]
    assert "uri /api/system/seal-gate" in auth
    assert "copy_headers X-Olano-Seal-Gate X-Olano-Seal-User" in auth


def test_the_seal_block_never_proxies_to_the_runtime() -> None:
    """The runtime has no ``/api/seal/*`` routes; a ceremony request that landed
    there would 404 (or worse, be answered by a future route by accident)."""
    block = _seal_block(_caddyfile())
    assert "reverse_proxy olano:8765" not in block
    assert block.count("reverse_proxy") == 1


def test_web_mounts_the_socket_directory_read_only_and_the_runtime_does_not() -> None:
    """The directory, not the socket file: the sealer unlinks and recreates the
    socket on every start, and a file bind mount would keep the deleted inode.
    Read-only is enough (connect() is not a write). The runtime container gets
    no mount: it never talks to the sealer, Caddy does, after asking it."""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    web = doc["services"]["web"]["volumes"]
    assert f"{SOCKET_DIR}:{SOCKET_DIR}:ro" in web
    assert not any("olano-sealer" in str(v) for v in doc["services"]["olano"]["volumes"])
