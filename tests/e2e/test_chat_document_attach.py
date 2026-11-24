"""E2E: Office documents and PDFs attach in the dashboard chat composer.

The composer's classifier used to accept only images, audio, video and text,
and refused everything else with "<name>: unsupported file type.", so a
spreadsheet, a Word document or a slide deck could not be sent to an agent
from the dashboard at all, while the same file worked on Telegram. Every file
is now staged (``kind: "document"`` for the binary ones) and the backend saves
it into the agent's ``uploads/`` (``runtime/chat_attachments.py``).

Runs at both viewports: the report this pins came from a phone.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

COMPOSER = ".composer textarea"
FILE_INPUT = ".composer input[type='file']"
SEND_BUTTON = ".composer button[aria-label='Send']"

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_office_documents_stage_and_send(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "(label) => { const e = document.querySelector('.topbar .muted');"
        " return e && e.textContent.trim() === label; }",
        arg="merli",
        timeout=15_000,
    )

    # The bytes need not be a valid workbook: the client never parses them.
    page.locator(FILE_INPUT).set_input_files(
        [
            {"name": "PM Trade Blotter.xlsx", "mimeType": _XLSX_MIME, "buffer": b"PK\x03\x04x"},
            {"name": "Decision_Report.docx", "mimeType": _DOCX_MIME, "buffer": b"PK\x03\x04y"},
        ]
    )

    staged = page.locator(".att-row.staged .att-file-name")
    staged.nth(1).wait_for(state="visible", timeout=10_000)
    assert staged.all_text_contents() == ["PM Trade Blotter.xlsx", "Decision_Report.docx"]
    assert page.locator(".toast-host").get_by_text("unsupported").count() == 0

    composer.click()
    composer.fill("summarize these")
    send = page.locator(SEND_BUTTON)
    page.wait_for_function("el => el && !el.disabled", arg=send.element_handle(), timeout=10_000)
    send.click()

    # A sent message's attachments render in their own row beside the bubble.
    sent = page.locator(".att-row:not(.staged) .att-file-name")
    sent.nth(1).wait_for(state="visible", timeout=10_000)
    assert sent.all_text_contents() == ["PM Trade Blotter.xlsx", "Decision_Report.docx"]
    page.locator(".bubble.assistant").first.wait_for(state="visible", timeout=30_000)
