"""Tests for Google services tools (Gmail, Calendar, Drive)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_gmail_read_no_credentials():
    with patch("olano.tools.google_tools._get_credentials", side_effect=RuntimeError("No creds")):
        from olano.tools.google_tools import gmail_read

        result = gmail_read.invoke({"max_results": 5})

    assert "Error" in result


def test_gmail_read_returns_emails():
    mock_service = MagicMock()
    mock_service.users().messages().list().execute.return_value = {"messages": [{"id": "msg123"}]}
    mock_service.users().messages().get().execute.return_value = {
        "id": "msg123",
        "snippet": "Hello from test",
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Test email"},
                {"name": "Date", "value": "2026-03-30"},
            ]
        },
    }

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_read

        result = gmail_read.invoke({"max_results": 5})

    assert "alice@example.com" in result
    assert "Test email" in result


def test_gmail_send_success():
    mock_service = MagicMock()
    mock_service.users().messages().send().execute.return_value = {"id": "sent123"}

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "Hello Bob",
                "body": "This is a test email.",
            }
        )

    assert "sent" in result.lower() or "sent123" in result


def test_gmail_send_error():
    mock_service = MagicMock()
    mock_service.users().messages().send().execute.side_effect = Exception("quota exceeded")

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "Test",
                "body": "Body",
            }
        )

    assert "Error" in result


def _decoded_sent_raw(mock_service):
    """Decode the MIME bytes of the last message handed to messages().send()."""
    import base64

    raw = mock_service.users().messages().send.call_args.kwargs["body"]["raw"]
    return base64.urlsafe_b64decode(raw)


def test_gmail_send_defaults_to_html():
    mock_service = MagicMock()
    mock_service.users().messages().send().execute.return_value = {"id": "html1"}

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "Rich",
                "body": "<h1>Hello</h1><p>Rich <b>email</b></p>",
            }
        )

    assert "html1" in result
    decoded = _decoded_sent_raw(mock_service)
    # Every send is a rich HTML email: multipart/alternative carrying both the
    # HTML part and an auto-derived text/plain fallback.
    assert b"multipart/alternative" in decoded
    assert b"text/html" in decoded
    assert b"text/plain" in decoded


def test_gmail_send_derives_plaintext_fallback_from_html():
    mock_service = MagicMock()
    mock_service.users().messages().send().execute.return_value = {"id": "fb1"}

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "Fallback",
                "body": "<h1>Heading</h1><p>Body text</p>",
            }
        )

    assert "fb1" in result
    decoded = _decoded_sent_raw(mock_service)
    assert b"multipart/alternative" in decoded
    assert b"text/plain" in decoded


def test_gmail_send_with_attachment(tmp_path):
    import base64

    doc = tmp_path / "report.txt"
    doc.write_text("hello report")

    mock_service = MagicMock()
    mock_service.users().messages().send().execute.return_value = {"id": "sent123"}

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "With attachment",
                "body": "<p>See attached.</p>",
                "attachments": str(doc),
            }
        )

    assert "sent123" in result
    decoded = _decoded_sent_raw(mock_service)
    # Attachments wrap the HTML alternative in a multipart/mixed envelope.
    assert b"multipart/mixed" in decoded
    assert b"multipart/alternative" in decoded
    assert b"report.txt" in decoded
    assert b"attachment" in decoded
    # File bytes are base64-encoded into the attachment part.
    assert base64.b64encode(b"hello report") in decoded


def test_gmail_send_with_image_attachment(tmp_path):
    img = tmp_path / "logo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake png bytes")

    mock_service = MagicMock()
    mock_service.users().messages().send().execute.return_value = {"id": "img1"}

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "Logo",
                "body": "<p>See logo.</p>",
                "attachments": str(img),
            }
        )

    assert "img1" in result
    decoded = _decoded_sent_raw(mock_service)
    # Images get an image/* type (not octet-stream) so Gmail previews them.
    assert b"image/png" in decoded
    assert b"logo.png" in decoded


def test_gmail_send_multiple_attachments(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("aaa")
    b.write_text("bbb")

    mock_service = MagicMock()
    mock_service.users().messages().send().execute.return_value = {"id": "multi1"}

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "Two files",
                "body": "<p>Both attached.</p>",
                "attachments": f"{a}, {b}",
            }
        )

    assert "multi1" in result
    decoded = _decoded_sent_raw(mock_service)
    assert b"a.txt" in decoded
    assert b"b.txt" in decoded


def test_gmail_send_attachment_not_found():
    mock_service = MagicMock()

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_send

        result = gmail_send.invoke(
            {
                "to": "bob@example.com",
                "subject": "Oops",
                "body": "<p>Body</p>",
                "attachments": "/no/such/file.pdf",
            }
        )

    assert "not found" in result.lower()
    # Nothing should be sent when an attachment can't be read.
    mock_service.users().messages().send.assert_not_called()


def test_gmail_create_draft_with_attachment(tmp_path):
    doc = tmp_path / "draft.txt"
    doc.write_text("draft attachment")

    mock_service = MagicMock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft1"}

    with patch("olano.tools.google_tools._gmail_service", return_value=mock_service):
        from olano.tools.google_tools import gmail_create_draft

        result = gmail_create_draft.invoke(
            {
                "to": "bob@example.com",
                "subject": "Draft with file",
                "body": "<p>See attached.</p>",
                "attachments": str(doc),
            }
        )

    assert "draft1" in result
    import base64

    raw = mock_service.users().drafts().create.call_args.kwargs["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw)
    assert b"multipart/mixed" in decoded
    assert b"draft.txt" in decoded


def test_html_to_plaintext_strips_tags_and_unescapes():
    from olano.tools.google_tools import _html_to_plaintext

    html = "<h1>Title</h1><p>Hello <b>World</b> &amp; more</p><br><div>Line</div>"
    text = _html_to_plaintext(html)

    assert "Title" in text
    assert "World" in text
    assert "&amp;" not in text  # entity was unescaped
    assert "&" in text
    assert "<" not in text  # no tags remain


def test_calendar_list_events_empty():
    mock_service = MagicMock()
    mock_service.events().list().execute.return_value = {"items": []}

    with patch("olano.tools.google_tools._calendar_service", return_value=mock_service):
        from olano.tools.google_tools import calendar_list_events

        result = calendar_list_events.invoke({"days": 7})

    assert "No events" in result


def test_calendar_list_events_returns_events():
    mock_service = MagicMock()
    mock_service.events().list().execute.return_value = {
        "items": [
            {
                "id": "evt123",
                "summary": "Team standup",
                "start": {"dateTime": "2026-04-01T09:00:00Z"},
                "location": "Zoom",
            }
        ]
    }

    with patch("olano.tools.google_tools._calendar_service", return_value=mock_service):
        from olano.tools.google_tools import calendar_list_events

        result = calendar_list_events.invoke({"days": 7})

    assert "Team standup" in result
    assert "Zoom" in result


def test_calendar_create_event():
    mock_service = MagicMock()
    mock_service.events().insert().execute.return_value = {
        "id": "new_evt",
        "htmlLink": "https://calendar.google.com/event/new_evt",
    }

    with patch("olano.tools.google_tools._calendar_service", return_value=mock_service):
        from olano.tools.google_tools import calendar_create_event

        result = calendar_create_event.invoke(
            {
                "title": "Planning meeting",
                "start": "2026-04-01T14:00:00",
                "end": "2026-04-01T15:00:00",
            }
        )

    assert "Planning meeting" in result
    assert "new_evt" in result


def test_drive_list_files():
    mock_service = MagicMock()
    mock_service.files().list().execute.return_value = {
        "files": [
            {
                "id": "file1",
                "name": "report.pdf",
                "mimeType": "application/pdf",
                "size": "102400",
                "modifiedTime": "2026-03-01T00:00:00Z",
            },
        ]
    }

    with patch("olano.tools.google_tools._drive_service", return_value=mock_service):
        from olano.tools.google_tools import drive_list

        result = drive_list.invoke({"max_results": 10})

    assert "report.pdf" in result


def test_drive_upload_file_not_found():
    from olano.tools.google_tools import drive_upload

    with patch("olano.tools.google_tools._get_credentials", return_value=MagicMock()):
        result = drive_upload.invoke({"path": "/nonexistent/file.txt"})

    assert "not found" in result.lower()


# ── Headless OAuth flow (dashboard Connections page) ──────────────────────────


class _FakeCreds:
    def to_json(self):
        return '{"token": "abc", "refresh_token": "r", "scopes": []}'


class _FakeFlow:
    """Stand-in for google_auth_oauthlib.flow.Flow (no network)."""

    captured: dict = {}

    def __init__(self):
        self.credentials = _FakeCreds()

    @classmethod
    def from_client_secrets_file(cls, path, scopes=None, redirect_uri=None):
        cls.captured = {"path": path, "scopes": scopes, "redirect_uri": redirect_uri}
        return cls()

    @classmethod
    def from_client_config(cls, client_config, scopes=None, redirect_uri=None):
        cls.captured = {
            "client_config": client_config,
            "scopes": scopes,
            "redirect_uri": redirect_uri,
        }
        return cls()

    def authorization_url(self, **kwargs):
        _FakeFlow.captured["auth_kwargs"] = kwargs
        return ("https://accounts.google.com/o/oauth2/auth?state=xyz", "xyz")

    def fetch_token(self, code=None, **kwargs):
        # Real Flow.fetch_token forwards kwargs (e.g. timeout) to requests.
        _FakeFlow.captured["code"] = code


def _reset_google_sessions():
    from olano.tools import google_tools as gt

    gt._GOOGLE_AUTH_SESSIONS.clear()
    gt._GOOGLE_AUTH_BY_STATE.clear()


def test_start_google_auth_flow_returns_link_without_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    session = gt.start_google_auth_flow(
        creds_path="/tmp/client_secret.json",
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )

    assert session.status == "awaiting_authorization"
    assert session.authorization_url.startswith("https://accounts.google.com")
    assert session.state == "xyz"
    # The redirect URI is threaded into the OAuth client, not localhost.
    assert _FakeFlow.captured["redirect_uri"] == "https://host.example/api/auth/google/callback"
    # offline access so a refresh token is returned.
    assert _FakeFlow.captured["auth_kwargs"].get("access_type") == "offline"
    # The session and its state are registered for the callback to find.
    assert gt.poll_google_auth_flow(session.session_id) is session
    assert session.public()["redirect_uri"].endswith("/api/auth/google/callback")


def test_resolve_google_auth_callback_saves_token(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.config.home import get_olano_paths
    from olano.tools import google_tools as gt

    session = gt.start_google_auth_flow(
        creds_path="/tmp/client_secret.json",
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )

    resolved = gt.resolve_google_auth_callback("xyz", "auth-code-123", None)

    assert resolved is session
    assert session.status == "authorized"
    assert _FakeFlow.captured["code"] == "auth-code-123"
    assert get_olano_paths().google_token_path.exists()
    # State is consumed so the link can't be replayed.
    assert "xyz" not in gt._GOOGLE_AUTH_BY_STATE


def test_resolve_google_auth_callback_per_agent_shared(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.config.home import get_olano_paths
    from olano.tools import google_tools as gt

    gt.start_google_auth_flow(
        creds_path="/tmp/client_secret.json",
        agent="merli",
        shared=True,
        redirect_uri="https://host.example/api/auth/google/callback",
    )
    gt.resolve_google_auth_callback("xyz", "code", None)

    assert get_olano_paths().agent_google_token_path("merli").exists()
    assert gt.is_google_shared("merli") is True


def test_disconnect_google_clears_shared_flag_and_pending_flow(monkeypatch, tmp_path):
    """Disconnect must leave nothing behind: a stale shared=True would silently
    re-share the next token, and a live flow could still land one for the
    account just removed."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.config.home import get_olano_paths
    from olano.tools import google_tools as gt

    session = gt.start_google_auth_flow(
        creds_path="/tmp/client_secret.json",
        agent="merli",
        shared=True,
        redirect_uri="https://host.example/api/auth/google/callback",
    )
    gt.resolve_google_auth_callback("xyz", "code", None)
    assert gt.is_google_shared("merli") is True

    assert gt.disconnect_google(agent="merli") is True
    assert not get_olano_paths().agent_google_token_path("merli").exists()
    assert gt.is_google_shared("merli") is False
    assert session.session_id not in gt._GOOGLE_AUTH_SESSIONS
    assert "xyz" not in gt._GOOGLE_AUTH_BY_STATE

    # A second disconnect is a no-op, not an error.
    assert gt.disconnect_google(agent="merli") is False


def test_resolve_google_auth_callback_error(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    session = gt.start_google_auth_flow(
        creds_path="/tmp/client_secret.json",
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )
    resolved = gt.resolve_google_auth_callback("xyz", None, "access_denied")

    assert resolved is session
    assert session.status == "error"
    assert "access_denied" in (session.error or "")


def test_resolve_google_auth_callback_unknown_state(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    assert gt.resolve_google_auth_callback("does-not-exist", "code", None) is None
    assert gt.resolve_google_auth_callback(None, "code", None) is None


def _clear_google_client_env(monkeypatch):
    """Drop every stored-client source so fallback tests see a clean slate."""
    for name in (
        "GOOGLE_OAUTH_CREDENTIALS",
        "GOOGLE_OAUTH_CLIENT",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_start_google_auth_flow_requires_creds(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _clear_google_client_env(monkeypatch)
    _reset_google_sessions()

    import pytest

    from olano.tools import google_tools as gt

    with pytest.raises(ValueError, match="no OAuth client configured"):
        gt.start_google_auth_flow(
            creds_path=None,
            agent=None,
            shared=False,
            redirect_uri="https://host.example/api/auth/google/callback",
        )


def test_start_google_auth_flow_with_client_pair(monkeypatch, tmp_path):
    """Explicit Client ID/Secret (the connect panel's fields) build the flow
    from an in-memory client config — no client_secret.json involved."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    session = gt.start_google_auth_flow(
        creds_path=None,
        client_id="panel-id",
        client_secret="panel-secret",
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )

    assert session.status == "awaiting_authorization"
    web = _FakeFlow.captured["client_config"]["web"]
    assert web["client_id"] == "panel-id"
    assert web["client_secret"] == "panel-secret"
    assert _FakeFlow.captured["redirect_uri"] == "https://host.example/api/auth/google/callback"


def test_start_google_auth_flow_half_pair_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _clear_google_client_env(monkeypatch)
    _reset_google_sessions()

    import pytest

    from olano.tools import google_tools as gt

    with pytest.raises(ValueError, match="both a Client ID and a Client Secret"):
        gt.start_google_auth_flow(
            creds_path=None,
            client_id="panel-id",
            agent=None,
            shared=False,
            redirect_uri="https://host.example/api/auth/google/callback",
        )


def test_start_google_auth_flow_resolves_vault_blob(monkeypatch, tmp_path):
    """GOOGLE_OAUTH_CLIENT (vault/env JSON blob) wins over the legacy path, so
    an operator-provisioned official client needs no panel input."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _clear_google_client_env(monkeypatch)
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT", '{"client_id": "blob-id", "client_secret": "blob-secret"}'
    )
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/legacy/client_secret.json")
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    gt.start_google_auth_flow(
        creds_path=None,
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )

    assert _FakeFlow.captured["client_config"]["web"]["client_id"] == "blob-id"


def test_start_google_auth_flow_accepts_pasted_client_secret_json(monkeypatch, tmp_path):
    """The blob may be the full downloaded client_secret.json ({"web": …})."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _clear_google_client_env(monkeypatch)
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT",
        '{"web": {"client_id": "file-id", "client_secret": "file-secret",'
        ' "auth_uri": "https://accounts.google.com/o/oauth2/auth"}}',
    )
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    gt.start_google_auth_flow(
        creds_path=None,
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )

    assert _FakeFlow.captured["client_config"]["web"]["client_id"] == "file-id"


def test_start_google_auth_flow_resolves_client_id_pair(monkeypatch, tmp_path):
    """The GOOGLE_CLIENT_ID/-SECRET pair works like every other connector's
    client_env_pair; the legacy path only applies when no pair is stored."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _clear_google_client_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "pair-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "pair-secret")
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    gt.start_google_auth_flow(
        creds_path=None,
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )

    assert _FakeFlow.captured["client_config"]["web"]["client_id"] == "pair-id"


def test_start_google_auth_flow_explicit_path_beats_stored_pair(monkeypatch, tmp_path):
    """An explicitly passed creds_path is a deliberate override and wins over
    the stored official client."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _clear_google_client_env(monkeypatch)
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT", '{"client_id": "blob-id", "client_secret": "blob-secret"}'
    )
    monkeypatch.setattr("google_auth_oauthlib.flow.Flow", _FakeFlow)
    _reset_google_sessions()

    from olano.tools import google_tools as gt

    gt.start_google_auth_flow(
        creds_path="/explicit/client_secret.json",
        agent=None,
        shared=False,
        redirect_uri="https://host.example/api/auth/google/callback",
    )

    assert _FakeFlow.captured["path"] == "/explicit/client_secret.json"


def test_browser_available_false_over_ssh(monkeypatch):
    from olano.tools.google_tools import browser_available

    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert browser_available() is False


# ── Sheets: A1 / grid helpers ─────────────────────────────────────────────────


def test_col_letters_index_roundtrip():
    from olano.tools.google_tools import _col_index, _col_letters

    assert [_col_letters(i) for i in (1, 26, 27, 53, 702, 703)] == [
        "A",
        "Z",
        "AA",
        "BA",
        "ZZ",
        "AAA",
    ]
    for i in (1, 26, 27, 53, 702, 703):
        assert _col_index(_col_letters(i)) == i
    assert _col_index("") == 0


def test_quote_sheet_only_quotes_ambiguous_names():
    from olano.tools.google_tools import _quote_sheet

    # Bare names that cannot be read as a cell reference stay unquoted.
    assert _quote_sheet("Sheet1") == "Sheet1"
    assert _quote_sheet("Data") == "Data"
    # A tab named like a cell (columns stop at XFD) must be quoted.
    assert _quote_sheet("Q1") == "'Q1'"
    assert _quote_sheet("XFD9") == "'XFD9'"
    # Spaces / punctuation / embedded apostrophes.
    assert _quote_sheet("My Sheet") == "'My Sheet'"
    assert _quote_sheet("2026-Q1") == "'2026-Q1'"
    assert _quote_sheet("Bob's") == "'Bob''s'"
    assert _quote_sheet("") == ""


def test_split_a1_handles_quoted_tab_names():
    from olano.tools.google_tools import _split_a1

    assert _split_a1("Sheet1!A1:C10") == ("Sheet1", "A1", "C10")
    assert _split_a1("A1:C10") == ("", "A1", "C10")
    assert _split_a1("'My Sheet'!A1") == ("My Sheet", "A1", "")
    # rpartition on "!" keeps a bang inside a quoted tab name intact.
    assert _split_a1("'My!Sheet'!A1:B2") == ("My!Sheet", "A1", "B2")
    assert _split_a1("'Bob''s'!A1") == ("Bob's", "A1", "")


def test_a1_origin_reports_first_cell():
    from olano.tools.google_tools import _a1_origin

    assert _a1_origin("Sheet1!B2:D10") == (2, 2)
    assert _a1_origin("Sheet1!C:E") == (1, 3)  # unbounded rows start at 1
    assert _a1_origin("Sheet1!2:5") == (2, 1)  # unbounded columns start at A
    assert _a1_origin("AA5") == (5, 27)


def test_a1_to_grid_range_is_zero_based_half_open():
    from olano.tools.google_tools import _a1_to_grid_range

    assert _a1_to_grid_range("A1:C10", 7) == {
        "sheetId": 7,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
        "startRowIndex": 0,
        "endRowIndex": 10,
    }
    # A lone cell is the 1x1 range at that cell.
    assert _a1_to_grid_range("B2", 7) == {
        "sheetId": 7,
        "startColumnIndex": 1,
        "endColumnIndex": 2,
        "startRowIndex": 1,
        "endRowIndex": 2,
    }
    # Unbounded sides stay omitted so Sheets applies them to the whole axis.
    assert _a1_to_grid_range("A:C", 7) == {
        "sheetId": 7,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    assert _a1_to_grid_range("2:5", 7) == {"sheetId": 7, "startRowIndex": 1, "endRowIndex": 5}
    assert _a1_to_grid_range("", 7) == {"sheetId": 7}


def test_parse_values_accepts_grid_row_and_scalar():
    import pytest

    from olano.tools.google_tools import _parse_values

    assert _parse_values('[["a", 1], ["b", 2]]') == [["a", 1], ["b", 2]]
    assert _parse_values('["a", 1]') == [["a", 1]]  # flat array = one row
    assert _parse_values("42") == [[42]]  # scalar = one cell
    assert _parse_values("") == []
    assert _parse_values("[]") == []
    # Numbers keep their JSON type so Sheets stores numbers, not text.
    assert _parse_values("[[99]]") == [[99]]

    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_values("{oops")
    with pytest.raises(ValueError, match="not a mix"):
        _parse_values('[["a"], "b"]')


def test_parse_color_hex_forms():
    import pytest

    from olano.tools.google_tools import _parse_color

    assert _parse_color("#FFFFFF") == {"red": 1.0, "green": 1.0, "blue": 1.0}
    assert _parse_color("#000") == {"red": 0.0, "green": 0.0, "blue": 0.0}
    assert _parse_color("4285F4")["red"] == pytest.approx(0x42 / 255)

    with pytest.raises(ValueError, match="Invalid color"):
        _parse_color("periwinkle")


def test_render_grid_labels_real_spreadsheet_coordinates():
    from olano.tools.google_tools import _render_grid

    out = _render_grid(
        [["Name", "Email"], ["Alice", "a@x.com"], ["Bob", None]], (1, 1), 10
    ).splitlines()

    assert out[0].startswith("  | A") and "| B" in out[0]
    assert out[2].startswith("1 | Name")
    assert out[4].startswith("3 | Bob")

    # A range starting at AA5 is labelled from AA5, not A1.
    offset = _render_grid([["x", "y"]], (5, 27), 10).splitlines()
    assert "AA" in offset[0] and "AB" in offset[0]
    assert offset[2].startswith("5 |")

    # Truncation is announced rather than silent.
    capped = _render_grid([["a"], ["b"], ["c"]], (1, 1), 2)
    assert "1 more row(s) not shown" in capped
    assert _render_grid([], (1, 1), 10) == "(empty range)"


# ── Sheets: tools ─────────────────────────────────────────────────────────────


def _sheets_service_mock(tabs=(("Sheet1", 0),)):
    """Build a Sheets service mock whose spreadsheet has the given tabs."""
    service = MagicMock()
    service.spreadsheets().get().execute.return_value = {
        "properties": {"title": "Book", "locale": "en_US", "timeZone": "UTC"},
        "sheets": [
            {
                "properties": {
                    "title": title,
                    "sheetId": sheet_id,
                    "gridProperties": {"rowCount": 1000, "columnCount": 26},
                }
            }
            for title, sheet_id in tabs
        ],
    }
    return service


def test_sheets_read_renders_labelled_table():
    service = _sheets_service_mock()
    service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:B3",
        "values": [["Name", "Score"], ["Ada", 99], ["Grace", 97]],
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_read

        result = sheets_read.invoke({"spreadsheet_id": "abc", "range_a1": "Sheet1!A1:B3"})

    assert "Sheet1!A1:B3 — 3 row(s)" in result
    assert "1 | Name" in result and "2 | Ada" in result
    assert "| A" in result and "| B" in result


def test_sheets_read_defaults_to_first_tab():
    service = _sheets_service_mock(tabs=(("Q1", 11), ("Notes", 12)))
    service.spreadsheets().values().get().execute.return_value = {
        "range": "'Q1'!A1:A1",
        "values": [["hi"]],
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_read

        sheets_read.invoke({"spreadsheet_id": "abc"})

    # The ambiguous tab name "Q1" must be quoted when used as a range.
    assert service.spreadsheets().values().get.call_args.kwargs["range"] == "'Q1'"


def test_sheets_read_rejects_bad_render_option():
    from olano.tools.google_tools import sheets_read

    result = sheets_read.invoke({"spreadsheet_id": "abc", "value_render": "PRETTY"})

    assert "Invalid value_render" in result


def test_sheets_read_no_credentials():
    with patch("olano.tools.google_tools._get_credentials", side_effect=RuntimeError("No creds")):
        from olano.tools.google_tools import sheets_read

        result = sheets_read.invoke({"spreadsheet_id": "abc"})

    assert "Error connecting to Sheets" in result


def test_sheets_write_sends_parsed_values():
    service = _sheets_service_mock()
    service.spreadsheets().values().update().execute.return_value = {
        "updatedRange": "Sheet1!A1:B2",
        "updatedRows": 2,
        "updatedCells": 4,
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_write

        result = sheets_write.invoke(
            {
                "spreadsheet_id": "abc",
                "range_a1": "Sheet1!A1",
                "values_json": '[["Name", "Score"], ["Ada", 99]]',
            }
        )

    kwargs = service.spreadsheets().values().update.call_args.kwargs
    assert kwargs["body"] == {"values": [["Name", "Score"], ["Ada", 99]]}
    assert kwargs["valueInputOption"] == "USER_ENTERED"
    assert "Wrote 4 cell(s)" in result


def test_sheets_write_reports_bad_json():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_write

        result = sheets_write.invoke(
            {"spreadsheet_id": "abc", "range_a1": "Sheet1!A1", "values_json": "{oops"}
        )

    assert "not valid JSON" in result
    service.spreadsheets().values().update.assert_not_called()


def test_sheets_write_rejects_empty_values():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_write

        result = sheets_write.invoke(
            {"spreadsheet_id": "abc", "range_a1": "Sheet1!A1", "values_json": "[]"}
        )

    assert "sheets_clear" in result


def test_sheets_append_inserts_rows():
    service = _sheets_service_mock()
    service.spreadsheets().values().append().execute.return_value = {
        "updates": {"updatedRange": "Sheet1!A4:B4", "updatedRows": 1}
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_append

        result = sheets_append.invoke({"spreadsheet_id": "abc", "values_json": '["Ada", 99]'})

    kwargs = service.spreadsheets().values().append.call_args.kwargs
    assert kwargs["insertDataOption"] == "INSERT_ROWS"
    assert kwargs["body"] == {"values": [["Ada", 99]]}
    assert "Appended 1 row(s)" in result


def test_sheets_create_writes_initial_rows():
    service = MagicMock()
    service.spreadsheets().create().execute.return_value = {
        "spreadsheetId": "new123",
        "sheets": [{"properties": {"title": "Data", "sheetId": 0}}],
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_create

        result = sheets_create.invoke(
            {
                "title": "Budget",
                "sheet_names": "Data, Notes",
                "values_json": '[["Item", "Cost"]]',
            }
        )

    body = service.spreadsheets().create.call_args.kwargs["body"]
    assert body["properties"]["title"] == "Budget"
    assert [s["properties"]["title"] for s in body["sheets"]] == ["Data", "Notes"]
    assert service.spreadsheets().values().update.call_args.kwargs["range"] == "Data!A1"
    assert "new123" in result and "Wrote 1 row(s)" in result


def test_sheets_info_lists_tabs():
    service = _sheets_service_mock(tabs=(("Data", 0), ("Notes", 42)))

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_info

        result = sheets_info.invoke({"spreadsheet_id": "abc"})

    assert "# Book" in result
    assert "sheetId: 42" in result
    assert "1000 rows × 26 cols" in result
    assert "#gid=42" in result


def test_sheets_delete_sheet_resolves_name_to_id():
    service = _sheets_service_mock(tabs=(("Data", 0), ("Notes", 42)))

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_delete_sheet

        result = sheets_delete_sheet.invoke({"spreadsheet_id": "abc", "sheet_name": "Notes"})

    body = service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    assert body["requests"] == [{"deleteSheet": {"sheetId": 42}}]
    assert "Deleted tab 'Notes'" in result


def test_sheets_delete_sheet_unknown_tab_lists_available():
    service = _sheets_service_mock(tabs=(("Data", 0), ("Notes", 42)))

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_delete_sheet

        result = sheets_delete_sheet.invoke({"spreadsheet_id": "abc", "sheet_name": "Nope"})

    assert "No sheet named 'Nope'" in result
    assert "'Data'" in result and "'Notes'" in result


def test_sheets_insert_rows_converts_to_zero_based_half_open():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_insert_rows

        result = sheets_insert_rows.invoke({"spreadsheet_id": "abc", "start_index": 2, "count": 3})

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert request["insertDimension"]["range"] == {
        "sheetId": 0,
        "dimension": "ROWS",
        "startIndex": 1,
        "endIndex": 4,
    }
    assert request["insertDimension"]["inheritFromBefore"] is True
    assert "Inserted 3 row(s) at position 2" in result


def test_sheets_insert_rows_at_top_does_not_inherit():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_insert_rows

        sheets_insert_rows.invoke({"spreadsheet_id": "abc", "start_index": 1})

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert request["insertDimension"]["inheritFromBefore"] is False


def test_sheets_delete_rows_supports_columns():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_delete_rows

        result = sheets_delete_rows.invoke(
            {"spreadsheet_id": "abc", "start_index": 3, "count": 2, "dimension": "columns"}
        )

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert request["deleteDimension"]["range"]["dimension"] == "COLUMNS"
    assert request["deleteDimension"]["range"]["startIndex"] == 2
    assert "Deleted 2 column(s)" in result


def test_sheets_delete_rows_rejects_zero_index():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_delete_rows

        result = sheets_delete_rows.invoke({"spreadsheet_id": "abc", "start_index": 0})

    assert "1-based" in result
    service.spreadsheets().batchUpdate.assert_not_called()


def test_sheets_find_replace_defaults_to_all_tabs():
    service = _sheets_service_mock()
    service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [
            {"findReplace": {"occurrencesChanged": 4, "rowsChanged": 2, "sheetsChanged": 1}}
        ]
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_find_replace

        result = sheets_find_replace.invoke(
            {"spreadsheet_id": "abc", "find": "old", "replace": "new"}
        )

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert request["findReplace"]["allSheets"] is True
    assert "Replaced 4 occurrence(s)" in result and "all tabs" in result


def test_sheets_format_cells_builds_field_mask_from_set_args_only():
    service = _sheets_service_mock(tabs=(("Data", 5),))

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_format_cells

        result = sheets_format_cells.invoke(
            {
                "spreadsheet_id": "abc",
                "range_a1": "Data!A1:C1",
                "bold": True,
                "background": "#4285F4",
            }
        )

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    fields = request["repeatCell"]["fields"].split(",")
    assert set(fields) == {
        "userEnteredFormat.textFormat.bold",
        "userEnteredFormat.backgroundColor",
    }
    # Untouched properties must not appear in the mask or the payload.
    assert "italic" not in request["repeatCell"]["fields"]
    assert request["repeatCell"]["range"] == {
        "sheetId": 5,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
        "startRowIndex": 0,
        "endRowIndex": 1,
    }
    assert "Formatted" in result


def test_sheets_format_cells_whole_tab_by_name():
    service = _sheets_service_mock(tabs=(("Data", 5), ("Notes", 6)))

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_format_cells

        sheets_format_cells.invoke({"spreadsheet_id": "abc", "range_a1": "Notes", "italic": True})

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    # A bare tab name means the whole tab, not the cell reference "NOTES".
    assert request["repeatCell"]["range"] == {"sheetId": 6}


def test_sheets_format_cells_requires_at_least_one_property():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_format_cells

        result = sheets_format_cells.invoke({"spreadsheet_id": "abc", "range_a1": "Sheet1!A1"})

    assert "Nothing to change" in result
    service.spreadsheets().batchUpdate.assert_not_called()


def test_sheets_format_cells_rejects_bad_color():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_format_cells

        result = sheets_format_cells.invoke(
            {"spreadsheet_id": "abc", "range_a1": "Sheet1!A1", "background": "burgundy"}
        )

    assert "Invalid color" in result


def test_sheets_freeze_sets_grid_properties():
    service = _sheets_service_mock()

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_freeze

        result = sheets_freeze.invoke({"spreadsheet_id": "abc", "rows": 1, "cols": 2})

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    grid = request["updateSheetProperties"]["properties"]["gridProperties"]
    assert grid == {"frozenRowCount": 1, "frozenColumnCount": 2}
    assert "frozenRowCount" in request["updateSheetProperties"]["fields"]
    assert "Froze 1 row(s) and 2 column(s)" in result


def test_sheets_clear_calls_values_clear():
    service = _sheets_service_mock()
    service.spreadsheets().values().clear().execute.return_value = {"clearedRange": "Sheet1!A2:D9"}

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_clear

        result = sheets_clear.invoke({"spreadsheet_id": "abc", "range_a1": "Sheet1!A2:D9"})

    assert "Cleared values in Sheet1!A2:D9" in result


def test_sheets_add_sheet_reports_new_tab():
    service = _sheets_service_mock()
    service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"title": "Q3", "sheetId": 77}}}]
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_add_sheet

        result = sheets_add_sheet.invoke({"spreadsheet_id": "abc", "title": "Q3", "rows": 50})

    props = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]["addSheet"][
        "properties"
    ]
    assert props["gridProperties"]["rowCount"] == 50
    assert "Added tab 'Q3'" in result and "#gid=77" in result


def test_sheets_rename_sheet():
    service = _sheets_service_mock(tabs=(("Old", 3),))

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_rename_sheet

        result = sheets_rename_sheet.invoke(
            {"spreadsheet_id": "abc", "sheet_name": "Old", "new_title": "New"}
        )

    request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert request["updateSheetProperties"]["properties"] == {"sheetId": 3, "title": "New"}
    assert "'Old' → 'New'" in result


def test_sheets_batch_read_renders_each_range():
    service = _sheets_service_mock()
    service.spreadsheets().values().batchGet().execute.return_value = {
        "valueRanges": [
            {"range": "Data!A1:B2", "values": [["a", "b"], ["c", "d"]]},
            {"range": "Notes!A1:A1", "values": [["hello"]]},
        ]
    }

    with patch("olano.tools.google_tools._sheets_service", return_value=service):
        from olano.tools.google_tools import sheets_batch_read

        result = sheets_batch_read.invoke(
            {"spreadsheet_id": "abc", "ranges": "Data!A1:B2, Notes!A1"}
        )

    assert service.spreadsheets().values().batchGet.call_args.kwargs["ranges"] == [
        "Data!A1:B2",
        "Notes!A1",
    ]
    assert "## Data!A1:B2 — 2 row(s)" in result
    assert "## Notes!A1:A1 — 1 row(s)" in result


def test_sheets_batch_read_requires_ranges():
    from olano.tools.google_tools import sheets_batch_read

    with patch("olano.tools.google_tools._sheets_service", return_value=MagicMock()):
        result = sheets_batch_read.invoke({"spreadsheet_id": "abc", "ranges": " , "})

    assert "No ranges given" in result


def test_sheets_list_filters_to_spreadsheet_mime():
    service = MagicMock()
    service.files().list().execute.return_value = {
        "files": [{"id": "s1", "name": "Budget", "modifiedTime": "2026-07-01T00:00:00Z"}]
    }

    with patch("olano.tools.google_tools._drive_service", return_value=service):
        from olano.tools.google_tools import sheets_list

        result = sheets_list.invoke({"query": "Budget"})

    q = service.files().list.call_args.kwargs["q"]
    assert "application/vnd.google-apps.spreadsheet" in q
    assert "name contains 'Budget'" in q
    assert "Budget" in result and "s1" in result


def test_sheets_list_escapes_quotes_in_query():
    service = MagicMock()
    service.files().list().execute.return_value = {"files": []}

    with patch("olano.tools.google_tools._drive_service", return_value=service):
        from olano.tools.google_tools import sheets_list

        sheets_list.invoke({"query": "Bob's"})

    assert "name contains 'Bob\\'s'" in service.files().list.call_args.kwargs["q"]


def test_sheets_export_csv_writes_agent_workspace_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    service = _sheets_service_mock(tabs=(("Data", 9),))

    response = MagicMock()
    response.headers = {"content-type": "text/csv; charset=utf-8"}
    response.content = b"a,b\n1,2\n"
    session = MagicMock()
    session.get.return_value = response

    with (
        patch("olano.tools.google_tools._sheets_service", return_value=service),
        patch("olano.tools.google_tools._get_credentials", return_value=MagicMock()),
        patch("google.auth.transport.requests.AuthorizedSession", return_value=session),
    ):
        from olano.tools.google_tools import sheets_export_csv

        result = sheets_export_csv.invoke({"spreadsheet_id": "abc"})

    # Exports go through the per-gid URL so a non-first tab is respected.
    assert "gid=9" in session.get.call_args.args[0]
    out = tmp_path / "downloads" / "Data.csv"
    assert out.read_bytes() == b"a,b\n1,2\n"
    assert "Exported tab 'Data'" in result


def test_sheets_export_csv_rejects_html_error_page(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    service = _sheets_service_mock()

    response = MagicMock()
    response.headers = {"content-type": "text/html"}
    response.content = b"<html>Sign in</html>"
    session = MagicMock()
    session.get.return_value = response

    with (
        patch("olano.tools.google_tools._sheets_service", return_value=service),
        patch("olano.tools.google_tools._get_credentials", return_value=MagicMock()),
        patch("google.auth.transport.requests.AuthorizedSession", return_value=session),
    ):
        from olano.tools.google_tools import sheets_export_csv

        result = sheets_export_csv.invoke({"spreadsheet_id": "abc"})

    assert "instead of CSV" in result
    assert not (tmp_path / "downloads" / "Sheet1.csv").exists()


def test_every_writing_sheets_tool_is_classified_mutating():
    """Name inference misses clear/insert/format/freeze/rename/find_replace.

    Those carry an explicit @tool_metadata; this asserts the whole write surface
    so a new tool whose verb the inference doesn't know can't slip through as
    read_only.
    """
    from olano.security.tool_metadata import get_tool_metadata
    from olano.tools.google_tools import get_tool, get_tools

    writes = {
        "sheets_create",
        "sheets_write",
        "sheets_append",
        "sheets_clear",
        "sheets_add_sheet",
        "sheets_delete_sheet",
        "sheets_rename_sheet",
        "sheets_insert_rows",
        "sheets_delete_rows",
        "sheets_find_replace",
        "sheets_format_cells",
        "sheets_freeze",
    }
    for name in writes:
        assert get_tool_metadata(get_tool(name)).capability == "mutating", name

    # Everything else in the Sheets surface only reads the spreadsheet
    # (sheets_export_csv writes a local file, like the read_only drive_download).
    reads = {t.name for t in get_tools() if t.name.startswith("sheets_")} - writes
    assert reads == {
        "sheets_list",
        "sheets_info",
        "sheets_read",
        "sheets_batch_read",
        "sheets_export_csv",
    }
    for name in reads:
        assert get_tool_metadata(get_tool(name)).capability == "read_only", name


# ── Legacy-token scope handling (regression: refresh must never up-scope) ─────


def _write_legacy_token(home) -> set[str]:
    """Write a token granted a pre-Sheets scope set; return those scopes."""
    import json

    granted = {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive",
    }
    (home / "google_token.json").write_text(
        json.dumps(
            {
                "token": "ya29.test",
                "refresh_token": "1//refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "cid.apps.googleusercontent.com",
                "client_secret": "cs",
                "scopes": sorted(granted),
                "expiry": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return granted


def test_get_credentials_uses_granted_scopes_not_module_scopes(monkeypatch, tmp_path):
    """Tokens minted before a scope joined _SCOPES must carry only their own
    granted scopes: a refresh token can never be up-scoped, so loading with
    _SCOPES pinned made every refresh fail with invalid_scope."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    granted = _write_legacy_token(tmp_path)

    from olano.tools import google_tools as gt

    creds = gt._get_credentials()
    assert set(creds.scopes) == granted


def test_google_auth_status_connected_for_legacy_scope_token(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    _write_legacy_token(tmp_path)

    from olano.tools import google_tools as gt

    status = gt.google_auth_status()
    assert status["connected"] is True
    assert status["scope"] == "system"


# ── Gmail attachments and evidence fields ───────────────────────────────────


def _b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode().rstrip("=")


_CONFIRMATION_PDF = b"%PDF-1.4 broker confirmation"


def _message_with_attachments() -> dict:
    """A broker confirmation: HTML-only body, one fetched PDF, one inline CSV."""
    return {
        "id": "18c0ffee",
        "threadId": "18c0ff00",
        "internalDate": "1790064000000",  # 2026-09-22T08:00:00Z
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "desk@broker.example"},
                {"name": "To", "value": "trader@fund.example"},
                {"name": "Cc", "value": "pm@fund.example"},
                {"name": "Date", "value": "Tue, 22 Sep 2026 16:00:00 +0800"},
                {"name": "Subject", "value": "Execution GCF1-20260922-001"},
                {"name": "Message-ID", "value": "<abc@broker.example>"},
            ],
            "parts": [
                {
                    "mimeType": "text/html",
                    "filename": "",
                    "body": {"data": _b64(b"<p>Filled <b>1,000,000</b> at 99.5</p>")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "confirmation.pdf",
                    "body": {"attachmentId": "att-1", "size": len(_CONFIRMATION_PDF)},
                },
                {
                    "mimeType": "text/csv",
                    "filename": "fills.csv",
                    "body": {"data": _b64(b"isin,qty\nXS1,1000000\n"), "size": 21},
                },
            ],
        },
    }


def _attachment_service() -> MagicMock:
    service = MagicMock()
    service.users().messages().get().execute.return_value = _message_with_attachments()
    service.users().messages().attachments().get().execute.return_value = {
        "data": _b64(_CONFIRMATION_PDF)
    }
    return service


def test_gmail_get_message_lists_attachments_and_evidence_headers():
    with patch("olano.tools.google_tools._gmail_service", return_value=_attachment_service()):
        from olano.tools.google_tools import gmail_get_message

        result = gmail_get_message.invoke({"message_id": "18c0ffee"})

    assert "Cc: pm@fund.example" in result
    assert "Received (UTC): 2026-09-22T08:00:00Z" in result
    assert "Message-ID: <abc@broker.example>" in result
    assert "Thread ID: 18c0ff00" in result
    assert "- confirmation.pdf (application/pdf" in result
    assert "- fills.csv (text/csv" in result
    # HTML-only body is rendered to text rather than coming back empty.
    assert "Filled 1,000,000 at 99.5" in result


def test_gmail_download_attachments_saves_into_the_workspace(tmp_path, monkeypatch):
    import olano.tools.google_tools as g

    monkeypatch.setattr(g, "_gmail_workspace_root", lambda: tmp_path)
    with patch("olano.tools.google_tools._gmail_service", return_value=_attachment_service()):
        result = g.gmail_download_attachments.invoke({"message_id": "18c0ffee"})

    folder = tmp_path / "downloads" / "gmail" / "18c0ffee"
    assert (folder / "confirmation.pdf").read_bytes() == _CONFIRMATION_PDF
    assert (folder / "fills.csv").read_bytes() == b"isin,qty\nXS1,1000000\n"
    assert f"Saved: {folder / 'confirmation.pdf'}" in result

    # Downloading again reuses the identical file instead of making a copy.
    with patch("olano.tools.google_tools._gmail_service", return_value=_attachment_service()):
        again = g.gmail_download_attachments.invoke(
            {"message_id": "18c0ffee", "filenames": "confirmation.pdf"}
        )
    assert f"Saved: {folder / 'confirmation.pdf'}" in again
    assert sorted(p.name for p in folder.iterdir()) == ["confirmation.pdf", "fills.csv"]


def test_gmail_download_attachments_rejects_unknown_names_and_escapes(tmp_path, monkeypatch):
    import olano.tools.google_tools as g

    monkeypatch.setattr(g, "_gmail_workspace_root", lambda: tmp_path)
    with patch("olano.tools.google_tools._gmail_service", return_value=_attachment_service()):
        unknown = g.gmail_download_attachments.invoke(
            {"message_id": "18c0ffee", "filenames": "invoice.pdf"}
        )
        escaped = g.gmail_download_attachments.invoke(
            {"message_id": "18c0ffee", "save_dir": "/etc"}
        )

    assert "no attachment named invoice.pdf" in unknown
    assert "confirmation.pdf, fills.csv" in unknown
    assert "must be inside the workspace" in escaped
