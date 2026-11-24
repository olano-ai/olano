"""Tests for HTTP fetch and download tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_http_fetch_small_response(tmp_path):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}
    mock_response.content = b"Hello world"

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_response)

    with (
        patch("olano.tools.http_tools.httpx.Client", return_value=mock_client),
        patch("olano.tools.http_tools.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.http_tools import http_fetch

        result = http_fetch.invoke({"url": "https://example.com"})

    assert "200" in result
    assert "Hello world" in result


def test_http_fetch_large_response_saved(tmp_path):
    large_body = b"X" * 5000
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = large_body

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_response)

    with (
        patch("olano.tools.http_tools.httpx.Client", return_value=mock_client),
        patch("olano.tools.http_tools.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.http_tools import http_fetch

        result = http_fetch.invoke({"url": "https://example.com/data"})

    assert "saved to" in result.lower() or "Response saved" in result


def test_http_fetch_post_with_json_body(tmp_path):
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.headers = {"content-type": "application/json"}
    mock_response.content = b'{"id": 1}'

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(return_value=mock_response)

    with (
        patch("olano.tools.http_tools.httpx.Client", return_value=mock_client),
        patch("olano.tools.http_tools.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.http_tools import http_fetch

        result = http_fetch.invoke(
            {
                "url": "https://api.example.com/items",
                "method": "POST",
                "body": '{"name": "test"}',
            }
        )

    assert "201" in result


def test_http_fetch_timeout(tmp_path):
    import httpx

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(side_effect=httpx.TimeoutException("timed out"))

    with (
        patch("olano.tools.http_tools.httpx.Client", return_value=mock_client),
        patch("olano.tools.http_tools.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.http_tools import http_fetch

        result = http_fetch.invoke({"url": "https://slow.example.com"})

    assert "timed out" in result.lower()


def test_http_download_saves_file(tmp_path):
    mock_response = MagicMock()
    mock_response.headers = {"content-length": "100"}
    mock_response.raise_for_status = MagicMock()
    mock_response.iter_bytes = MagicMock(return_value=[b"file content here"])
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.stream = MagicMock(return_value=mock_response)

    with (
        patch("olano.tools.http_tools.httpx.Client", return_value=mock_client),
        patch("olano.tools.http_tools.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.http_tools import http_download

        result = http_download.invoke(
            {
                "url": "https://example.com/report.pdf",
                "filename": "report.pdf",
            }
        )

    assert "report.pdf" in result
    assert "Downloaded" in result
