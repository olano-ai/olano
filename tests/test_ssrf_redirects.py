"""The SSRF decision applies to every redirect hop, not only the first URL.

``assert_safe_outbound_url`` used to run once, on the URL the model asked for;
``follow_redirects=True`` then took the client wherever the public server
pointed, private and metadata addresses included. ``redirect_guard`` is an
httpx request hook that repeats the decision on each request the client makes.
"""

from __future__ import annotations

import os
from functools import partial
from unittest.mock import patch

import httpx
import pytest

from olano.security.ssrf import SSRFBlockedError, redirect_guard

PUBLIC = "https://public.example"
PRIVATE = "http://127.0.0.1:9/secret"


def _transport(seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path == "/to-private":
            return httpx.Response(302, headers={"location": PRIVATE})
        if path == "/to-public":
            return httpx.Response(302, headers={"location": f"{PUBLIC}/final"})
        if path == "/final":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="landed")
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="hello")

    return httpx.MockTransport(handler)


def test_guard_refuses_a_redirect_to_a_private_address():
    seen: list[str] = []
    hooks = {"request": [redirect_guard()]}
    with (
        httpx.Client(transport=_transport(seen), follow_redirects=True, event_hooks=hooks) as c,
        pytest.raises(SSRFBlockedError),
    ):
        c.get(f"{PUBLIC}/to-private")
    # The private hop was decided before it was sent.
    assert seen == [f"{PUBLIC}/to-private"]


def test_guard_follows_a_redirect_to_a_public_address():
    seen: list[str] = []
    hooks = {"request": [redirect_guard()]}
    with httpx.Client(transport=_transport(seen), follow_redirects=True, event_hooks=hooks) as c:
        resp = c.get(f"{PUBLIC}/to-public")
    assert resp.status_code == 200 and resp.text == "landed"
    assert seen == [f"{PUBLIC}/to-public", f"{PUBLIC}/final"]


def test_guard_honours_the_blocklist_on_a_hop():
    seen: list[str] = []
    hooks = {"request": [redirect_guard(blocklist=["public.example"])]}
    with (
        httpx.Client(transport=_transport(seen), follow_redirects=True, event_hooks=hooks) as c,
        pytest.raises(SSRFBlockedError),
    ):
        c.get(f"{PUBLIC}/to-public")


def test_web_fetch_refuses_a_redirect_to_a_private_address():
    import olano.tools.web_fetch as wf

    seen: list[str] = []
    wf._FETCH_CACHE.clear()
    with patch.object(wf.httpx, "Client", partial(httpx.Client, transport=_transport(seen))):
        result = wf.web_fetch.invoke({"url": f"{PUBLIC}/to-private"})
    assert result.startswith("Error:") and "Blocked" in result
    assert seen == [f"{PUBLIC}/to-private"]


def test_web_fetch_still_follows_a_public_redirect():
    import olano.tools.web_fetch as wf

    seen: list[str] = []
    wf._FETCH_CACHE.clear()
    with patch.object(wf.httpx, "Client", partial(httpx.Client, transport=_transport(seen))):
        result = wf.web_fetch.invoke({"url": f"{PUBLIC}/to-public"})
    assert "landed" in result
    assert seen == [f"{PUBLIC}/to-public", f"{PUBLIC}/final"]


def test_http_fetch_refuses_a_redirect_to_a_private_address(tmp_path):
    import olano.tools.http_tools as ht

    seen: list[str] = []
    with (
        patch.object(ht.httpx, "Client", partial(httpx.Client, transport=_transport(seen))),
        patch.object(ht, "get_workspace") as ws,
    ):
        ws.return_value.root = tmp_path
        result = ht.http_fetch.invoke({"url": f"{PUBLIC}/to-private"})
    assert result.startswith("Error:") and "Blocked" in result
    assert seen == [f"{PUBLIC}/to-private"]


def test_file_download_refuses_a_redirect_to_a_private_address(tmp_path):
    import olano.tools.fileops_tools as fo

    seen: list[str] = []
    dest = tmp_path / "out.bin"
    with patch("httpx.Client", partial(httpx.Client, transport=_transport(seen))):
        result = fo.file_download.invoke({"url": f"{PUBLIC}/to-private", "destination": str(dest)})
    assert "Blocked" in result
    assert seen == [f"{PUBLIC}/to-private"]
    assert not dest.exists() or dest.stat().st_size == 0


def test_file_download_refuses_a_private_first_url(tmp_path):
    """This tool had no outbound check at all before the guard."""
    import olano.tools.fileops_tools as fo

    seen: list[str] = []
    dest = tmp_path / "out.bin"
    with patch("httpx.Client", partial(httpx.Client, transport=_transport(seen))):
        result = fo.file_download.invoke({"url": PRIVATE, "destination": str(dest)})
    assert "Blocked" in result
    assert seen == []


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_private_file_helper_writes_owner_only(tmp_path):
    from olano.security.private_files import private_dir, write_private_text

    d = private_dir(tmp_path / "secrets")
    assert d.stat().st_mode & 0o777 == 0o700
    target = d / "tok.json"
    write_private_text(target, "{}")
    assert target.read_text() == "{}"
    assert target.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in d.iterdir()] == ["tok.json"]  # no temp file left behind
    # Overwriting keeps the mode and the content is replaced atomically.
    write_private_text(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'
    assert target.stat().st_mode & 0o777 == 0o600


def test_image_loader_refuses_a_redirect_to_a_private_address():
    import olano.tools.image_tools as it

    seen: list[str] = []
    with (
        patch.object(it.httpx, "Client", partial(httpx.Client, transport=_transport(seen))),
        pytest.raises(SSRFBlockedError),
    ):
        it._load_image_bytes(f"{PUBLIC}/to-private")
    assert seen == [f"{PUBLIC}/to-private"]


def test_rag_url_fetch_refuses_private_addresses():
    from olano.memory.rag_memory import RAGMemory

    seen: list[str] = []
    with (
        patch("olano.tools.firecrawl.firecrawl_scrape") as fc,
        patch("httpx.Client", partial(httpx.Client, transport=_transport(seen))),
    ):
        fc.invoke.return_value = "Error: no key"
        with pytest.raises(SSRFBlockedError):
            RAGMemory._fetch_url(None, PRIVATE)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Blocked"):
            RAGMemory._fetch_url(None, f"{PUBLIC}/to-private")  # type: ignore[arg-type]
    assert seen == [f"{PUBLIC}/to-private"]


def test_file_download_refusal_leaves_no_directories_behind(tmp_path):
    import olano.tools.fileops_tools as fo

    dest = tmp_path / "reports" / "q3" / "out.bin"
    result = fo.file_download.invoke({"url": PRIVATE, "destination": str(dest)})
    assert "Blocked" in result
    assert not (tmp_path / "reports").exists()


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_mcp_server_stores_are_owner_only(tmp_path):
    from olano.mcp_server.tokens import McpTokenStore

    store = McpTokenStore(tmp_path / "srv" / "tokens.json")
    store.mint(label="t", agents=["*"], ttl_days=None)  # any write
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in store.path.parent.iterdir()] == ["tokens.json"]
