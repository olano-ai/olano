"""Tests for image generation and editing tools."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch


def _client_with_get(get):
    """``httpx.Client`` stand-in: ``_load_image_bytes`` fetches URLs through a
    client (so the SSRF redirect guard can hook every hop), not ``httpx.get``."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = get
    return MagicMock(return_value=client)


FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fakeimagedata"
FAKE_PNG_B64 = base64.b64encode(FAKE_PNG).decode("ascii")


def _openai_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _gemini_image_response(n_images: int = 1, texts: list[str] | None = None) -> MagicMock:
    parts: list[dict] = [
        {"inlineData": {"mimeType": "image/png", "data": FAKE_PNG_B64}} for _ in range(n_images)
    ]
    parts += [{"text": t} for t in (texts or [])]
    return _openai_response({"candidates": [{"content": {"parts": parts}}]})


def _creds(available: dict[str, str]):
    def _get(name, default=""):
        return available.get(name, default)

    return _get


def _patched(tmp_path, creds: dict[str, str], post=None, get=None):
    patches = [
        patch("olano.tools.image_tools.get_credential", side_effect=_creds(creds)),
        patch("olano.tools.image_tools.get_workspace"),
    ]
    if post is not None:
        patches.append(patch("olano.tools.image_tools.httpx.post", post))
    if get is not None:
        patches.append(patch("olano.tools.image_tools.httpx.Client", _client_with_get(get)))
    return patches


def test_image_generate_openai_explicit(tmp_path):
    mock_post = MagicMock(return_value=_openai_response({"data": [{"b64_json": FAKE_PNG_B64}]}))
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke(
            {"prompt": "a red cube", "model": "openai:gpt-image-2.5-flare"}
        )

    url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert url == "https://api.openai.com/v1/images/generations"
    assert kwargs["json"]["model"] == "gpt-image-2.5-flare"
    assert kwargs["json"]["quality"] == "medium"
    saved = list((tmp_path / "images").glob("*.png"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == FAKE_PNG
    assert str(saved[0]) in result
    assert "![" in result


def test_image_generate_default_gemini(tmp_path):
    mock_post = MagicMock(return_value=_gemini_image_response())
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"GOOGLE_API_KEY": "g-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a lion"})

    url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert "models/gemini-3.1-flash-image:generateContent" in url
    assert kwargs["headers"]["x-goog-api-key"] == "g-x"
    assert kwargs["json"]["generationConfig"]["responseModalities"] == ["IMAGE"]
    saved = list((tmp_path / "images").glob("*.png"))
    assert len(saved) == 1
    assert "gemini:gemini-3.1-flash-image" in result


def test_image_generate_fallback_to_openai(tmp_path):
    mock_post = MagicMock(return_value=_openai_response({"data": [{"b64_json": FAKE_PNG_B64}]}))
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a lion"})

    assert mock_post.call_args[0][0] == "https://api.openai.com/v1/images/generations"
    assert mock_post.call_args[1]["json"]["model"] == "gpt-image-2.5-flare"
    assert "[tool-error]" not in result


def test_image_generate_no_keys():
    with patch("olano.tools.image_tools.get_credential", side_effect=_creds({})):
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a lion"})

    assert result.startswith("[missing-credentials]")
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        assert var in result


def test_image_generate_n_clamped(tmp_path):
    mock_post = MagicMock(return_value=_openai_response({"data": [{"b64_json": FAKE_PNG_B64}]}))
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        image_generate.invoke({"prompt": "cubes", "model": "openai:gpt-image-2.5-flare", "n": 9})

    assert mock_post.call_args[1]["json"]["n"] == 4


def test_image_generate_unknown_provider():
    with patch(
        "olano.tools.image_tools.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk-x"})
    ):
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "x", "model": "foo:bar"})

    assert result.startswith("[tool-error] Unknown image provider")


def test_image_generate_gemini_no_image_parts(tmp_path):
    mock_post = MagicMock(
        return_value=_openai_response(
            {"candidates": [{"content": {"parts": [{"text": "cannot comply"}]}}]}
        )
    )
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"GEMINI_API_KEY": "g-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a lion"})

    assert result.startswith("[tool-error]")
    assert "cannot comply" in result


def test_image_edit_local_file_no_mask(tmp_path):
    src = tmp_path / "input.png"
    src.write_bytes(FAKE_PNG)
    mock_post = MagicMock(return_value=_openai_response({"data": [{"b64_json": FAKE_PNG_B64}]}))
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_edit

        result = image_edit.invoke({"image": str(src), "prompt": "add a hat"})

    files = mock_post.call_args[1]["files"]
    assert mock_post.call_args[0][0] == "https://api.openai.com/v1/images/edits"
    assert files["model"] == (None, "gpt-image-2.5-flare")
    assert "mask" not in files
    assert files["image"][1] == FAKE_PNG
    saved = list((tmp_path / "images").glob("*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in result


def test_image_edit_with_mask_url(tmp_path):
    src = tmp_path / "input.png"
    src.write_bytes(FAKE_PNG)
    mask_resp = MagicMock()
    mask_resp.status_code = 200
    mask_resp.raise_for_status = MagicMock()
    mask_resp.content = FAKE_PNG
    mask_resp.headers = {"content-type": "image/png"}
    mock_get = MagicMock(return_value=mask_resp)
    mock_post = MagicMock(return_value=_openai_response({"data": [{"b64_json": FAKE_PNG_B64}]}))
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
        patch("olano.tools.image_tools.httpx.Client", _client_with_get(mock_get)),
        patch("olano.tools.image_tools.assert_safe_outbound_url"),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_edit

        image_edit.invoke(
            {
                "image": str(src),
                "prompt": "add a hat",
                "mask": "https://example.com/mask.png",
            }
        )

    mock_get.assert_called_once()
    files = mock_post.call_args[1]["files"]
    assert "mask" in files
    assert files["mask"][1] == FAKE_PNG


def test_nanobanana_create_image(tmp_path):
    mock_post = MagicMock(return_value=_gemini_image_response())
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"GOOGLE_API_KEY": "g-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import nanobanana_create_image

        result = nanobanana_create_image.invoke({"prompt": "a lion on the beach"})

    url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert "models/gemini-3.1-flash-image:generateContent" in url
    assert kwargs["headers"]["x-goog-api-key"] == "g-x"
    assert kwargs["json"]["contents"][0]["parts"] == [{"text": "a lion on the beach"}]
    saved = list((tmp_path / "images").glob("*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in result
    assert "Nano Banana" in result


def test_nanobanana_create_image_no_key():
    with patch("olano.tools.image_tools.get_credential", side_effect=_creds({})):
        from olano.tools.image_tools import nanobanana_create_image

        result = nanobanana_create_image.invoke({"prompt": "a lion"})

    assert result.startswith("[missing-credentials]")
    assert "GOOGLE_API_KEY" in result
    assert "GEMINI_API_KEY" in result
    assert "image_generate" in result


def test_nanobanana_image_edit_local_and_url_inputs(tmp_path):
    src = tmp_path / "photo.png"
    src.write_bytes(FAKE_PNG)
    url_resp = MagicMock()
    url_resp.status_code = 200
    url_resp.raise_for_status = MagicMock()
    url_resp.content = FAKE_PNG
    url_resp.headers = {"content-type": "image/jpeg"}
    mock_get = MagicMock(return_value=url_resp)
    mock_post = MagicMock(return_value=_gemini_image_response())
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"GOOGLE_API_KEY": "g-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
        patch("olano.tools.image_tools.httpx.Client", _client_with_get(mock_get)),
        patch("olano.tools.image_tools.assert_safe_outbound_url"),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import nanobanana_image_edit

        result = nanobanana_image_edit.invoke(
            {
                "instruction": "combine into one scene",
                "images": f"{src}, https://example.com/ref.jpg",
            }
        )

    body = mock_post.call_args[1]["json"]
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "combine into one scene"}
    inline = [p for p in parts if "inlineData" in p]
    assert len(inline) == 2
    assert inline[1]["inlineData"]["mimeType"] == "image/jpeg"
    saved = list((tmp_path / "images").glob("*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in result
    assert "Nano Banana" in result


def test_nanobanana_image_edit_no_google_key():
    with patch(
        "olano.tools.image_tools.get_credential", side_effect=_creds({"OPENAI_API_KEY": "sk-x"})
    ):
        from olano.tools.image_tools import nanobanana_image_edit

        result = nanobanana_image_edit.invoke({"instruction": "edit", "images": "/tmp/x.png"})

    assert result.startswith("[missing-credentials]")
    assert "GEMINI_API_KEY" in result
    assert "image_edit" in result


def test_result_paths_not_in_backticks(tmp_path):
    """Local paths must be bare (extract_local_files ignores code spans)."""
    mock_post = MagicMock(return_value=_gemini_image_response())
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"GOOGLE_API_KEY": "g-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a lion"})

    assert "`" not in result


def test_olano_leads_auto_selection_when_billable(tmp_path):
    """A managed deployment generates with olano credits by default, even with
    a BYOK key present — the included service is the product default."""
    mock_post = MagicMock(return_value=_openai_response({"data": [{"b64_json": FAKE_PNG_B64}]}))
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"GOOGLE_API_KEY": "g-x", "OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools._olano_image_available", return_value=True),
        patch(
            "olano.deployment.gateway_key.ensure_gateway_key",
            return_value="sk-gw",
            create=True,
        ),
        patch(
            "olano.deployment.gateway_key.openai_base_url",
            return_value="https://gateway.example/v1",
            create=True,
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a lion"})

    assert mock_post.call_args[0][0] == "https://gateway.example/v1/images/generations"
    assert mock_post.call_args[1]["json"]["model"] == "olano-image"
    assert "olano:olano-image" in result


def test_auto_falls_through_to_next_backend(tmp_path):
    """Gemini out of quota + an OpenAI key configured → generate with OpenAI."""
    import httpx

    def post(url, **kwargs):
        if "generativelanguage" in url:
            resp = MagicMock()
            resp.status_code = 429
            resp.text = "quota exceeded"
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "429", request=MagicMock(), response=resp
            )
            return resp
        return _openai_response({"data": [{"b64_json": FAKE_PNG_B64}]})

    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"GOOGLE_API_KEY": "g-x", "OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools._olano_image_available", return_value=False),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", side_effect=post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a lion"})

    assert "openai:gpt-image-2.5-flare" in result
    assert "[image-fallback]" in result
    # The rung that fell through is still named, with its reason — but the
    # reason is now the CLASSIFIED one rather than the raw 429 body ("quota
    # exceeded"), so a provider refusal reads the same here as it does in chat.
    assert "gemini:gemini-3.1-flash-image" in result
    assert "quota limit was reached" in result


def test_explicit_model_without_key_lists_alternatives():
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools._olano_image_available", return_value=False),
    ):
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke(
            {"prompt": "a lion", "model": "gemini:gemini-3.1-flash-image"}
        )

    assert result.startswith("[missing-credentials]")
    assert "openai:gpt-image-2.5-flare" in result


def test_generators_say_they_cannot_read_images():
    """An agent whose vision tool failed must not read image_generate as a
    fallback — the tool description has to close that door."""
    from olano.tools.image_tools import image_generate, nanobanana_create_image

    for t in (image_generate, nanobanana_create_image):
        assert "image_describe" in t.description
        assert "cannot" in t.description.lower()


def test_tools_registered():
    from olano.tools.image_tools import get_tools

    names = {t.name for t in get_tools(None)}
    assert {
        "image_generate",
        "image_edit",
        "nanobanana_create_image",
        "nanobanana_image_edit",
        "screenshot_url",
    } <= names


def test_image_generate_records_service_calls_per_image(tmp_path):
    """Generated images land in the service ledger, one unit per image.

    Image generation spends money (olano credits on a managed box, the
    operator's own key otherwise) without producing a token, so the LLM usage
    ledger never sees it. The gateway meters the charge cloud-side and returns
    no per-call figure, so the row is counted and left unpriced.
    """
    from olano.analytics import service_ledger

    mock_post = MagicMock(
        return_value=_openai_response(
            {"data": [{"b64_json": FAKE_PNG_B64}, {"b64_json": FAKE_PNG_B64}]}
        )
    )
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        image_generate.invoke(
            {"prompt": "two cubes", "model": "openai:gpt-image-2.5-flare", "n": 2}
        )

    events = service_ledger.read_service_events()
    assert len(events) == 1
    assert events[0].service == "image"
    assert events[0].provider == "openai"
    assert events[0].billing_source == "customer"
    assert events[0].units == 2
    assert events[0].credits is None


def test_image_generate_olano_records_the_gateway_charge(tmp_path):
    """Managed image calls are plain HTTP, so the gateway's per-call cost
    header prices the service-ledger row instead of 'counted, never priced'."""
    resp = _openai_response({"data": [{"b64_json": FAKE_PNG_B64}]})
    resp.headers = {"x-olano-credits-charged": "3.2"}
    mock_post = MagicMock(return_value=resp)
    with (
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
        patch("olano.tools.image_tools._olano_image_available", return_value=True),
        patch("olano.deployment.gateway_key.ensure_gateway_key", return_value="gk-x"),
        patch(
            "olano.deployment.gateway_key.openai_base_url",
            return_value="https://gw.test/v1",
        ),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        result = image_generate.invoke({"prompt": "a cube", "model": "olano:olano-image"})

    assert "Generated 1 image(s)" in result
    from olano.analytics.service_ledger import read_service_events

    (event,) = [e for e in read_service_events() if e.service == "image"]
    assert event.credits == 3.2 and event.billing_source == "olano"


def test_image_generate_byok_never_fabricates_a_charge(tmp_path):
    """An OpenAI-key call carrying gateway-style cost telemetry stays
    unpriced — the customer paid their own provider, not Olano."""
    resp = _openai_response({"data": [{"b64_json": FAKE_PNG_B64}]})
    resp.headers = {"x-litellm-response-cost": "0.01"}
    mock_post = MagicMock(return_value=resp)
    with (
        patch(
            "olano.tools.image_tools.get_credential",
            side_effect=_creds({"OPENAI_API_KEY": "sk-x"}),
        ),
        patch("olano.tools.image_tools.get_workspace") as mock_ws,
        patch("olano.tools.image_tools.httpx.post", mock_post),
    ):
        mock_ws.return_value.root = tmp_path
        from olano.tools.image_tools import image_generate

        image_generate.invoke({"prompt": "a cube", "model": "openai:gpt-image-2.5-flare"})

    from olano.analytics.service_ledger import read_service_events

    (event,) = [e for e in read_service_events() if e.service == "image"]
    assert event.credits is None and event.billing_source != "olano"
