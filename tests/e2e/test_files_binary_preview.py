"""E2E: a file the browser cannot draw is offered for download, never printed.

Every name the Files preview did not recognise fell through to "text": the
runtime decoded it with ``errors="replace"`` and the pane printed a screen of
mojibake (a skills export ``.zip`` was the report), with Copy and Edit on
offer. A known binary format now never reads its bytes at all, and an unknown
name whose head is not text is caught by the server's sniff; both show a card
with the type, the size and a Download button.
"""

from __future__ import annotations

import io
import zipfile

import pytest

pytestmark = pytest.mark.e2e

DIR = "e2e-binary-preview"
BODY = ".files-preview-body"
CARD = "[data-preview-binary]"


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("skills/contextual-investing-review/SKILL.md", "# review\n" * 400)
    return buf.getvalue()


def _upload(page, base: str, name: str, data: bytes) -> None:
    resp = page.request.post(
        f"{base}/api/files/upload?agent=merli&dir={DIR}&name={name}&overwrite=true",
        data=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.ok, f"could not seed {name}: {resp.status} {resp.text()}"


def _open(page, base: str, name: str) -> None:
    page.goto(f"{base}/files?path={DIR}/{name}&agent=merli", wait_until="domcontentloaded")
    page.locator(".files-pane-title strong").first.wait_for(state="visible", timeout=15_000)


@pytest.mark.parametrize(
    "name, data",
    [
        ("skills-export.zip", _zip_bytes()),
        # No extension to go on: the server's sniff decides.
        ("blob.dat", b"\x00\x01\x02\xff\xfe" * 400),
    ],
)
def test_binary_file_shows_a_download_card(page, dashboard, name: str, data: bytes) -> None:
    base = dashboard.base_url.rstrip("/")
    _upload(page, base, name, data)
    _open(page, base, name)

    card = page.locator(f"{BODY} {CARD}").first
    card.wait_for(state="visible", timeout=15_000)
    assert "No preview for this file" in card.inner_text()
    assert page.locator(f"{BODY} pre").count() == 0, "the bytes were printed as text"
    download = card.get_by_role("link", name="Download")
    assert download.is_visible()
    assert download.get_attribute("download") == name
    # Neither Copy nor Edit is offered for bytes that are not text.
    assert page.get_by_role("button", name="Copy", exact=True).count() == 0
    assert page.get_by_role("button", name="Edit", exact=True).count() == 0
    box = download.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"] + 1
