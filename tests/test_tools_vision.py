"""Tests for image description / vision tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _creds(available: dict[str, str]):
    def _get(name, default=""):
        return available.get(name, default)

    return _get


def _model(content: str | None = None, error: Exception | None = None) -> MagicMock:
    m = MagicMock()
    if error is not None:
        m.invoke.side_effect = error
    else:
        m.invoke.return_value = MagicMock(content=content)
    return m


def test_image_describe_url():
    built: list[str] = []

    def builder(model_str):
        built.append(model_str)
        return _model("A beautiful sunset over the ocean.")

    with (
        patch(
            "olano.tools.vision.get_credential", side_effect=_creds({"ANTHROPIC_API_KEY": "sk-a"})
        ),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke(
            {"image": "https://example.com/sunset.jpg", "question": "Describe this image"}
        )

    assert "sunset" in result.lower() or "ocean" in result.lower()
    assert built == ["anthropic:claude-haiku-4-5-20251001"]


def test_managed_vision_call_lands_in_the_service_ledger_priced():
    """A successful `olano:vision` call is a real credits burn that passes
    through no runner and no other ledger — it must land in the service ledger,
    priced from the gateway's cost header (vision is non-streamed, so unlike
    image/speech the header is actually present)."""
    from olano.analytics.service_ledger import read_service_events

    resp = MagicMock()
    resp.content = "A red pixel."
    resp.response_metadata = {"headers": {"x-litellm-response-cost": "0.0004"}}
    model = MagicMock()
    model.invoke.return_value = resp

    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({})),
        patch("olano.tools.vision._build_vision_model", return_value=model),
        patch("olano.tools.vision._olano_vision_available", return_value=True),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/p.png", "question": "?"})

    assert "red pixel" in result.lower()
    events = read_service_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.service == "vision"
    assert ev.billing_source == "olano" and ev.provider == "olano"
    assert ev.credits is not None and ev.credits > 0


def test_byok_vision_call_recorded_as_customer_unpriced():
    from olano.analytics.service_ledger import read_service_events

    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk"})),
        patch("olano.tools.vision._build_vision_model", return_value=_model("A dog.")),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        image_describe.invoke({"image": "https://example.com/d.png", "question": "?"})

    events = read_service_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.service == "vision" and ev.billing_source == "customer"
    assert ev.credits is None  # Olano sold nothing here


def test_image_describe_file_not_found():
    from olano.tools.vision import image_describe

    result = image_describe.invoke({"image": "/nonexistent/path/image.png"})
    assert "not found" in result.lower()


def test_image_describe_unsupported_extension(tmp_path):
    bad_file = tmp_path / "test.txt"
    bad_file.write_text("not an image")

    from olano.tools.vision import image_describe

    result = image_describe.invoke({"image": str(bad_file)})
    assert "unsupported" in result.lower()


def test_image_describe_local_file(tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(PNG_BYTES)

    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk-o"})),
        patch("olano.tools.vision._build_vision_model", return_value=_model("A solid red pixel.")),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": str(img), "question": "What color is this?"})

    assert "red" in result.lower() or "pixel" in result.lower()


def test_no_vision_provider_configured():
    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({})),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/img.png"})

    assert result.startswith("[missing-credentials]")
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        assert var in result


def test_auto_falls_back_when_default_has_no_credits():
    """The reported bug: Anthropic key present but out of credits, OpenAI key
    configured — the tool must use OpenAI instead of giving up."""
    calls: list[str] = []

    def builder(model_str):
        calls.append(model_str)
        if model_str.startswith("anthropic:"):
            return _model(error=Exception("Your credit balance is too low"))
        return _model("A plate with avocado toast and a fried egg.")

    with (
        patch(
            "olano.tools.vision.get_credential",
            side_effect=_creds({"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}),
        ),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/img.png"})

    assert calls == ["anthropic:claude-haiku-4-5-20251001", "openai:gpt-5.4-mini"]
    assert "avocado toast" in result
    assert "[vision-fallback]" in result
    assert "credits" in result.lower()


def test_fallback_note_names_every_failed_rung_before_success():
    """When two rungs fall through before one answers, the note must name BOTH
    failures — hiding the middle one makes a recurring outage invisible."""
    calls: list[str] = []

    def builder(model_str):
        calls.append(model_str)
        if model_str.startswith("olano:") or model_str.startswith("anthropic:"):
            return _model(error=Exception("Your credit balance is too low"))
        return _model("A red bicycle.")

    with (
        patch(
            "olano.tools.vision.get_credential",
            side_effect=_creds({"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}),
        ),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=True),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/img.png"})

    assert "A red bicycle." in result
    assert "[vision-fallback]" in result
    # BOTH failed rungs named, not just the first.
    assert "olano:vision" in result
    assert "anthropic:" in result


def test_auto_reports_every_attempt_when_all_fail():
    def builder(model_str):
        return _model(error=Exception("Your credit balance is too low"))

    with (
        patch(
            "olano.tools.vision.get_credential",
            side_effect=_creds({"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}),
        ),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/img.png"})

    assert result.startswith("[tool-error]")
    assert "anthropic:claude-haiku-4-5-20251001" in result
    assert "openai:gpt-5.4-mini" in result
    # Nothing left to suggest, and the agent is told not to try image_generate.
    assert "image_generate" in result


def test_model_error_is_reported():
    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk-o"})),
        patch(
            "olano.tools.vision._build_vision_model",
            return_value=_model(error=Exception("API error")),
        ),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/img.png"})

    assert "tool-error" in result or "Error" in result


def test_olano_leads_the_chain_on_managed_deployments():
    calls: list[str] = []

    def builder(model_str):
        calls.append(model_str)
        return _model("Described on the managed service.")

    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk-o"})),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=True),
    ):
        from olano.tools.vision import image_describe

        image_describe.invoke({"image": "https://example.com/img.png"})

    assert calls == ["olano:vision"]


def test_operator_pin_wins_and_does_not_fall_back():
    """LIONCLAW_VISION_MODEL is a standing choice, so a broken pin must report
    itself rather than quietly moving billing somewhere else."""
    calls: list[str] = []

    def builder(model_str):
        calls.append(model_str)
        return _model(error=Exception("Your credit balance is too low"))

    with (
        patch(
            "olano.tools.vision.get_credential",
            side_effect=_creds(
                {
                    "LIONCLAW_VISION_MODEL": "google_genai:gemini-2.5-flash",
                    "GOOGLE_API_KEY": "g-x",
                    "OPENAI_API_KEY": "sk-o",
                }
            ),
        ),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=True),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/img.png"})

    assert calls == ["google_genai:gemini-2.5-flash"]
    assert result.startswith("[tool-error]")
    assert "openai:gpt-5.4-mini" in result  # ...but the retry path is named


def test_explicit_model_is_not_silently_substituted():
    calls: list[str] = []

    def builder(model_str):
        calls.append(model_str)
        return _model(error=Exception("Your credit balance is too low"))

    with (
        patch(
            "olano.tools.vision.get_credential",
            side_effect=_creds({"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}),
        ),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke(
            {"image": "https://example.com/img.png", "model": "anthropic:claude-haiku-4-5-20251001"}
        )

    assert calls == ["anthropic:claude-haiku-4-5-20251001"]
    assert result.startswith("[tool-error]")
    # ...but the agent is told what it CAN retry with.
    assert "openai:gpt-5.4-mini" in result


def test_explicit_model_without_key_fails_before_any_call():
    def builder(model_str):  # pragma: no cover - must not be reached
        raise AssertionError("should not build a model without credentials")

    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk-o"})),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke(
            {"image": "https://example.com/img.png", "model": "anthropic:claude-haiku-4-5"}
        )

    assert result.startswith("[missing-credentials]")
    assert "ANTHROPIC_API_KEY" in result
    assert "openai:gpt-5.4-mini" in result


def test_explicit_olano_model_without_deployment():
    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk-o"})),
        patch("olano.tools.vision._olano_vision_available", return_value=False),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke(
            {"image": "https://example.com/img.png", "model": "olano:vision"}
        )

    assert result.startswith("[missing-credentials]")
    assert "cloud.olano.ai" in result


def test_legacy_describe_image_name_still_resolves():
    """The toolkit ships ONE image→text tool now. `describe_image` was the old
    spelling of the same thing, so a config written against it must still land
    on `image_describe` rather than silently resolving to nothing."""
    from olano.tools.registry import resolve_tools

    assert [t.name for t in resolve_tools(["describe_image"])] == ["image_describe"]
    assert [t.name for t in resolve_tools(["image_describe"])] == ["image_describe"]


def test_only_one_image_to_text_tool_is_exposed():
    from olano.tools import vision

    assert [t.name for t in vision.get_tools()] == ["image_describe"]


def test_tool_exposes_the_model_argument():
    from olano.tools.vision import image_describe

    assert "model" in image_describe.args


# ── The failure memo: stop recommending a model that just hard-failed ─────────
# The bug this covers, verbatim from a managed box: image_describe led with
# olano:vision, the gateway served no such group, and the resulting error told
# the agent "Vision models that ARE available here: olano:vision — retry with
# one of those". The agent bounced between the same two dead ends and gave up.

_UNROUTED = (
    "Error code: 400 - {'error': {'message': '/chat/completions: Invalid model name passed in "
    "model=olano-vision. Call `/v1/models` to view available models for your key.', "
    "'code': '400'}}"
)


def _describe_with(builder, creds: dict[str, str], *, olano: bool = True) -> str:
    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds(creds)),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
        patch("olano.tools.vision._olano_vision_available", return_value=olano),
    ):
        from olano.tools.vision import image_describe

        return image_describe.invoke({"image": "https://example.com/img.png"})


def test_unrouted_olano_vision_is_not_offered_as_the_retry():
    """The exact loop: the only configured model fails as unrouted, so the
    output must not turn around and recommend it."""
    result = _describe_with(lambda m: _model(error=Exception(_UNROUTED)), {})

    assert result.startswith("[tool-error]")
    # It is still NAMED (an outage must stay visible)…
    assert "olano:vision" in result
    # …but never as something to retry with.
    assert 'Retry image_describe with model="<one of those>"' not in result
    # And it says whose problem this is, instead of demanding provider keys the
    # customer deliberately does not have.
    assert "Olano-side routing problem" in result
    assert "olano models gateway --probe" in result


def test_second_call_annotates_the_remembered_failure():
    """A model that hard-failed is annotated as failing wherever it is listed,
    so a later pinned call cannot be pointed back at it."""
    _describe_with(lambda m: _model(error=Exception(_UNROUTED)), {})

    # Now pin a model with no key: the alternatives line must mark olano:vision
    # as failing rather than presenting it as a working option.
    with (
        patch("olano.tools.vision.get_credential", side_effect=_creds({})),
        patch("olano.tools.vision._olano_vision_available", return_value=True),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke(
            {"image": "https://example.com/img.png", "model": "openai:gpt-5.4-mini"}
        )

    assert result.startswith("[missing-credentials]")
    assert "failing" in result
    assert 'Retry image_describe with model="<one of those>"' not in result


def test_a_working_model_is_still_recommended_and_clears_its_memo():
    """The memo must not suppress a healthy alternative — nor outlive a success."""
    from olano.tools import vision

    vision._note_failure("anthropic:claude-haiku-4-5-20251001", "boom")
    assert vision._recent_failure("anthropic:claude-haiku-4-5-20251001") == "boom"

    # A later success on that model forgets the failure.
    _describe_with(
        lambda m: _model("a lion"),
        {"ANTHROPIC_API_KEY": "sk-a"},
        olano=False,
    )
    assert vision._recent_failure("anthropic:claude-haiku-4-5-20251001") is None


def test_transient_failures_are_never_remembered():
    """A rate limit says nothing about the next call. Marking it "failing"
    would turn a blip into a lie that outlives it."""
    from olano.tools import vision

    _describe_with(
        lambda m: _model(error=Exception("Error code: 429 - rate_limit exceeded")),
        {},
    )
    assert vision._recent_failure("olano:vision") is None


def test_memo_expires(monkeypatch):
    from olano.tools import vision

    monkeypatch.setenv("LIONCLAW_VISION_FAILURE_TTL", "0.01")
    vision._note_failure("olano:vision", "down")
    assert vision._recent_failure("olano:vision") == "down"
    import time as _time

    _time.sleep(0.02)
    assert vision._recent_failure("olano:vision") is None


def test_memo_can_be_disabled(monkeypatch):
    from olano.tools import vision

    monkeypatch.setenv("LIONCLAW_VISION_FAILURE_TTL", "0")
    vision._note_failure("olano:vision", "down")
    assert vision._recent_failure("olano:vision") is None


# ── The managed rung consults the gateway probe ──────────────────────────────
# Credentials and entitlement both said yes throughout the outage where the
# gateway served no `olano-vision` group at all, so they cannot be the whole
# answer. A recent reading that actually looked at the gateway's model list is.


def _probe_saying_vision_is_missing(*, ok: bool = True, age: float = 0.0):
    """A cached probe reading, as gateway_probe would have stored it."""
    import time as _time

    from olano.deployment.gateway_probe import GatewayProbe

    return GatewayProbe(
        ok=ok,
        served=("olano-turbo",),
        expected=("olano-turbo", "olano-vision"),
        missing=("olano-vision",) if ok else (),
        error=None if ok else "connection refused",
        checked_at=_time.time() - age,
    )


def _entitled():
    """Credential + entitlement both green — the state during the outage."""
    return (
        patch("olano.tools.image_tools.gateway_media_reachable", return_value=True),
        patch("olano.deployment.enforcement.service_entitled", return_value=True),
    )


def test_managed_rung_is_skipped_when_a_recent_probe_says_the_group_is_unrouted():
    from olano.tools.vision import _olano_vision_available

    reach, entitled = _entitled()
    with (
        reach,
        entitled,
        patch(
            "olano.deployment.gateway_probe.cached",
            return_value=_probe_saying_vision_is_missing(),
        ),
    ):
        # Would have been True on every signal this box can see locally.
        assert _olano_vision_available() is False


def test_managed_rung_survives_a_stale_reading_so_a_published_group_heals():
    from olano.tools.vision import _olano_vision_available

    reach, entitled = _entitled()
    with (
        reach,
        entitled,
        patch(
            "olano.deployment.gateway_probe.cached",
            return_value=_probe_saying_vision_is_missing(age=36_000),
        ),
    ):
        assert _olano_vision_available() is True


def test_managed_rung_survives_a_failed_probe():
    # An unreachable gateway proves nothing about routing; taking vision away
    # for a network blip would be a worse bug than the one this prevents.
    from olano.tools.vision import _olano_vision_available

    reach, entitled = _entitled()
    with (
        reach,
        entitled,
        patch(
            "olano.deployment.gateway_probe.cached",
            return_value=_probe_saying_vision_is_missing(ok=False),
        ),
    ):
        assert _olano_vision_available() is True


def test_unrouted_group_falls_straight_to_byok_without_calling_the_gateway():
    """The end-to-end point: no wasted call, and the answer still arrives."""
    calls: list[str] = []

    def builder(model_str):
        calls.append(model_str)
        return _model("A red bicycle.")

    reach, entitled = _entitled()
    with (
        reach,
        entitled,
        patch(
            "olano.deployment.gateway_probe.cached",
            return_value=_probe_saying_vision_is_missing(),
        ),
        patch(
            "olano.tools.vision.get_credential", side_effect=_creds({"ANTHROPIC_API_KEY": "sk-a"})
        ),
        patch("olano.tools.vision._build_vision_model", side_effect=builder),
    ):
        from olano.tools.vision import image_describe

        result = image_describe.invoke({"image": "https://example.com/img.png"})

    assert "A red bicycle." in result
    assert not any(m.startswith("olano:") for m in calls), calls


def test_vision_call_is_isolated_from_the_callers_live_stream():
    """A transcription is never the agent's answer, so it must not stream.

    ``image_describe`` runs inside the agent's LangGraph turn, so without
    isolation ``ensure_config()`` merges the ambient run config — carrying
    LangGraph's ``StreamMessagesHandler`` — into this model call and its tokens
    are appended to the turn's reply. Two concurrent calls (one Telegram photo
    burst → two ``image_describe`` calls) then interleaved two transcriptions
    chunk-by-chunk into a single unreadable message. Both belts are asserted
    here: the ambient config is gone for the duration, and the call is tagged
    ``nostream`` for LangGraph's own suppression.
    """
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.runnables.config import var_child_runnable_config

    from olano.core.stream_isolation import NOSTREAM_TAG

    class _ParentStreamHandler(BaseCallbackHandler):
        """Stands in for LangGraph's StreamMessagesHandler."""

    seen: dict[str, object] = {}
    parent_stream = {"callbacks": [_ParentStreamHandler()]}

    def _invoke(_messages, config=None, **_kwargs):
        seen["ambient"] = var_child_runnable_config.get()
        seen["config"] = config
        return MagicMock(content="A schedule table.")

    model = MagicMock()
    model.invoke.side_effect = _invoke

    token = var_child_runnable_config.set(parent_stream)
    try:
        with (
            patch(
                "olano.tools.vision.get_credential",
                side_effect=_creds({"ANTHROPIC_API_KEY": "sk-a"}),
            ),
            patch("olano.tools.vision._build_vision_model", return_value=model),
            patch("olano.tools.vision._olano_vision_available", return_value=False),
        ):
            from olano.tools.vision import image_describe

            result = image_describe.invoke({"image": "https://example.com/schedule.png"})
    finally:
        var_child_runnable_config.reset(token)

    assert "schedule" in result.lower()
    assert seen["ambient"] == {}, "the caller's streaming callbacks leaked into the vision call"
    assert NOSTREAM_TAG in (seen["config"] or {}).get("tags", [])
