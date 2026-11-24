"""Tests for the lightweight web fetch tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _mock_http_client(response: MagicMock) -> MagicMock:
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=response)
    return mock_client


def test_web_fetch_extracts_html_without_browser() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html; charset=utf-8"}
    mock_response.content = (
        b"<html><head><title>Nasdaq</title></head><body>"
        b"<div class='hidden'>ignore me</div>"
        b"<h1>Option Chain</h1><p>Visible content</p>"
        b"<a href='https://example.com/docs'>Docs</a>"
        b"</body></html>"
    )
    mock_response.url = "https://example.com/final"

    with patch("olano.tools.web_fetch.httpx.Client", return_value=_mock_http_client(mock_response)):
        import olano.tools.web_fetch as wf

        wf._FETCH_CACHE.clear()
        result = wf.web_fetch.invoke({"url": "https://example.com/options"})

    payload = json.loads(result)
    assert payload["status"] == 200
    assert payload["contentType"] == "text/html"
    assert payload["extractor"] == "raw-html"
    assert payload["title"] == "Nasdaq"
    assert payload["finalUrl"] == "https://example.com/final"
    assert "Option Chain" in payload["text"]
    assert "Visible content" in payload["text"]
    assert "ignore me" not in payload["text"]
    assert payload["cached"] is False


def test_web_fetch_converts_markdown_to_plain_text() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/markdown"}
    mock_response.content = b"# Headline\n- Bullet\n[Docs](https://example.com/docs)"
    mock_response.url = "https://example.com/page"

    with patch("olano.tools.web_fetch.httpx.Client", return_value=_mock_http_client(mock_response)):
        import olano.tools.web_fetch as wf

        wf._FETCH_CACHE.clear()
        result = wf.web_fetch.invoke(
            {
                "url": "https://example.com/page",
                "extractMode": "text",
            }
        )

    payload = json.loads(result)
    assert payload["extractor"] == "cf-markdown"
    assert payload["extractMode"] == "text"
    assert payload["text"] == "Headline\nBullet\nDocs"


def test_web_fetch_uses_cache_for_repeat_requests() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}
    mock_response.content = b"<html><body><p>Hello</p></body></html>"
    mock_response.url = "https://example.com/page"

    import olano.tools.web_fetch as wf

    wf._FETCH_CACHE.clear()
    with patch("olano.tools.web_fetch.httpx.Client", return_value=_mock_http_client(mock_response)):
        first = json.loads(wf.web_fetch.invoke({"url": "https://example.com/page"}))

    with patch("olano.tools.web_fetch.httpx.Client") as mock_client_ctor:
        second = json.loads(wf.web_fetch.invoke({"url": "https://example.com/page"}))

    assert first["cached"] is False
    assert second["cached"] is True
    mock_client_ctor.assert_not_called()


def test_web_download_alias_delegates_to_http_download() -> None:
    with patch(
        "olano.tools.http_tools.http_download.func", return_value="Downloaded file"
    ) as mocked:
        from olano.tools.web_fetch import web_download

        result = web_download.invoke({"url": "https://example.com/skill.md"})

    assert result == "Downloaded file"
    mocked.assert_called_once_with(
        url="https://example.com/skill.md",
        filename="",
        timeout=60,
    )


def test_page_read_uses_same_fetch_implementation() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}
    mock_response.content = b"<html><body><p>Hello</p></body></html>"
    mock_response.url = "https://example.com/page"

    with patch("olano.tools.web_fetch.httpx.Client", return_value=_mock_http_client(mock_response)):
        import olano.tools.web_fetch as wf

        wf._FETCH_CACHE.clear()
        result = wf.page_read.invoke({"url": "https://example.com/page"})

    payload = json.loads(result)
    assert payload["text"] == "Hello"
