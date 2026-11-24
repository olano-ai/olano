"""Microsoft 365 toolkit: request shapes, safety rails and error handling.

Graph is faked at the transport (``httpx.MockTransport`` through the module's
``_client`` seam), so every test sees the exact method, path, query, headers
and JSON body a tool sends, without a network or a token.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from olano.tools import microsoft365_tools as m
from olano.tools._connector_auth import is_auth_refusal

Handler = Callable[[httpx.Request], httpx.Response]


class FakeGraph:
    """Route table over a mock transport; records every request it serves."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self.requests: list[httpx.Request] = []

    def on(self, method: str, pattern: str, reply: Any = None, status: int = 200) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if callable(reply):
                return reply(request)
            if reply is None:
                return httpx.Response(status)
            return httpx.Response(status, json=reply)

        self.routes.append((method.upper(), re.compile(pattern), handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        target = f"{request.url.host}{request.url.path}"
        for method, pattern, handler in reversed(self.routes):
            if method == request.method and pattern.search(target):
                return handler(request)
        return httpx.Response(599, json={"error": {"code": "unrouted", "message": target}})

    def sent(self, method: str, pattern: str) -> list[httpx.Request]:
        rx = re.compile(pattern)
        return [
            r
            for r in self.requests
            if r.method == method and rx.search(f"{r.url.host}{r.url.path}")
        ]

    @staticmethod
    def body(request: httpx.Request) -> Any:
        return json.loads(request.content) if request.content else None


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGraph:
    fake = FakeGraph()
    transport = httpx.MockTransport(fake)
    monkeypatch.setattr(m, "_client", lambda **kw: httpx.Client(transport=transport, **kw))
    monkeypatch.setattr(m, "_token", lambda: "tok-123")
    monkeypatch.setattr(m, "_workspace_root", lambda: tmp_path)
    m._TZ_CACHE.clear()
    # Every test gets a mailbox time zone unless it overrides this route.
    fake.on("GET", r"/me/mailboxSettings/timeZone$", {"value": "Pacific Standard Time"})
    return fake


def _call(tool: Any, **kwargs: Any) -> str:
    return str(tool.invoke(kwargs))


# ── plumbing ─────────────────────────────────────────────────────────────────


def test_every_request_carries_the_token_and_immutable_ids(graph: FakeGraph) -> None:
    graph.on("GET", r"/me/mailFolders/inbox/messages$", {"value": []})
    _call(m.ms365_list_messages)
    request = graph.sent("GET", r"/messages$")[0]
    assert request.headers["authorization"] == "Bearer tok-123"
    assert 'IdType="ImmutableId"' in request.headers["prefer"]


def test_a_page_token_off_graph_is_refused_before_any_request(graph: FakeGraph) -> None:
    out = _call(m.ms365_list_messages, page_token="https://evil.example/v1.0/me/messages?$skip=10")
    assert "refusing a URL outside graph.microsoft.com" in out
    assert graph.requests == []


def test_a_graph_page_token_is_followed_as_given(graph: FakeGraph) -> None:
    link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc"
    graph.on("GET", r"/v1.0/me/messages$", {"value": []})
    _call(m.ms365_list_messages, page_token=link)
    assert graph.requests[0].url.params["$skiptoken"] == "abc"


def test_a_403_names_the_reconnect_and_never_reads_as_a_refused_sign_in(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/people$",
        {"error": {"code": "ErrorAccessDenied", "message": "Access is denied. Check credentials."}},
        status=403,
    )
    graph.on(
        "GET",
        r"/users$",
        # A 403 whose wording would otherwise trip the boundary's refusal markers.
        {"error": {"code": "Authorization_RequestDenied", "message": "Unauthorized scope"}},
        status=403,
    )
    graph.on("GET", r"/me/contacts$", {"error": {"code": "ErrorAccessDenied"}}, status=403)
    out = _call(m.ms365_find_people, query="maria")
    assert 'magic_link_connect_service("microsoft365")' in out
    assert "403" in out
    assert not is_auth_refusal(out)


def test_a_401_keeps_its_status_so_the_boundary_can_refresh(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/mailFolders/inbox/messages$",
        {"error": {"code": "InvalidAuthenticationToken", "message": "Access token has expired."}},
        status=401,
    )
    out = _call(m.ms365_list_messages)
    assert out.startswith("[tool-error]")
    assert is_auth_refusal(out)


def test_throttling_names_the_wait(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/mailFolders/inbox/messages$",
        lambda r: httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {}}),
    )
    assert "Wait 7s" in _call(m.ms365_list_messages)


# ── mail ─────────────────────────────────────────────────────────────────────


def test_list_messages_leads_the_filter_with_the_ordered_property(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/mailFolders/inbox/messages$",
        {
            "value": [
                {
                    "id": "m1",
                    "subject": "Q3",
                    "from": {"emailAddress": {"name": "Ana", "address": "ana@x.com"}},
                    "receivedDateTime": "2026-09-30T14:02:00Z",
                    "isRead": False,
                    "hasAttachments": True,
                }
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=10",
        },
    )
    out = _call(
        m.ms365_list_messages,
        unread_only=True,
        from_address="o'neil@x.com",
        received_after="2026-09-01",
    )
    params = graph.requests[-1].url.params
    assert params["$filter"].startswith("receivedDateTime ge 2026-09-01T00:00:00Z")
    assert "isRead eq false" in params["$filter"]
    assert "from/emailAddress/address eq 'o''neil@x.com'" in params["$filter"]
    assert params["$orderby"] == "receivedDateTime desc"
    assert "Ana <ana@x.com>" in out and "[unread, attachment]" in out
    assert 'page_token="https://graph.microsoft.com/v1.0/me/messages?$skip=10"' in out


def test_list_messages_search_carries_the_filters_as_kql(graph: FakeGraph) -> None:
    graph.on("GET", r"/me/messages$", {"value": []})
    _call(m.ms365_list_messages, folder="all", query='invoice "urgent"', from_address="bo@y.com")
    params = graph.requests[-1].url.params
    assert params["$search"] == '"invoice urgent from:bo@y.com"'
    assert "$filter" not in params and "$orderby" not in params


def test_list_messages_resolves_a_folder_by_display_name(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/mailFolders$",
        {"value": [{"id": "F-CLIENTS", "displayName": "Clients", "childFolderCount": 0}]},
    )
    graph.on("GET", r"/me/mailFolders/F-CLIENTS/messages$", {"value": []})
    assert _call(m.ms365_list_messages, folder="clients") == "No messages found."


def test_send_mail_builds_html_recipients_and_inline_attachments(
    graph: FakeGraph, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_text("hello")
    graph.on("POST", r"/me/sendMail$", None, status=202)
    out = _call(
        m.ms365_send_mail,
        to="Ana Ruiz <ana@x.com>; bo@y.com",
        subject="Your session has expired",
        body="Hi Ana,\n\nThe numbers <are> in.",
        cc="cc@x.com",
        attachments="notes.txt",
        importance="high",
    )
    msg = FakeGraph.body(graph.sent("POST", r"/sendMail$")[0])["message"]
    assert msg["toRecipients"] == [
        {"emailAddress": {"address": "ana@x.com", "name": "Ana Ruiz"}},
        {"emailAddress": {"address": "bo@y.com"}},
    ]
    assert msg["body"] == {
        "contentType": "HTML",
        "content": "<p>Hi Ana,</p><p>The numbers &lt;are&gt; in.</p>",
    }
    assert msg["importance"] == "high"
    assert msg["attachments"][0]["name"] == "notes.txt"
    # The result names recipients only: an echoed subject that reads like a
    # refused sign-in would make the error boundary retry, i.e. send twice.
    assert "expired" not in out
    assert out.startswith("Sent Microsoft 365 email to ana@x.com, bo@y.com")


def test_the_body_descriptions_tell_the_model_html_and_the_image_routes() -> None:
    # The recipient of a Markdown body reads literal asterisks: every composing
    # tool's body parameter must say HTML, say Markdown is not rendered, and
    # name both image routes, so the model can pick one before writing.
    for tool, field in (
        (m.ms365_send_mail, "body"),
        (m.ms365_create_draft, "body"),
        (m.ms365_update_draft, "body"),
        (m.ms365_reply, "body"),
        (m.ms365_forward, "comment"),
    ):
        desc = tool.args_schema.model_json_schema()["properties"][field]["description"]
        assert "HTML" in desc and "Markdown is NOT rendered" in desc, tool.name
        assert "cid:FILENAME" in desc and "https://" in desc, tool.name
        images = tool.args_schema.model_json_schema()["properties"]
        key = "add_inline_images" if tool is m.ms365_update_draft else "inline_images"
        assert "cid:logo.png" in images[key]["description"], tool.name


def test_send_mail_embeds_inline_images_by_file_name(graph: FakeGraph, tmp_path: Path) -> None:
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    (tmp_path / "deck.pdf").write_bytes(b"%PDF")
    graph.on("POST", r"/me/sendMail$", None, status=202)
    out = _call(
        m.ms365_send_mail,
        to="ana@x.com",
        subject="s",
        body='<p>Hi</p><img src="cid:logo.png" alt="logo"><p>Bye</p>',
        attachments="deck.pdf",
        inline_images="logo.png",
    )
    msg = FakeGraph.body(graph.sent("POST", r"/sendMail$")[0])["message"]
    # HTML passes through untouched, so the cid reference survives.
    assert msg["body"]["content"] == '<p>Hi</p><img src="cid:logo.png" alt="logo"><p>Bye</p>'
    by_name = {a["name"]: a for a in msg["attachments"]}
    assert by_name["logo.png"]["isInline"] is True
    assert by_name["logo.png"]["contentId"] == "logo.png"
    assert by_name["logo.png"]["contentType"] == "image/png"
    assert "isInline" not in by_name["deck.pdf"] and "contentId" not in by_name["deck.pdf"]
    assert "1 attachment(s); 1 inline image(s)" in out


@pytest.mark.parametrize(
    ("body", "inline_images", "wording"),
    [
        # The picture would be a stray attachment, never shown.
        ("<p>Hi</p>", "logo.png", "not shown anywhere in the body"),
        # The reference would render as a broken picture.
        ('<img src="cid:banner.png">', "logo.png", "references cid:banner.png"),
        ('<img src="cid:banner.png">', "", "references cid:banner.png"),
    ],
)
def test_inline_images_and_cid_references_must_match(
    graph: FakeGraph, tmp_path: Path, body: str, inline_images: str, wording: str
) -> None:
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    out = _call(
        m.ms365_send_mail, to="ana@x.com", subject="s", body=body, inline_images=inline_images
    )
    assert wording in out
    assert graph.sent("POST", r"/sendMail$") == []


def test_an_inline_image_must_be_an_image(graph: FakeGraph, tmp_path: Path) -> None:
    (tmp_path / "deck.pdf").write_bytes(b"%PDF")
    out = _call(
        m.ms365_send_mail,
        to="ana@x.com",
        subject="s",
        body='<img src="cid:deck.pdf">',
        inline_images="deck.pdf",
    )
    assert "is not an image" in out and "attachments instead" in out
    assert graph.requests == []


def test_create_draft_posts_inline_images_as_inline_attachments(
    graph: FakeGraph, tmp_path: Path
) -> None:
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    graph.on("POST", r"/me/messages$", {"id": "D1", "toRecipients": []}, status=201)
    graph.on("POST", r"/me/messages/D1/attachments$", {"id": "A1"}, status=201)
    out = _call(
        m.ms365_create_draft,
        to="ana@x.com",
        subject="s",
        body='<p>Our logo:</p><img src="cid:logo.png">',
        inline_images="logo.png",
    )
    posted = FakeGraph.body(graph.sent("POST", r"/D1/attachments$")[0])
    assert posted["isInline"] is True and posted["contentId"] == "logo.png"
    assert "not sent" in out


def test_reply_with_an_inline_image_attaches_it_to_the_reply_draft(
    graph: FakeGraph, tmp_path: Path
) -> None:
    (tmp_path / "chart.png").write_bytes(b"\x89PNG")
    graph.on("POST", r"/me/messages/M1/createReply$", {"id": "R1", "body": {}}, status=201)
    graph.on("PATCH", r"/me/messages/R1$", {"toRecipients": []})
    graph.on("POST", r"/me/messages/R1/attachments$", {"id": "A1"}, status=201)
    graph.on("POST", r"/me/messages/R1/send$", None, status=202)
    out = _call(
        m.ms365_reply,
        message_id="M1",
        body='<p>Here is the chart:</p><img src="cid:chart.png">',
        inline_images="chart.png",
    )
    assert out.startswith("Reply sent")
    posted = FakeGraph.body(graph.sent("POST", r"/R1/attachments$")[0])
    assert posted["isInline"] is True and posted["contentId"] == "chart.png"
    assert len(graph.sent("POST", r"/R1/send$")) == 1


def test_update_draft_adds_inline_images_without_a_new_body(
    graph: FakeGraph, tmp_path: Path
) -> None:
    # The kept body is not fetched, so the cid check only runs on a body given here.
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    graph.on("POST", r"/me/messages/D1/attachments$", {"id": "A1"}, status=201)
    out = _call(m.ms365_update_draft, draft_id="D1", add_inline_images="logo.png")
    assert "added 1 inline image(s)" in out
    assert graph.sent("PATCH", r"/me/messages/D1$") == []
    out = _call(
        m.ms365_update_draft, draft_id="D1", body="<p>no picture</p>", add_inline_images="logo.png"
    )
    assert "not shown anywhere in the body" in out


def test_send_mail_refuses_a_bare_name(graph: FakeGraph) -> None:
    out = _call(m.ms365_send_mail, to="Maria", subject="s", body="b")
    assert "ms365_find_people" in out
    assert graph.sent("POST", r"/sendMail$") == []


def test_send_mail_with_a_large_attachment_drafts_uploads_and_sends(
    graph: FakeGraph, tmp_path: Path
) -> None:
    big = tmp_path / "deck.pdf"
    big.write_bytes(b"x" * (m._UPLOAD_CHUNK + 10))
    graph.on("POST", r"/me/messages$", {"id": "D1"}, status=201)
    graph.on(
        "POST",
        r"/me/messages/D1/attachments/createUploadSession$",
        {"uploadUrl": "https://outlook.office.com/upload/abc"},
    )
    graph.on("PUT", r"outlook.office.com/upload/abc$", None, status=201)
    graph.on("POST", r"/me/messages/D1/send$", None, status=202)

    out = _call(m.ms365_send_mail, to="ana@x.com", subject="s", body="b", attachments=str(big))

    assert out.startswith("Sent Microsoft 365 email to ana@x.com")
    assert graph.sent("POST", r"/sendMail$") == []
    session = FakeGraph.body(graph.sent("POST", r"createUploadSession$")[0])
    assert session["AttachmentItem"]["size"] == big.stat().st_size
    puts = graph.sent("PUT", r"/upload/abc$")
    assert len(puts) == 2  # one full chunk, then the last 10 bytes
    assert all("authorization" not in p.headers for p in puts)
    assert puts[-1].headers["content-range"].endswith(f"/{big.stat().st_size}")
    assert graph.sent("POST", r"/D1/send$")[0].headers["content-length"] == "0"


def test_a_failed_send_discards_the_draft_it_made(graph: FakeGraph) -> None:
    graph.on("POST", r"/me/messages/M1/createReply$", {"id": "R1", "body": {}}, status=201)
    graph.on("PATCH", r"/me/messages/R1$", {"toRecipients": []})
    graph.on("POST", r"/me/messages/R1/send$", {"error": {"code": "boom"}}, status=500)
    graph.on("DELETE", r"/me/messages/R1$", None, status=204)
    out = _call(m.ms365_reply, message_id="M1", body="Thanks!")
    assert "500" in out
    assert len(graph.sent("DELETE", r"/me/messages/R1$")) == 1


def test_create_draft_reply_keeps_the_quoted_thread_below_the_new_text(graph: FakeGraph) -> None:
    graph.on(
        "POST",
        r"/me/messages/M1/createReplyAll$",
        {
            "id": "R1",
            "webLink": "https://outlook.office.com/owa/?ItemID=R1",
            "toRecipients": [{"emailAddress": {"address": "ana@x.com"}}],
            "body": {"contentType": "html", "content": "<html><body><hr>quoted</body></html>"},
        },
        status=201,
    )
    graph.on("PATCH", r"/me/messages/R1$", {})
    out = _call(
        m.ms365_create_draft, reply_to_message_id="M1", mode="reply_all", body="Sounds good."
    )
    patch = FakeGraph.body(graph.sent("PATCH", r"/me/messages/R1$")[0])
    assert patch["body"]["content"] == "<html><body><p>Sounds good.</p><hr>quoted</body></html>"
    assert graph.sent("POST", r"/send$") == []
    assert "not sent" in out and "https://outlook.office.com/owa/?ItemID=R1" in out


def test_send_draft_refuses_an_already_sent_message(graph: FakeGraph) -> None:
    graph.on("GET", r"/me/messages/X1$", {"isDraft": False})
    assert "already sent" in _call(m.ms365_send_draft, draft_id="X1")
    assert graph.sent("POST", r"/send$") == []


def test_update_message_flag_due_sets_start_and_due(graph: FakeGraph) -> None:
    graph.on("PATCH", r"/me/messages/M1$", {})
    _call(m.ms365_update_message, message_id="M1", is_read=True, flag_due_date="2099-01-02")
    patch = FakeGraph.body(graph.sent("PATCH", r"/me/messages/M1$")[0])
    assert patch["isRead"] is True
    assert patch["flag"]["flagStatus"] == "flagged"
    assert patch["flag"]["dueDateTime"] == {
        "dateTime": "2099-01-02T00:00:00",
        "timeZone": "Pacific Standard Time",
    }
    assert "startDateTime" in patch["flag"]


def test_download_attachment_sanitizes_the_sender_chosen_name(
    graph: FakeGraph, tmp_path: Path
) -> None:
    graph.on(
        "GET",
        r"/me/messages/M1/attachments$",
        {
            "value": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "id": "A1",
                    "name": "../../.bashrc",
                    "contentType": "text/plain",
                    "size": 5,
                }
            ]
        },
    )
    graph.on(
        "GET",
        r"/me/messages/M1/attachments/A1/\$value$",
        lambda r: httpx.Response(200, content=b"hello"),
    )
    out = _call(m.ms365_download_attachment, message_id="M1")
    saved = [p for p in (tmp_path / "downloads").iterdir()]
    assert len(saved) == 1 and saved[0].read_bytes() == b"hello"
    assert saved[0].parent == tmp_path / "downloads"
    assert f"Saved: {saved[0]}" in out


_INLINE_AND_FILE = {
    "value": [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "A1",
            "name": "screenshot.png",
            "contentType": "image/png",
            "size": 4,
            "isInline": True,
        },
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "A2",
            "name": "invoice.pdf",
            "contentType": "application/pdf",
            "size": 4,
            "isInline": False,
        },
    ]
}


def test_get_message_lists_inline_pictures_even_when_graph_says_no_attachments(
    graph: FakeGraph,
) -> None:
    # A screenshot pasted into the body is an inline attachment, and Graph's
    # hasAttachments is false for a message whose attachments are all inline.
    graph.on(
        "GET",
        r"/me/messages/M1$",
        {"id": "M1", "subject": "Look", "hasAttachments": False, "body": {"content": "See:"}},
    )
    graph.on(
        "GET",
        r"/me/messages/M1/attachments$",
        {"value": [dict(_INLINE_AND_FILE["value"][0])]},
    )
    out = _call(m.ms365_get_message, message_id="M1")
    assert "Attachments (1)" in out
    assert (
        "screenshot.png (image/png, 4 B, inline image, shown inside the body) attachment_id: A1"
        in out
    )
    assert len(graph.sent("GET", r"/M1/attachments$")) == 1


def test_download_attachment_saves_inline_pictures_unless_told_not_to(
    graph: FakeGraph, tmp_path: Path
) -> None:
    graph.on("GET", r"/me/messages/M1/attachments$", _INLINE_AND_FILE)
    graph.on(
        "GET",
        r"/me/messages/M1/attachments/A[12]/\$value$",
        lambda r: httpx.Response(200, content=b"data"),
    )
    out = _call(m.ms365_download_attachment, message_id="M1")
    names = sorted(p.name for p in (tmp_path / "downloads").iterdir())
    assert names == ["invoice.pdf", "screenshot.png"]
    assert "screenshot.png (4 B, inline image from the body)" in out
    out = _call(m.ms365_download_attachment, message_id="M1", include_inline=False)
    assert "invoice" in out and "screenshot" not in out
    # One picked by id is saved whether or not it is inline.
    out = _call(m.ms365_download_attachment, message_id="M1", attachment_id="A1")
    assert "screenshot-1.png" in out  # the earlier copy is kept, never overwritten


def test_downloads_refuse_a_folder_outside_the_workspace(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/messages/M1/attachments$",
        {
            "value": [
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A1", "name": "a.txt"}
            ]
        },
    )
    out = _call(m.ms365_download_attachment, message_id="M1", save_dir="/etc")
    assert "must be inside the workspace" in out


# ── calendar ─────────────────────────────────────────────────────────────────


def test_create_event_invites_adds_teams_and_repeats(graph: FakeGraph) -> None:
    graph.on(
        "POST",
        r"/me/events$",
        {
            "id": "E1",
            "subject": "Sync",
            "start": {"dateTime": "2026-10-05T09:00:00.0000000"},
            "end": {"dateTime": "2026-10-05T09:30:00.0000000"},
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/1"},
        },
        status=201,
    )
    out = _call(
        m.ms365_create_event,
        subject="Sync",
        start_datetime="2026-10-05T09:00",
        end_datetime="2026-10-05T09:30",
        attendees="ana@x.com, Bo <bo@y.com>",
        optional_attendees="cy@z.com",
        online_meeting=True,
        recurrence="weekly",
        recurrence_days="mon,thu",
        recurrence_count=6,
    )
    payload = FakeGraph.body(graph.sent("POST", r"/me/events$")[0])
    assert payload["start"] == {
        "dateTime": "2026-10-05T09:00:00",
        "timeZone": "Pacific Standard Time",
    }
    assert [(a["emailAddress"]["address"], a["type"]) for a in payload["attendees"]] == [
        ("ana@x.com", "required"),
        ("bo@y.com", "required"),
        ("cy@z.com", "optional"),
    ]
    assert payload["isOnlineMeeting"] is True
    assert payload["onlineMeetingProvider"] == "teamsForBusiness"
    assert payload["recurrence"] == {
        "pattern": {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["monday", "thursday"],
            "firstDayOfWeek": "sunday",
        },
        "range": {"startDate": "2026-10-05", "type": "numbered", "numberOfOccurrences": 6},
    }
    assert payload["transactionId"]
    assert "Invitations sent to: ana@x.com, bo@y.com, cy@z.com" in out
    assert "https://teams.microsoft.com/l/meetup-join/1" in out


def test_a_retried_create_reuses_its_transaction_id(graph: FakeGraph) -> None:
    graph.on("POST", r"/me/events$", {"id": "E1"}, status=201)
    args = {
        "subject": "S",
        "start_datetime": "2026-10-05T09:00",
        "end_datetime": "2026-10-05T10:00",
    }
    _call(m.ms365_create_event, **args)
    _call(m.ms365_create_event, **args)
    first, second = (FakeGraph.body(r)["transactionId"] for r in graph.sent("POST", r"/me/events$"))
    assert first == second
    _call(m.ms365_create_event, **{**args, "subject": "Other"})
    assert FakeGraph.body(graph.sent("POST", r"/me/events$")[-1])["transactionId"] != first


def test_a_duplicate_create_reads_as_already_booked(graph: FakeGraph) -> None:
    graph.on("POST", r"/me/events$", {"error": {"code": "ErrorDuplicateTransactionId"}}, status=409)
    out = _call(
        m.ms365_create_event,
        subject="S",
        start_datetime="2026-10-05T09:00",
        end_datetime="2026-10-05T10:00",
    )
    assert "not booked again" in out


@pytest.mark.parametrize(
    ("time_zone", "expected"),
    [("America/Costa_Rica", "2026-10-05T09:00:00"), ("UTC", "2026-10-05T15:00:00")],
)
def test_an_offset_start_is_converted_into_the_event_zone(
    graph: FakeGraph, time_zone: str, expected: str
) -> None:
    graph.on("POST", r"/me/events$", {"id": "E1"}, status=201)
    _call(
        m.ms365_create_event,
        subject="S",
        start_datetime="2026-10-05T09:00:00-06:00",
        end_datetime="2026-10-05T10:00:00-06:00",
        time_zone=time_zone,
    )
    start = FakeGraph.body(graph.sent("POST", r"/me/events$")[0])["start"]
    assert start == {"dateTime": expected, "timeZone": time_zone}


def test_an_all_day_event_ends_the_midnight_after_its_last_day(graph: FakeGraph) -> None:
    graph.on("POST", r"/me/events$", {"id": "E1"}, status=201)
    _call(
        m.ms365_create_event,
        subject="Offsite",
        start_datetime="2026-10-05",
        end_datetime="2026-10-06",
        is_all_day=True,
    )
    payload = FakeGraph.body(graph.sent("POST", r"/me/events$")[0])
    assert payload["start"]["dateTime"] == "2026-10-05T00:00:00"
    assert payload["end"]["dateTime"] == "2026-10-07T00:00:00"


def test_list_events_reads_a_calendar_view_in_the_mailbox_zone(graph: FakeGraph) -> None:
    graph.on("GET", r"/me/calendarView$", {"value": []})
    _call(m.ms365_list_events, start="2026-10-05", end="2026-10-05", time_zone="UTC")
    request = graph.sent("GET", r"/calendarView$")[0]
    assert request.url.params["startDateTime"] == "2026-10-05T00:00:00Z"
    assert request.url.params["endDateTime"] == "2026-10-06T00:00:00Z"
    assert request.url.params["$orderby"] == "start/dateTime"
    assert 'outlook.timezone="UTC"' in request.headers["prefer"]


def test_update_event_keeps_the_length_and_merges_attendees(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/events/E1$",
        {
            "isOrganizer": True,
            "start": {"dateTime": "2026-10-05T16:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-05T16:45:00.0000000", "timeZone": "UTC"},
            "attendees": [
                {"emailAddress": {"address": "ana@x.com"}, "type": "required"},
                {"emailAddress": {"address": "old@x.com"}, "type": "required"},
            ],
        },
    )
    graph.on("PATCH", r"/me/events/E1$", {"attendees": [{}]})
    _call(
        m.ms365_update_event,
        event_id="E1",
        start_datetime="2026-10-06T10:00",
        time_zone="UTC",
        add_attendees="new@x.com, ana@x.com",
        remove_attendees="old@x.com",
    )
    patch = FakeGraph.body(graph.sent("PATCH", r"/me/events/E1$")[0])
    assert patch["start"] == {"dateTime": "2026-10-06T10:00:00", "timeZone": "UTC"}
    assert patch["end"] == {"dateTime": "2026-10-06T10:45:00", "timeZone": "UTC"}
    assert [a["emailAddress"]["address"] for a in patch["attendees"]] == ["ana@x.com", "new@x.com"]


def test_only_the_organizer_can_change_attendees(graph: FakeGraph) -> None:
    graph.on("GET", r"/me/events/E1$", {"isOrganizer": False, "attendees": []})
    out = _call(m.ms365_update_event, event_id="E1", add_attendees="x@y.com")
    assert "only the organizer" in out
    assert graph.sent("PATCH", r"/events/E1$") == []


def test_deleting_an_organized_meeting_cancels_it_with_the_note(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/events/E1$",
        {"isOrganizer": True, "attendees": [{"emailAddress": {"address": "a@x.com"}}]},
    )
    graph.on("POST", r"/me/events/E1/cancel$", None, status=202)
    out = _call(m.ms365_delete_event, event_id="E1", cancellation_message="Moving to next week")
    assert FakeGraph.body(graph.sent("POST", r"/cancel$")[0]) == {"Comment": "Moving to next week"}
    assert graph.sent("DELETE", r"/events/E1$") == []
    assert "1 attendee(s) were notified" in out


def test_deleting_someone_elses_meeting_says_the_organizer_was_not_told(graph: FakeGraph) -> None:
    graph.on("GET", r"/me/events/E1$", {"isOrganizer": False, "attendees": [{}]})
    graph.on("DELETE", r"/me/events/E1$", None, status=204)
    assert "ms365_respond_to_event" in _call(m.ms365_delete_event, event_id="E1")


def test_respond_only_proposes_a_time_when_declining_or_tentative(graph: FakeGraph) -> None:
    out = _call(
        m.ms365_respond_to_event,
        event_id="E1",
        response="accept",
        propose_new_start="2026-10-06T10:00",
        propose_new_end="2026-10-06T11:00",
    )
    assert "tentative or decline" in out
    graph.on("POST", r"/me/events/E1/decline$", None, status=202)
    _call(
        m.ms365_respond_to_event,
        event_id="E1",
        response="decline",
        comment="Clash",
        propose_new_start="2026-10-06T10:00",
        propose_new_end="2026-10-06T11:00",
    )
    body = FakeGraph.body(graph.sent("POST", r"/decline$")[0])
    assert body["proposedNewTime"]["start"]["dateTime"] == "2026-10-06T10:00:00"
    assert body["sendResponse"] is True


def test_find_meeting_times_asks_for_every_attendee_as_required(graph: FakeGraph) -> None:
    graph.on(
        "POST",
        r"/me/findMeetingTimes$",
        {
            "meetingTimeSuggestions": [
                {
                    "confidence": 100,
                    "meetingTimeSlot": {
                        "start": {"dateTime": "2026-10-06T10:00:00.0000000"},
                        "end": {"dateTime": "2026-10-06T10:30:00.0000000"},
                    },
                }
            ]
        },
    )
    out = _call(m.ms365_find_meeting_times, attendees="ana@x.com,bo@y.com", duration_minutes=30)
    body = FakeGraph.body(graph.sent("POST", r"/findMeetingTimes$")[0])
    assert body["meetingDuration"] == "PT30M"
    assert {a["type"] for a in body["attendees"]} == {"required"}
    assert "2026-10-06 10:00-10:30" in out


# ── people, contacts, settings ───────────────────────────────────────────────


def test_find_people_merges_sources_and_dedupes_by_address(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/people$",
        {
            "value": [
                {
                    "displayName": "Maria Lopez",
                    "jobTitle": "CFO",
                    "scoredEmailAddresses": [{"address": "maria@x.com"}],
                }
            ]
        },
    )
    graph.on(
        "GET",
        r"/users$",
        {
            "value": [
                {"displayName": "Maria Lopez", "mail": "MARIA@x.com"},
                {"displayName": "Maria Diaz", "mail": "md@x.com"},
            ]
        },
    )
    graph.on(
        "GET",
        r"/me/contacts$",
        {
            "value": [
                {"displayName": "Maria Vendor", "emailAddresses": [{"address": "maria@vendor.com"}]}
            ]
        },
    )
    out = _call(m.ms365_find_people, query="maria")
    assert out.count("maria@x.com") == 1 and "md@x.com" in out and "maria@vendor.com" in out
    assert graph.sent("GET", r"/users$")[0].headers["consistencylevel"] == "eventual"


def test_auto_reply_scheduled_needs_both_ends_and_reuses_internal_text(graph: FakeGraph) -> None:
    assert "both start and end" in _call(
        m.ms365_set_auto_reply, mode="scheduled", internal_message="Away"
    )
    graph.on("PATCH", r"/me/mailboxSettings$", {})
    _call(
        m.ms365_set_auto_reply,
        mode="scheduled",
        internal_message="Away until Monday",
        start="2099-10-10T18:00",
        end="2099-10-13T08:00",
    )
    setting = FakeGraph.body(graph.sent("PATCH", r"/mailboxSettings$")[0])[
        "automaticRepliesSetting"
    ]
    assert setting["status"] == "scheduled"
    assert setting["externalReplyMessage"] == "<p>Away until Monday</p>"
    assert setting["scheduledStartDateTime"]["timeZone"] == "Pacific Standard Time"


# ── files ────────────────────────────────────────────────────────────────────


def test_upload_addresses_a_folder_path_and_conflict_behaviour(
    graph: FakeGraph, tmp_path: Path
) -> None:
    (tmp_path / "r.csv").write_text("a,b")
    graph.on(
        "PUT",
        r"/me/drive/root:/Docs/Q3 files/r.csv:/content$",
        {"id": "I1", "name": "r.csv"},
        status=201,
    )
    out = _call(
        m.ms365_upload_drive_file, local_path="r.csv", folder="/Docs/Q3 files", conflict="replace"
    )
    request = graph.sent("PUT", r"/content$")[0]
    assert "/me/drive/root:/Docs/Q3%20files/r.csv:/content" in str(request.url)
    assert request.url.params["@microsoft.graph.conflictBehavior"] == "replace"
    assert request.content == b"a,b"
    assert "Uploaded r.csv" in out


def test_search_drive_escapes_quotes(graph: FakeGraph) -> None:
    graph.on("GET", r"/me/drive/root/search", {"value": []})
    _call(m.ms365_search_drive, query="O'Brien #2")
    assert "search(q='O%27%27Brien%20%232')" in str(graph.requests[-1].url)


def test_download_drive_file_fetches_the_preauthenticated_url_without_the_token(
    graph: FakeGraph, tmp_path: Path
) -> None:
    graph.on(
        "GET",
        r"/me/drive/items/I1$",
        {
            "name": "Q3.xlsx",
            "file": {"mimeType": "application/vnd.ms-excel"},
            "@microsoft.graph.downloadUrl": "https://tenant.sharepoint.com/download/q3?tempauth=x",
        },
    )
    graph.on(
        "GET", r"tenant.sharepoint.com/download/q3$", lambda r: httpx.Response(200, content=b"XLSX")
    )
    out = _call(m.ms365_download_drive_file, item="I1")
    fetch = graph.sent("GET", r"sharepoint.com/download/q3$")[0]
    assert "authorization" not in fetch.headers
    assert (tmp_path / "downloads" / "Q3.xlsx").read_bytes() == b"XLSX"
    assert "Saved: " in out


def test_share_with_people_invites_them_with_the_role(graph: FakeGraph) -> None:
    graph.on("POST", r"/me/drive/root:/Docs/a.docx:/invite$", {"value": []})
    out = _call(
        m.ms365_share_drive_item, item="/Docs/a.docx", access="edit", recipients="ana@x.com"
    )
    body = FakeGraph.body(graph.sent("POST", r"/invite$")[0])
    assert body["roles"] == ["write"] and body["recipients"] == [{"email": "ana@x.com"}]
    assert "Invited ana@x.com to edit" in out


def test_excel_write_expands_a_top_left_cell_to_the_block(graph: FakeGraph) -> None:
    graph.on("PATCH", r"/workbook/worksheets/Sales 2026/range", {"address": "'Sales 2026'!Y9:AA10"})
    out = _call(
        m.ms365_excel_write_range,
        item="I1",
        sheet="Sales 2026",
        address="Y9",
        values='[["a", 1, "=SUM(1,2)"], ["b", 2]]',
    )
    request = graph.sent("PATCH", r"/range")[0]
    assert "/worksheets/Sales%202026/range(address='Y9:AA10')" in str(request.url)
    assert FakeGraph.body(request)["values"] == [["a", 1, "=SUM(1,2)"], ["b", 2, ""]]
    assert "Wrote 2 x 3 cells" in out


# ── Teams and To Do ──────────────────────────────────────────────────────────


def test_chat_to_a_person_opens_the_one_on_one_then_posts(graph: FakeGraph) -> None:
    graph.on("GET", r"/v1.0/me$", {"id": "ME-ID"})
    graph.on("POST", r"/v1.0/chats$", {"id": "19:abc@unq.gbl.spaces"}, status=201)
    graph.on("POST", r"/chats/19:abc@unq.gbl.spaces/messages$", {"id": "1"}, status=201)
    out = _call(m.ms365_send_chat_message, message="Ping", to="ana@x.com")
    chat = FakeGraph.body(graph.sent("POST", r"/v1.0/chats$")[0])
    assert chat["chatType"] == "oneOnOne"
    assert [mem["user@odata.bind"] for mem in chat["members"]] == [
        "https://graph.microsoft.com/v1.0/users('ME-ID')",
        "https://graph.microsoft.com/v1.0/users('ana@x.com')",
    ]
    # No chat id in a send result: a hex id containing "401" would read as a
    # refused sign-in and the boundary would send the message again.
    assert out == "Sent a Teams chat message to ana@x.com."


def test_tasks_default_to_the_well_known_list(graph: FakeGraph) -> None:
    graph.on(
        "GET",
        r"/me/todo/lists$",
        {
            "value": [
                {"id": "L-OTHER", "displayName": "Groceries"},
                {"id": "L-DEF", "displayName": "Tasks", "wellknownListName": "defaultList"},
            ]
        },
    )
    graph.on("POST", r"/me/todo/lists/L-DEF/tasks$", {"id": "T1"}, status=201)
    _call(m.ms365_create_task, title="Send deck", due_date="2026-10-09", importance="high")
    body = FakeGraph.body(graph.sent("POST", r"/tasks$")[0])
    assert body["dueDateTime"] == {
        "dateTime": "2026-10-09T00:00:00",
        "timeZone": "Pacific Standard Time",
    }
    assert body["importance"] == "high"


# ── wiring ───────────────────────────────────────────────────────────────────


def test_sends_and_invites_need_approval_but_drafts_do_not() -> None:
    from olano.security.hitl_policy import CATEGORY_TOOLS

    gated = CATEGORY_TOOLS["write_external"]
    for name in (
        "ms365_send_mail",
        "ms365_send_draft",
        "ms365_reply",
        "ms365_forward",
        "ms365_create_event",
        "ms365_respond_to_event",
        "ms365_share_drive_item",
        "ms365_send_chat_message",
    ):
        assert name in gated, f"{name} lost its approval gate"
    for name in (
        "ms365_create_draft",
        "ms365_update_draft",
        "ms365_list_messages",
        "ms365_create_task",
    ):
        assert name not in gated
    assert {n for n in gated if n.startswith("ms365_")} <= {t.name for t in m.get_tools()}


def test_the_subagent_carries_every_tool_but_the_decode_helper() -> None:
    from olano.config.json5_fast import loads

    definition = loads(
        (Path(m.__file__).parents[1] / "subagents/definitions/microsoft365.json5").read_text()
    )
    tools = {t.name for t in m.get_tools()}
    assert set(definition["default_tools"]) == tools - {"ms365_decode_file_content"}


def test_the_connector_requests_no_admin_only_scope() -> None:
    from olano.core.connectors.providers import microsoft365  # noqa: F401  (registers it)
    from olano.core.connectors.registry import get_provider

    scopes = set(get_provider("microsoft365").scopes)
    assert {
        "Mail.ReadWrite",
        "Calendars.Read.Shared",
        "Chat.ReadWrite",
        "Tasks.ReadWrite",
    } <= scopes
    assert "ChannelMessage.Read.All" not in scopes
