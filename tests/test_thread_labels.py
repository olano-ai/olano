"""Conversation label derivation.

Every literal in here is copied from the producer that emits it, so this suite
doubles as the record of which prefixes exist and where they come from. If a
producer changes its format string, the matching case here should fail.
"""

from __future__ import annotations

import pytest

from olano.core.thread_labels import (
    CHANNEL_CLI,
    CHANNEL_DASHBOARD,
    CHANNEL_SCHEDULER,
    CHANNEL_WEBHOOK,
    ORIGIN_AUTONOMOUS,
    ORIGIN_CORTEX,
    ORIGIN_EXTERNAL,
    ORIGIN_HEARTBEAT,
    ORIGIN_INTERACTIVE,
    ORIGIN_PEER,
    ORIGIN_SCHEDULED,
    channel_for_context,
    classify_thread,
    condense,
    derive_label,
    is_system_thread,
    split_sender_tag,
    strip_directives,
    strip_human_namespace,
)

# The exact string scheduler/cron.py:456-459 prepends.
AUTONOMOUS = (
    "[Autonomous scheduled task — execute directly and completely. "
    "Do NOT ask the user for confirmation or approval before acting.]\n\n"
)


def _entry(content: str, role: str = "user", **extra):
    return {"role": role, "content": content, **extra}


# ── strip_directives ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected", "sender"),
    [
        # platforms/session.py:format_sender_prefix — the screenshot's worst row.
        (
            "[from @jrivera (Jamie | Acme, id=5550001234)]: can you check the deadline",
            "can you check the deadline",
            "Jamie | Acme",
        ),
        # Handle only — no display name to recover.
        ("[from @jrivera]: hola", "hola", "jrivera"),
        # Display name only (Discord stores the handle in user_name).
        ("[from Carlos]: hola", "hola", "Carlos"),
        # Bot marker must not leak into the sender name.
        ("[from bot @relay (Relay, id=9)]: ping", "ping", "Relay"),
        # Identity with ONLY a numeric id — never surface the id as a name.
        ("[from id=5550001234]: hey", "hey", ""),
        # scheduler/cron.py autonomous preamble.
        (AUTONOMOUS + "Review the tender board", "Review the tender board", ""),
        # runtime/server.py:2246 stacks [think:…] OUTSIDE the sender tag.
        ("[think:high] [from @ana (Ana, id=7)]: status?", "status?", "Ana"),
        # scheduler/cron.py:594 heartbeat, with its Context block dropped.
        ("HEARTBEAT: inbox triage\n\nContext: check unread", "inbox triage", ""),
        # platforms/email.py:446 — the subject is the better title.
        ("[Subject: Invoice #42]\n\nPlease see attached", "Invoice #42", ""),
        # webhooks/dispatcher.py:459.
        (
            "--- Incoming webhook payload: stripe ---\n```json\n{}\n```",
            "Webhook · stripe",
            "",
        ),
        # platforms/slack.py:1498 thread context, prepended ahead of the tag.
        (
            "[Thread context — prior messages in this thread (not yet in conversation "
            "history):]\n[thread parent] hi\n[End of thread context]\n\n"
            "[from @bo (Bo, id=3)]: and now?",
            "and now?",
            "Bo",
        ),
        # platforms/inbound.py:565 — prefer the transcript over "Voice message".
        (
            "[Voice message transcript] where are we with the filing",
            "where are we with the filing",
            "",
        ),
        # Media-only turns collapse to a friendly kind label.
        ("[Image attached: shot.png — saved to /w/shot.png (image/png, 4 KB)]", "Image", ""),
        ("[Voice message received, saved to /tmp/a.ogg]", "Voice message", ""),
        ("[Attachment received: /tmp/x.bin (unknown type)]", "Attachment", ""),
        # platforms/inbound.py:78 /start greeting — very common first message.
        (
            "[The user just opened this chat — their messaging app sent its /start "
            'command. Greet them briefly.]\n[The user\'s name is "Ana".]',
            "",
            "",
        ),
        # No directive at all — untouched.
        ("can you check other jobs in openai", "can you check other jobs in openai", ""),
    ],
)
def test_strip_directives(raw, expected, sender):
    text, found = strip_directives(raw)
    assert text == expected
    assert found == sender


def test_strip_directives_survives_a_prefix_only_message():
    """The 120-char cut used to yield a title that was 100% prefix."""
    text, sender = strip_directives("[from @jrivera (Jamie | Acme, id=5550001234)]:")
    assert text == ""
    assert sender == "Jamie | Acme"


# ── condense ─────────────────────────────────────────────────────────────────


def test_condense_collapses_whitespace_to_one_line():
    assert condense("hello\n\n  world\ttab") == "hello world tab"


def test_condense_truncates_on_a_word_boundary():
    out = condense("alpha bravo charlie delta echo foxtrot", max_len=20)
    assert out == "alpha bravo charlie…"
    assert len(out) <= 21


def test_condense_hard_cuts_when_there_is_no_usable_space():
    # A single long token has no boundary past the 60% mark — cut it anyway
    # rather than returning a wildly over-length title.
    assert condense("x" * 40, max_len=10) == "xxxxxxxxxx…"


# ── classify_thread ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("thread_id", "origin", "channel"),
    [
        ("heartbeat_merli@20260101T000000-ab12", ORIGIN_HEARTBEAT, CHANNEL_SCHEDULER),
        ("scheduled_merli_1a2b3c4d@20260101T000000-ab12", ORIGIN_SCHEDULED, CHANNEL_SCHEDULER),
        ("cog::merli", ORIGIN_AUTONOMOUS, ""),
        ("kb-console-1f0c2a9e-7b1d-4c55-9a53-0d2e8f6a1b77", ORIGIN_AUTONOMOUS, ""),
        ("acp::marcus::luna", ORIGIN_PEER, ""),
        ("delegate::merli::sub", ORIGIN_PEER, ""),
        ("agent:main:telegram:dm:5550001234", ORIGIN_INTERACTIVE, "telegram"),
        ("agent:main:discord:group:99:12", ORIGIN_INTERACTIVE, "discord"),
        ("tg_9f8e7d6c", ORIGIN_INTERACTIVE, "telegram"),
        ("3f6a1b2c-0000-4000-8000-000000000000", ORIGIN_INTERACTIVE, CHANNEL_DASHBOARD),
        # The issue poll (scheduler/cron.py:_add_issues_job) is a scheduler firing.
        ("issues_merli@20260101T000000-ab12", ORIGIN_SCHEDULED, CHANNEL_SCHEDULER),
        # Opened from Cortex — a dashboard chat that says which surface started
        # it, so the rail can badge it. The ids are minted by the Pulse card and
        # the Cortex item hand-off (dashboard_2.0 views/cortex/chatHandoff.ts).
        ("pulse:a1b2c3d4", ORIGIN_CORTEX, CHANNEL_DASHBOARD),
        ("cortex:idea-2026-01-01-repeatable", ORIGIN_CORTEX, CHANNEL_DASHBOARD),
        ("u:abc123:cortex:idea-2026-01-01-repeatable", ORIGIN_CORTEX, CHANNEL_DASHBOARD),
        # Threads written under the retired per-human namespace — including the
        # forks a dashboard reply inside a run used to create — keep their shape.
        (
            "u:abc123:scheduled_merli_1a2b3c4d@20260101T000000-ab12",
            ORIGIN_SCHEDULED,
            CHANNEL_SCHEDULER,
        ),
        ("u:abc123:heartbeat_merli@20260101T000000-ab12", ORIGIN_HEARTBEAT, CHANNEL_SCHEDULER),
        ("u:abc123:acp::marcus::luna", ORIGIN_PEER, ""),
        ("u:abc123:3f6a1b2c-0000-4000-8000-000000000000", ORIGIN_INTERACTIVE, CHANNEL_DASHBOARD),
    ],
)
def test_classify_thread_from_id_shape(thread_id, origin, channel):
    assert classify_thread(thread_id, None) == (origin, channel)


def test_strip_human_namespace():
    assert strip_human_namespace("u:abc123:scheduled_merli_1a2b@x") == "scheduled_merli_1a2b@x"
    # One layer only: the dashboard prepended exactly one, so a second is content.
    assert strip_human_namespace("u:abc123:u:other:t") == "u:other:t"
    assert strip_human_namespace("scheduled_merli_1a2b@x") == "scheduled_merli_1a2b@x"
    assert strip_human_namespace("") == ""


def test_structured_fields_beat_the_id_shape():
    """New transcripts carry origin/channel — they win over id sniffing."""
    entry = _entry("hi", origin=ORIGIN_INTERACTIVE, channel="whatsapp")
    assert classify_thread("scheduled_merli@x", entry) == (ORIGIN_INTERACTIVE, "whatsapp")


def test_peer_inbound_role_classifies_as_peer():
    assert classify_thread("uuid-1", _entry("ping", role="peer_inbound")) == (ORIGIN_PEER, "")


def test_is_system_thread():
    assert is_system_thread("scheduled_merli_1a2b@x")
    assert is_system_thread("acp::a::b")
    assert is_system_thread("issues_merli@x")
    assert is_system_thread("u:abc123:scheduled_merli_1a2b@x")
    assert not is_system_thread("3f6a1b2c-0000-4000-8000-000000000000")
    assert not is_system_thread("u:abc123:3f6a1b2c-0000-4000-8000-000000000000")


# ── derive_label ─────────────────────────────────────────────────────────────


def test_scheduled_thread_is_named_by_its_task_not_its_prompt():
    """The whole point: identical preambles become distinct task names."""
    label = derive_label(
        "scheduled_merli_1a2b3c4d@20260101T000000-ab12",
        [_entry(AUTONOMOUS + "Sweep the SICOP board and report")],
        task_names={"1a2b3c4d": "Daily SICOP tender sweep"},
    )
    assert label.title == "Daily SICOP tender sweep"
    assert label.origin == ORIGIN_SCHEDULED
    assert label.channel == CHANNEL_SCHEDULER


def test_scheduled_thread_falls_back_to_the_prompt_without_a_task_name():
    label = derive_label(
        "scheduled_merli_1a2b3c4d@20260101T000000-ab12",
        [_entry(AUTONOMOUS + "Sweep the SICOP board")],
        task_names={},
    )
    assert label.title == "Sweep the SICOP board"


def test_agent_name_containing_underscores_still_resolves_its_task():
    label = derive_label(
        "scheduled_my_long_agent_1a2b3c4d@20260101T000000-ab12",
        [_entry(AUTONOMOUS + "go")],
        task_names={"1a2b3c4d": "Nightly sync"},
    )
    assert label.title == "Nightly sync"


def test_forked_scheduled_thread_keeps_its_task_name():
    """A dashboard reply inside a run used to fork it onto ``u:<uid>:scheduled_…``
    with the human's words as the first row. Those rows exist on real boxes and
    must still read as the run they came from — pill, channel and task name."""
    label = derive_label(
        "u:abc123:scheduled_merli_1a2b3c4d@20260101T000000-ab12",
        [_entry("what did you just do?")],
        task_names={"1a2b3c4d": "Daily SICOP tender sweep"},
    )
    assert label.title == "Daily SICOP tender sweep"
    assert label.origin == ORIGIN_SCHEDULED
    assert label.channel == CHANNEL_SCHEDULER


def test_issue_poll_thread_is_a_scheduled_run():
    label = derive_label("issues_merli@20260101T000000-ab12", [_entry("")])
    assert label.origin == ORIGIN_SCHEDULED
    assert label.channel == CHANNEL_SCHEDULER
    assert label.title == "Issue sweep"


def test_structured_task_name_wins_without_any_lookup():
    label = derive_label(
        "scheduled_merli_1a2b3c4d@x",
        [_entry("Sweep the board", task_name="Daily SICOP tender sweep")],
    )
    assert label.title == "Daily SICOP tender sweep"


def test_platform_thread_titles_by_the_humans_words():
    label = derive_label(
        "agent:main:telegram:dm:5550001234",
        [_entry("[from @jrivera (Jamie, id=5550001234)]: can you check the deadline")],
    )
    assert label.title == "can you check the deadline"
    assert label.sender == "Jamie"
    assert label.channel == "telegram"
    assert label.origin == ORIGIN_INTERACTIVE


def test_title_falls_back_to_an_origin_label_when_nothing_readable_survives():
    label = derive_label(
        "heartbeat_merli@20260101T000000-ab12",
        [_entry("HEARTBEAT:")],
    )
    assert label.title == "Heartbeat"
    assert label.origin == ORIGIN_HEARTBEAT


def test_peer_thread_uses_the_from_field_as_sender():
    label = derive_label(
        "acp::marcus::luna",
        [{"role": "peer_inbound", "from": "marcus", "content": "pricing check?"}],
    )
    assert label.title == "pricing check?"
    assert label.sender == "marcus"
    assert label.origin == ORIGIN_PEER


def test_first_user_turn_wins_over_a_leading_system_entry():
    label = derive_label(
        "uuid-1",
        [
            _entry("[Conversation compacted] Prior thread: old", role="system"),
            _entry("what changed since yesterday?"),
        ],
    )
    assert label.title == "what changed since yesterday?"


def test_thread_with_no_user_turn_falls_back_to_the_last_entry():
    label = derive_label("uuid-1", [_entry("all done", role="assistant")])
    assert label.title == "all done"


def test_empty_transcript_is_safe():
    label = derive_label("uuid-1", [])
    assert label.title == ""
    assert label.origin == ORIGIN_INTERACTIVE


# ── provenance: the write side ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "channel", "expected"),
    [
        # A platform's own ingress (runtime/server.py:_boot_platform_adapter).
        ("platform", "telegram", "telegram"),
        ("platform", "whatsapp", "whatsapp"),
        # scheduler/multi_user.py runs a fan-out as source="scheduled" with a
        # REAL platform channel. The platform has to win, or a Telegram
        # broadcast would record itself as having come from the scheduler.
        ("scheduled", "telegram", "telegram"),
        # …while an ordinary cron run has no platform and stays the scheduler.
        ("scheduled", "scheduler", CHANNEL_SCHEDULER),
        # The dashboard (runtime/auth.py:context_kwargs_for_request).
        ("api", "api", CHANNEL_DASHBOARD),
        ("webhook", "webhook", CHANNEL_WEBHOOK),
        ("cli", "cli", CHANNEL_CLI),
        # The origin pill already reads AGENT / AUTONOMOUS for these two, so a
        # channel beside it would be duplication, not information.
        ("peer", "talk_to_agent", ""),
        ("cognition", "cognition", ""),
        # THE one that must stay empty: "local" is the RequestContext default,
        # so it is indistinguishable from a context that was never bound — and
        # an SSE body generator routinely observes exactly that, because
        # Starlette's BaseHTTPMiddleware has already exited by then. Mapping it
        # to a channel would stamp every dashboard thread with the wrong one.
        ("local", "local", ""),
        ("", "", ""),
    ],
)
def test_channel_for_context(source, channel, expected):
    assert channel_for_context(source, channel) == expected


def test_channel_for_context_never_returns_an_origin():
    """The function's whole contract: channels only.

    ``RequestContext.source`` cannot express the origin taxonomy — one
    ``"scheduled"`` covers both a heartbeat poll and a scheduled task — so
    stamping origin from it would collapse distinctions ``classify_thread``
    still gets right from the thread id.
    """
    assert channel_for_context("scheduled", "scheduler") not in {
        ORIGIN_SCHEDULED,
        ORIGIN_HEARTBEAT,
    }


# ── provenance: the read side ────────────────────────────────────────────────


def test_stamped_channel_names_a_bare_uuid_thread():
    """The bug this whole path exists for.

    A platform conversation is persisted under the bare platform chat id
    (runtime/server.py: ``source.thread_id or source.chat_id``), so nothing in
    the file name says "WhatsApp" and the rail used to label it "Dashboard".
    """
    entry = _entry("hi", channel="whatsapp")
    assert classify_thread("3f6a1b2c-0000-4000-8000-000000000000", entry) == (
        ORIGIN_INTERACTIVE,
        "whatsapp",
    )


def test_stamped_channel_refines_a_scheduled_run():
    entry = _entry("go", channel="telegram")
    assert classify_thread("scheduled_merli_1a2b@x", entry) == (ORIGIN_SCHEDULED, "telegram")
    assert classify_thread("heartbeat_merli@x", entry) == (ORIGIN_HEARTBEAT, "telegram")


def test_channel_hint_backfills_only_when_nothing_else_knows():
    tid = "5550001234"  # a bare Telegram chat id
    assert classify_thread(tid, None, channel_hint="telegram") == (
        ORIGIN_INTERACTIVE,
        "telegram",
    )
    # A stamped channel is evidence from the transcript; the hint is a heuristic
    # join on a chat id, so it must never win.
    assert classify_thread(tid, _entry("hi", channel="slack"), channel_hint="telegram") == (
        ORIGIN_INTERACTIVE,
        "slack",
    )
    # …and it must not reach a thread whose id already classifies it.
    assert classify_thread("heartbeat_merli@x", None, channel_hint="telegram") == (
        ORIGIN_HEARTBEAT,
        CHANNEL_SCHEDULER,
    )


def test_provenance_survives_a_system_row_at_the_head():
    """entries[0] is not reliably the provenance row.

    The runner prepends `system` rows (a prompt-change notice, a recorded
    context note) ahead of the first human turn.
    """
    label = derive_label(
        "3f6a1b2c-0000-4000-8000-000000000000",
        [
            _entry("[Prompt updated]", role="system"),
            _entry("[from @jrivera (Jamie)]: yooo", channel="whatsapp"),
        ],
    )
    assert label.channel == "whatsapp"
    assert label.title == "yooo"


# ── redaction inside a runtime-authored tag ──────────────────────────────────


def test_redacted_sender_id_does_not_leak_into_the_title():
    r"""The regression from the rail screenshot.

    security/pii_middleware.py rewrites the phone in the runtime's own
    ``[from …]`` tag to ``[PHONE_US_REDACTED]``, putting a ``]`` INSIDE the tag.
    A ``[^\]]*`` body stopped there and titled the row ``)]: yooo``.
    """
    text = "[from @jrivera (Jamie | Acme, id=[PHONE_US_REDACTED])]: yooo"
    assert strip_directives(text) == ("yooo", "Jamie | Acme")


@pytest.mark.parametrize(
    "placeholder",
    ["[REDACTED]", "[PHONE_US_REDACTED]", "[REDACTED:api_key]"],
)
def test_every_redaction_placeholder_shape_is_tolerated(placeholder):
    text = f"[from @x (Y, id={placeholder})]: hello"
    assert strip_directives(text) == ("hello", "Y")


def test_a_body_that_merely_starts_with_a_bracket_is_not_swallowed():
    """The alternation admits an inner ``[`` only as a complete placeholder."""
    assert strip_directives("[from @x (Y, id=1)]: [Image attached: cat.png]") == ("Image", "Y")
    # An unterminated tag still fails to match rather than scanning the message.
    assert strip_directives("[from @x (Y, id=1) hello") == ("[from @x (Y, id=1) hello", "")


def test_group_chat_tag_is_peeled_too():
    """``format_chat_prefix`` stacks an ``[in …]`` tag after the sender tag.

    Nothing stripped it before, so every group conversation was titled with the
    room's own id.
    """
    text = '[from @x (Y, id=1)] [in group "Team", chat id=99]: ship it'
    assert strip_directives(text) == ("ship it", "Y")


# ── split_sender_tag ─────────────────────────────────────────────────────────


def test_split_sender_tag_keeps_the_body_intact():
    """The narrow peel the chat window needs.

    ``strip_directives`` is a TITLE builder: it replaces an email body with its
    subject line and a document body with its filename. Doing that in the
    transcript view would delete the message.
    """
    text = "[from Bob]: [Subject: Q3 numbers]\n\nthe actual email body"
    body, sender = split_sender_tag(text)
    assert sender == "Bob"
    assert body == "[Subject: Q3 numbers]\n\nthe actual email body"
    # …whereas the title builder deliberately collapses it to the subject.
    assert strip_directives(text)[0] == "Q3 numbers"


def test_split_sender_tag_peels_stacked_tags_in_any_order():
    text = '[think:high] [from @x (Y, id=1)] [in group "Team", chat id=9]: hi'
    assert split_sender_tag(text) == ("hi", "Y")


# ── inbound MCP / A2A turns ───────────────────────────────────────────────────


def test_external_threads_classify_by_id_prefix_with_the_protocol_as_channel():
    assert classify_thread("mcp::claude-desktop::default", None) == (ORIGIN_EXTERNAL, "mcp")
    assert classify_thread("a2a::zapier::ctx-1", None) == (ORIGIN_EXTERNAL, "a2a")
    # A stamped origin wins, and an empty stamped channel is filled from the id.
    assert classify_thread("mcp::c::t", {"origin": "external", "channel": ""}) == (
        ORIGIN_EXTERNAL,
        "mcp",
    )
    assert classify_thread("a2a::c::t", {"origin": "external", "channel": "a2a"}) == (
        ORIGIN_EXTERNAL,
        "a2a",
    )


def test_external_channels_are_stamped_from_the_request_context():
    assert channel_for_context("mcp", "mcp") == "mcp"
    assert channel_for_context("a2a", "a2a") == "a2a"
    assert channel_for_context("mcp", "") == "mcp"
    # A platform still wins over everything, as before.
    assert channel_for_context("scheduled", "telegram") == "telegram"


def test_external_threads_are_conversations_not_system_threads():
    assert not is_system_thread("mcp::claude-desktop::default")
    assert not is_system_thread("a2a::zapier::ctx-1")


def test_external_thread_label_shows_the_caller_without_the_protocol_prefix():
    entries = [
        {
            "ts": "2026-09-05T10:00:00+00:00",
            "role": "user",
            "content": "what is the order status?",
            "channel": "mcp",
            "sender": "mcp:Claude desktop",
        },
        {"ts": "2026-09-05T10:00:03+00:00", "role": "assistant", "content": "Shipped."},
    ]
    label = derive_label("mcp::claude-desktop::default", entries)
    assert label.origin == ORIGIN_EXTERNAL and label.channel == "mcp"
    assert label.sender == "Claude desktop"
    assert label.title == "what is the order status?"


def test_a_turn_from_the_mobile_app_is_labelled_mobile_and_nothing_else_changes():
    """The app marks its turns ``metadata["client"] = "mobile"``: the stamp
    reads MOBILE, while the context's own channel stays ``api``, which is what
    the human gate, magic links and browser auto-approval key on."""
    from olano.core.thread_labels import CHANNEL_MOBILE, channel_for_request
    from olano.security.request_context import RequestContext

    mobile = RequestContext(source="api", channel="api", metadata={"client": "mobile"})
    assert channel_for_request(mobile) == CHANNEL_MOBILE
    assert mobile.channel == "api"
    assert channel_for_request(RequestContext(source="api", channel="api")) == CHANNEL_DASHBOARD
    # Only a dashboard turn can become a mobile one: a marker on anything else
    # (a Telegram turn, a scheduled run) never relabels it.
    assert (
        channel_for_request(
            RequestContext(source="telegram", channel="telegram", metadata={"client": "mobile"})
        )
        == "telegram"
    )
    assert (
        channel_for_request(
            RequestContext(source="scheduled", channel="scheduler", metadata={"client": "mobile"})
        )
        == CHANNEL_SCHEDULER
    )
    # And the read side files it as an ordinary conversation on that channel.
    assert classify_thread("t-1", {"role": "user", "content": "hi", "channel": "mobile"}) == (
        ORIGIN_INTERACTIVE,
        CHANNEL_MOBILE,
    )
