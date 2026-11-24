"""Tests for Tavily web search tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_web_search_no_api_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    from olano.tools.web_search import web_search

    result = web_search.invoke({"query": "test"})
    assert "TAVILY_API_KEY" in result


def test_web_search_returns_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "answer": "Python is a language",
        "results": [
            {
                "title": "Python docs",
                "url": "https://python.org",
                "content": "Python programming language",
            },
        ],
    }

    with patch("olano.tools.web_search.TavilyClient", return_value=mock_client):
        from olano.tools.web_search import web_search

        result = web_search.invoke({"query": "Python", "max_results": 3})

    assert "Python docs" in result
    assert "https://python.org" in result


def test_web_search_no_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.search.return_value = {"results": []}

    with patch("olano.tools.web_search.TavilyClient", return_value=mock_client):
        from olano.tools.web_search import web_search

        result = web_search.invoke({"query": "xyzzy12345"})

    assert "No results" in result


def test_web_extract_no_api_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    from olano.tools.web_search import web_extract

    result = web_extract.invoke({"url": "https://example.com"})
    assert "TAVILY_API_KEY" in result


def test_web_extract_returns_content(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.extract.return_value = {
        "results": [{"raw_content": "Hello world content", "content": "Hello world"}]
    }

    with patch("olano.tools.web_search.TavilyClient", return_value=mock_client):
        from olano.tools.web_search import web_extract

        result = web_extract.invoke({"url": "https://example.com"})

    assert "Hello world content" in result


def test_web_search_clamps_max_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.search.return_value = {"results": []}

    with patch("olano.tools.web_search.TavilyClient", return_value=mock_client):
        from olano.tools.web_search import web_search

        web_search.invoke({"query": "test", "max_results": 100})

    call_kwargs = mock_client.search.call_args
    assert call_kwargs.kwargs["max_results"] == 10


def test_web_search_records_a_byok_service_call(monkeypatch):
    """Every web call lands in the service ledger, priced or not.

    Search spends money without producing a token, so the LLM usage ledger
    never sees it — this is the only record Insights can count it from.
    """
    from olano.analytics import service_ledger

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_client = MagicMock()
    mock_client.search.return_value = {"results": [{"title": "t", "url": "u", "content": "c"}]}

    with patch("olano.tools.web_search.TavilyClient", return_value=mock_client):
        from olano.tools.web_search import web_search

        web_search.invoke({"query": "Python"})

    events = service_ledger.read_service_events()
    assert [(e.service, e.billing_source, e.credits) for e in events] == [
        ("web_search", "customer", None)
    ]


def test_managed_web_search_records_the_cloud_charge(monkeypatch):
    """A managed call carries the cloud's own per-call credit figure."""
    from olano.analytics import service_ledger
    from olano.deployment import services_client

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(services_client, "services_available", lambda: True)
    monkeypatch.setattr(
        services_client,
        "service_search",
        lambda **kw: {
            "results": [{"title": "t", "url": "u", "excerpts": ["e"]}],
            "credits_charged": 3.5,
        },
    )

    from olano.tools.web_search import web_search

    web_search.invoke({"query": "Python"})

    events = service_ledger.read_service_events()
    assert [(e.service, e.billing_source, e.credits) for e in events] == [
        ("web_search", "olano", 3.5)
    ]
