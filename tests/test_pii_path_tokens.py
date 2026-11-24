"""A digit run inside a file name or path is not a US phone number.

The runtime names saved voice notes ``voice-<unix timestamp>.wav`` and appends
``[Audio attached: … saved to <path>]`` to the turn; the ten-digit timestamp
matched the ``phone_us`` pattern and the agent received a redacted name and a
saved-to path that did not exist.
"""

from __future__ import annotations

from olano.security.pii_middleware import redact, scan

VOICE_NOTE = (
    "[Voice message transcript] What's the price of Apple stock?\n\n"
    "[Audio attached: voice-1792547952.wav — saved to "
    "/home/olano/.olano/agents/aurelius/workspace/uploads/voice-1792547952.wav "
    "(audio/wav, 76.8 KB); already transcribed above, so the transcript is the "
    "user's message and the recording needs no tool.]"
)


def test_voice_note_file_name_and_path_survive_redaction() -> None:
    result = redact(VOICE_NOTE)
    assert result.cleaned == VOICE_NOTE
    assert not [f for f in result.findings if f["type"] == "phone_us"]


def test_other_upload_name_shapes_are_not_phones() -> None:
    for text in (
        "[Image attached: IMG_5551234567.jpg — saved to /ws/uploads/IMG_5551234567.jpg (image/jpeg, 2 MB)]",
        "see /ws/uploads/1792547952.png and 1792547952.wav",
        "report-555-123-4567.pdf",
    ):
        assert not [f for f in scan(text).findings if f["type"] == "phone_us"], text


def test_phone_numbers_in_prose_are_still_redacted() -> None:
    for text, expect in (
        ("call me at 555-123-4567 tomorrow", "call me at [PHONE_US_REDACTED] tomorrow"),
        ("my number is 5551234567.", "my number is [PHONE_US_REDACTED]."),
        ("tel:5551234567", "tel:[PHONE_US_REDACTED]"),
    ):
        assert redact(text).cleaned == expect, text
