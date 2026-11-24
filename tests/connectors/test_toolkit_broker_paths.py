"""Toolkit-side broker paths.

The OAuth connector broker exposes live tokens through
``olano.tools._credentials.get_credential``, which falls through to
``os.environ`` when nothing is connected — so these tests inject
broker-shaped credentials with ``monkeypatch.setenv`` and assert each
toolkit prefers the new user-context path, while keeping its legacy path
unchanged when the broker credential is absent.

Covered:
- youtube: ``YOUTUBE_API_KEY`` stays primary; Google-OAuth ``Authorization:
  Bearer`` fallback; missing-credentials guidance mentions the Connections tab.
- reddit: ``REDDIT_REFRESH_TOKEN`` → ``praw.Reddit(refresh_token=…)`` for
  reads AND writes; app-only / username+password fallbacks intact.
- x_twitter: ``X_USER_ACCESS_TOKEN`` → ``tweepy.Client(bearer_token=…)`` with
  ``user_auth=False`` on user-context calls; OAuth 1.0a kwargs intact.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

# ── youtube ───────────────────────────────────────────────────────────────────


class _CapturingHttpxGet:
    """Stand-in for ``httpx.get`` recording url/params/headers per call."""

    def __init__(self, payload: dict | None = None):
        self.calls: list[dict[str, Any]] = []
        self._payload = payload if payload is not None else {"items": []}

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        payload = self._payload
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def test_youtube_api_key_stays_primary(monkeypatch):
    from olano.tools import google_tools, youtube_tools

    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-key-123")

    def _fail():
        raise AssertionError("Google OAuth fallback must not run when the key is set")

    monkeypatch.setattr(google_tools, "_get_credentials", _fail)
    fake_get = _CapturingHttpxGet()
    monkeypatch.setattr(youtube_tools.httpx, "get", fake_get)

    out = youtube_tools.youtube_search.invoke({"query": "lions"})

    assert "No videos found" in out
    (call,) = fake_get.calls
    assert call["params"]["key"] == "yt-key-123"
    assert call["params"]["q"] == "lions"
    assert "Authorization" not in call["headers"]


def test_youtube_falls_back_to_google_oauth_bearer(monkeypatch):
    from olano.tools import google_tools, youtube_tools

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(google_tools, "_get_credentials", lambda: SimpleNamespace(token="goog-tok"))
    fake_get = _CapturingHttpxGet(
        {"items": [{"id": {"videoId": "v1"}, "snippet": {"title": "T", "channelTitle": "C"}}]}
    )
    monkeypatch.setattr(youtube_tools.httpx, "get", fake_get)

    out = youtube_tools.youtube_search.invoke({"query": "lions"})

    assert "v1" in out
    (call,) = fake_get.calls
    assert call["headers"]["Authorization"] == "Bearer goog-tok"
    assert "key" not in call["params"]
    assert call["params"]["q"] == "lions"  # regular query params still flow


def test_youtube_no_key_no_google_mentions_connections(monkeypatch):
    from olano.tools import google_tools, youtube_tools

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    def _no_creds():
        raise RuntimeError("Google credentials not found. Run: olano auth google")

    monkeypatch.setattr(google_tools, "_get_credentials", _no_creds)
    fake_get = _CapturingHttpxGet()
    monkeypatch.setattr(youtube_tools.httpx, "get", fake_get)

    out = youtube_tools.youtube_get_analytics.invoke({"video_id": "abc"})

    assert out.startswith("[missing-credentials]")
    assert "Connections" in out
    assert not fake_get.calls  # no request goes out without auth


# ── reddit ────────────────────────────────────────────────────────────────────


class _FakePraw:
    """Fake ``praw`` module whose ``Reddit`` records construction kwargs."""

    def __init__(self):
        self.reddit_kwargs: dict[str, Any] | None = None

    def Reddit(self, **kwargs):
        self.reddit_kwargs = kwargs
        return SimpleNamespace(fake=True)


@pytest.fixture
def fake_praw(monkeypatch):
    from olano.tools import reddit_tools

    module = _FakePraw()
    monkeypatch.setattr(reddit_tools, "optional_import", lambda *a, **k: (module, None))
    return module


def _clear_reddit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_REFRESH_TOKEN",
        "REDDIT_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_reddit_refresh_token_builds_user_context_client(monkeypatch, fake_praw):
    from olano.tools import reddit_tools

    _clear_reddit_env(monkeypatch)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "rtok")

    reddit, err = reddit_tools._reddit()

    assert err is None
    assert reddit is not None
    assert fake_praw.reddit_kwargs == {
        "client_id": "cid",
        "client_secret": "csecret",
        "user_agent": "olano-bot/1.0",
        "refresh_token": "rtok",
    }


def test_reddit_refresh_token_covers_writes_and_beats_password(monkeypatch, fake_praw):
    from olano.tools import reddit_tools

    _clear_reddit_env(monkeypatch)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "rtok")
    # Legacy write creds present too — the connected account must win.
    monkeypatch.setenv("REDDIT_USERNAME", "user")
    monkeypatch.setenv("REDDIT_PASSWORD", "pass")

    reddit, err = reddit_tools._reddit(write=True)

    assert err is None
    assert reddit is not None
    assert fake_praw.reddit_kwargs is not None
    assert fake_praw.reddit_kwargs["refresh_token"] == "rtok"
    assert "username" not in fake_praw.reddit_kwargs
    assert "password" not in fake_praw.reddit_kwargs


def test_reddit_without_refresh_token_keeps_legacy_paths(monkeypatch, fake_praw):
    from olano.tools import reddit_tools

    _clear_reddit_env(monkeypatch)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "csecret")

    # Read path: app-only client, exactly the old kwargs.
    reddit, err = reddit_tools._reddit()
    assert err is None and reddit is not None
    assert fake_praw.reddit_kwargs == {
        "client_id": "cid",
        "client_secret": "csecret",
        "user_agent": "olano-bot/1.0",
    }

    # Write path still requires username/password …
    reddit, err = reddit_tools._reddit(write=True)
    assert reddit is None
    assert err is not None and err.startswith("[missing-credentials]")
    assert "Connections" in err

    # … and uses them when present (password grant, as before).
    monkeypatch.setenv("REDDIT_USERNAME", "user")
    monkeypatch.setenv("REDDIT_PASSWORD", "pass")
    reddit, err = reddit_tools._reddit(write=True)
    assert err is None and reddit is not None
    assert fake_praw.reddit_kwargs is not None
    assert fake_praw.reddit_kwargs["username"] == "user"
    assert fake_praw.reddit_kwargs["password"] == "pass"
    assert "refresh_token" not in fake_praw.reddit_kwargs


def test_reddit_missing_client_creds_mentions_connections(monkeypatch, fake_praw):
    from olano.tools import reddit_tools

    _clear_reddit_env(monkeypatch)

    reddit, err = reddit_tools._reddit()

    assert reddit is None
    assert err is not None and err.startswith("[missing-credentials]")
    assert "Connections" in err


# ── x_twitter ─────────────────────────────────────────────────────────────────


class _FakeTweepyClient:
    """Stand-in for ``tweepy.Client`` capturing init kwargs + method calls."""

    instances: list[_FakeTweepyClient] = []

    def __init__(
        self,
        bearer_token=None,
        consumer_key=None,
        consumer_secret=None,
        access_token=None,
        access_token_secret=None,
        **kwargs,
    ):
        provided = {
            "bearer_token": bearer_token,
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
            "access_token": access_token,
            "access_token_secret": access_token_secret,
            **kwargs,
        }
        self.init_kwargs = {k: v for k, v in provided.items() if v is not None}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        _FakeTweepyClient.instances.append(self)

    def create_tweet(self, **kwargs):
        self.calls.append(("create_tweet", kwargs))
        return SimpleNamespace(data={"id": "111"})

    def get_me(self, **kwargs):
        self.calls.append(("get_me", kwargs))
        return SimpleNamespace(data=SimpleNamespace(id="42"))

    def get_users_tweets(self, user_id, **kwargs):
        self.calls.append(("get_users_tweets", {"user_id": user_id, **kwargs}))
        return SimpleNamespace(data=[SimpleNamespace(id="1", text="hello")])

    def like(self, **kwargs):
        self.calls.append(("like", kwargs))
        return SimpleNamespace(data={"liked": True})

    def search_recent_tweets(self, query, **kwargs):
        self.calls.append(("search_recent_tweets", {"query": query, **kwargs}))
        return SimpleNamespace(data=None)


@pytest.fixture
def fake_tweepy(monkeypatch):
    tweepy = pytest.importorskip("tweepy")
    _FakeTweepyClient.instances = []
    monkeypatch.setattr(tweepy, "Client", _FakeTweepyClient)
    return _FakeTweepyClient


def _clear_x_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "X_USER_ACCESS_TOKEN",
        "X_BEARER_TOKEN",
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_x_user_token_uses_bearer_client_and_user_auth_false(monkeypatch, fake_tweepy):
    from olano.tools import x_twitter_tools

    _clear_x_env(monkeypatch)
    monkeypatch.setenv("X_USER_ACCESS_TOKEN", "x-user-tok")
    # Legacy 1.0a keys present too — the connected user token must win.
    monkeypatch.setenv("X_API_KEY", "k")
    monkeypatch.setenv("X_API_SECRET", "s")
    monkeypatch.setenv("X_ACCESS_TOKEN", "at")
    monkeypatch.setenv("X_ACCESS_SECRET", "as")

    out = x_twitter_tools.x_post_tweet.invoke({"text": "hello"})

    assert "Tweet posted" in out
    client = fake_tweepy.instances[-1]
    assert client.init_kwargs == {"bearer_token": "x-user-tok"}
    name, kwargs = client.calls[-1]
    assert name == "create_tweet"
    assert kwargs["text"] == "hello"
    assert kwargs["user_auth"] is False


def test_x_timeline_oauth2_passes_user_auth_false_on_each_call(monkeypatch, fake_tweepy):
    from olano.tools import x_twitter_tools

    _clear_x_env(monkeypatch)
    monkeypatch.setenv("X_USER_ACCESS_TOKEN", "x-user-tok")

    out = x_twitter_tools.x_timeline.invoke({})

    assert "hello" in out
    client = fake_tweepy.instances[-1]
    calls = dict(client.calls)
    assert calls["get_me"]["user_auth"] is False
    assert calls["get_users_tweets"]["user_auth"] is False
    assert calls["get_users_tweets"]["user_id"] == "42"


def test_x_like_oauth2_passes_user_auth_false(monkeypatch, fake_tweepy):
    from olano.tools import x_twitter_tools

    _clear_x_env(monkeypatch)
    monkeypatch.setenv("X_USER_ACCESS_TOKEN", "x-user-tok")

    out = x_twitter_tools.x_like_tweet.invoke({"tweet_id": "999"})

    assert "Liked tweet 999" in out
    client = fake_tweepy.instances[-1]
    name, kwargs = client.calls[-1]
    assert name == "like"
    assert kwargs == {"tweet_id": "999", "user_auth": False}


def test_x_read_tools_prefer_user_token_over_app_bearer(monkeypatch, fake_tweepy):
    from olano.tools import x_twitter_tools

    _clear_x_env(monkeypatch)
    monkeypatch.setenv("X_USER_ACCESS_TOKEN", "x-user-tok")
    monkeypatch.setenv("X_BEARER_TOKEN", "app-bearer")

    out = x_twitter_tools.x_search.invoke({"query": "q"})

    assert "No results found" in out
    client = fake_tweepy.instances[-1]
    assert client.init_kwargs == {"bearer_token": "x-user-tok"}


def test_x_oauth1_fallback_stays_byte_compatible(monkeypatch, fake_tweepy):
    from olano.tools import x_twitter_tools

    _clear_x_env(monkeypatch)
    monkeypatch.setenv("X_API_KEY", "k")
    monkeypatch.setenv("X_API_SECRET", "s")
    monkeypatch.setenv("X_ACCESS_TOKEN", "at")
    monkeypatch.setenv("X_ACCESS_SECRET", "as")

    out = x_twitter_tools.x_post_tweet.invoke({"text": "hello"})

    assert "Tweet posted" in out
    client = fake_tweepy.instances[-1]
    assert client.init_kwargs == {
        "consumer_key": "k",
        "consumer_secret": "s",
        "access_token": "at",
        "access_token_secret": "as",
    }
    name, kwargs = client.calls[-1]
    assert name == "create_tweet"
    assert "user_auth" not in kwargs  # legacy call shape unchanged

    # App-only bearer read path is also unchanged.
    monkeypatch.setenv("X_BEARER_TOKEN", "app-bearer")
    x_twitter_tools.x_search.invoke({"query": "q"})
    client = fake_tweepy.instances[-1]
    assert client.init_kwargs == {"bearer_token": "app-bearer"}
    name, kwargs = client.calls[-1]
    assert "user_auth" not in kwargs


def test_x_missing_credentials_mention_connections(monkeypatch, fake_tweepy):
    from olano.tools import x_twitter_tools

    _clear_x_env(monkeypatch)

    for out in (
        x_twitter_tools.x_post_tweet.invoke({"text": "hi"}),
        x_twitter_tools.x_search.invoke({"query": "q"}),
    ):
        assert out.startswith("[missing-credentials]")
        assert "Connections" in out
    assert not fake_tweepy.instances  # no client constructed without creds


# ── github ────────────────────────────────────────────────────────────────────


class _FakeGithubMod:
    """Fake ``github`` (PyGithub) module recording the auth token used."""

    def __init__(self):
        self.tokens: list[str] = []

    def Github(self, token):
        self.tokens.append(token)
        return SimpleNamespace(fake=True)


@pytest.fixture
def fake_github(monkeypatch):
    from olano.tools import github_tools

    module = _FakeGithubMod()
    monkeypatch.setattr(github_tools, "optional_import", lambda *a, **k: (module, None))
    return module


def _clear_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_TOKEN", "GITHUB_PAT", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_github_token_builds_client(monkeypatch, fake_github):
    from olano.tools import github_tools

    _clear_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "gho_live")

    gh, err = github_tools._gh_client()

    assert err is None and gh is not None
    assert fake_github.tokens == ["gho_live"]


def test_github_legacy_pat_fallback(monkeypatch, fake_github):
    from olano.tools import github_tools

    _clear_github_env(monkeypatch)
    monkeypatch.setenv("GITHUB_PAT", "ghp_legacy")

    gh, err = github_tools._gh_client()

    assert err is None and gh is not None
    assert fake_github.tokens == ["ghp_legacy"]


def test_github_missing_credentials_mention_connections(monkeypatch, fake_github):
    from olano.tools import github_tools

    _clear_github_env(monkeypatch)

    gh, err = github_tools._gh_client()

    assert gh is None
    assert err.startswith("[missing-credentials]")
    assert "Connections" in err
    assert not fake_github.tokens  # no client constructed without creds
