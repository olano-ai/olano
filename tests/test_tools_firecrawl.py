"""Tests for Firecrawl scraping tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_firecrawl_scrape_no_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with patch("olano.tools.firecrawl.get_workspace") as mock_ws:
        mock_ws.return_value.root = tmp_path
        from olano.tools.firecrawl import firecrawl_scrape

        result = firecrawl_scrape.invoke({"url": "https://example.com"})

    assert "FIRECRAWL_API_KEY" in result


def test_firecrawl_scrape_rejects_local_pdf_without_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    from olano.tools.firecrawl import firecrawl_scrape

    result = firecrawl_scrape.invoke({"url": f"file://{tmp_path}/report.pdf"})

    assert "document_to_text" in result
    assert "pdf_to_text" in result


def test_firecrawl_scrape_returns_markdown(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    mock_result = MagicMock()
    mock_result.markdown = "# Hello\nThis is content."
    mock_app = MagicMock()
    mock_app.scrape_url.return_value = mock_result

    with (
        patch("olano.tools.firecrawl.FirecrawlApp", return_value=mock_app),
        patch("olano.tools.firecrawl.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.firecrawl import firecrawl_scrape

        result = firecrawl_scrape.invoke({"url": "https://example.com", "save_to_workspace": False})

    assert "Hello" in result


def test_firecrawl_scrape_saves_to_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    mock_result = MagicMock()
    mock_result.markdown = "# Test page"
    mock_app = MagicMock()
    mock_app.scrape_url.return_value = mock_result

    with (
        patch("olano.tools.firecrawl.FirecrawlApp", return_value=mock_app),
        patch("olano.tools.firecrawl.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.firecrawl import firecrawl_scrape

        result = firecrawl_scrape.invoke({"url": "https://example.com", "save_to_workspace": True})

    assert "saved to" in result.lower() or "web_scraping" in result


def test_firecrawl_map_returns_urls(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    mock_result = MagicMock()
    mock_result.links = ["https://example.com/a", "https://example.com/b"]
    mock_app = MagicMock()
    mock_app.map_url.return_value = mock_result

    with (
        patch("olano.tools.firecrawl.FirecrawlApp", return_value=mock_app),
        patch("olano.tools.firecrawl.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.firecrawl import firecrawl_map

        result = firecrawl_map.invoke({"url": "https://example.com"})

    assert "https://example.com/a" in result
    assert "2 URLs" in result


def test_firecrawl_extract_returns_json(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    mock_result = MagicMock()
    mock_result.data = {"price": "$99", "title": "Product"}
    mock_app = MagicMock()
    mock_app.extract.return_value = mock_result

    with (
        patch("olano.tools.firecrawl.FirecrawlApp", return_value=mock_app),
        patch("olano.tools.firecrawl.get_workspace") as mock_ws,
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.firecrawl import firecrawl_extract

        result = firecrawl_extract.invoke(
            {
                "urls": "https://example.com/product",
                "prompt": "Extract product price and title",
            }
        )

    assert "$99" in result
    assert "Product" in result
