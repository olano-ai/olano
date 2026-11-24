"""``document_to_text`` / ``pdf_to_text`` across the formats people attach.

Three behaviours, each with a real-converter test (skipped when the binary is
not installed: the sandbox image carries all three, a bare CI runner may not)
and a converter-free test of the decision around it:

* any text file reads as text, whatever its extension (the bytes decide);
* older and OpenDocument office files convert through LibreOffice;
* a PDF with no text layer is OCR'd (pdftoppm + tesseract), and when OCR cannot
  run the agent is told so and how else to read it, instead of "No text".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import olano.tools.media_tools as media_tools
from olano.tools.media_tools import document_to_text, pdf_to_text
from olano.uploads import file_category, looks_like_text

has_soffice = shutil.which("soffice") is not None
has_ocr = shutil.which("pdftoppm") is not None and shutil.which("tesseract") is not None


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    return tmp_path


# ─────────────────────────────── text sniff ─────────────────────────────────


@pytest.mark.parametrize(
    "data",
    [b"", b"SELECT 1;\n", "naïve café\n".encode(), "é".encode()[:1], b"\x1b[31mred\x1b[0m\n"],
)
def test_text_is_recognised_by_its_bytes(data: bytes) -> None:
    assert looks_like_text(data)


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04\x14\x00\x00",
        b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
        b"abc\x00def",
        bytes(range(1, 9)) * 50,
    ],
)
def test_binary_is_recognised_by_its_bytes(data: bytes) -> None:
    assert not looks_like_text(data)


def test_file_category_routes_each_kind_to_its_reader(tmp_path: Path) -> None:
    (tmp_path / "q.sql").write_text("select 1")
    (tmp_path / "Dockerfile").write_text("FROM alpine")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert file_category(tmp_path / "q.sql") == "text"
    assert file_category(tmp_path / "Dockerfile") == "text"
    assert file_category(tmp_path / "blob.bin") == "binary"
    # Extension decides these without reading (a PDF may open with ASCII).
    assert file_category(tmp_path / "missing.pdf") == "document"
    assert file_category(tmp_path / "old.XLS") == "document"
    assert file_category(tmp_path / "bundle.zip") == "archive"


# ──────────────────────────── any text file ─────────────────────────────────


@pytest.mark.parametrize(
    "name", ["schema.sql", "Main.kt", "Dockerfile", "notes.weird-ext", "app.ts"]
)
def test_any_text_file_reads_as_text(workspace: Path, name: str) -> None:
    (workspace / name).write_text("line one\nline two\n", encoding="utf-8")
    assert document_to_text.func(file_path=name) == "line one\nline two\n"


def test_binary_and_archive_get_a_pointer_not_an_error(workspace: Path) -> None:
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    (workspace / "bundle.zip").write_bytes(b"PK\x03\x04\x00\x00")
    assert "binary file" in document_to_text.func(file_path="blob.bin")
    assert "archive" in document_to_text.func(file_path="bundle.zip")


# ───────────────────────── legacy office formats ────────────────────────────


def _soffice_convert(src: Path, target: str) -> Path:
    subprocess.run(
        ["soffice", "--headless", "--convert-to", target, "--outdir", str(src.parent), str(src)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return src.with_suffix(f".{target}")


@pytest.mark.skipif(not has_soffice, reason="LibreOffice not installed")
def test_legacy_word_and_excel_convert_through_libreoffice(workspace: Path) -> None:
    import docx
    import openpyxl

    d = docx.Document()
    d.add_paragraph("Quarterly decision: hold the position.")
    d.save(workspace / "memo.docx")
    wb = openpyxl.Workbook()
    wb.active.append(["Ticker", "Qty"])
    wb.active.append(["ACME", 1200])
    wb.save(workspace / "book.xlsx")
    doc = _soffice_convert(workspace / "memo.docx", "doc")
    odt = _soffice_convert(workspace / "memo.docx", "odt")
    xls = _soffice_convert(workspace / "book.xlsx", "xls")

    for f in (doc, odt):
        assert "Quarterly decision: hold the position." in document_to_text.func(file_path=f.name)
    out = document_to_text.func(file_path=xls.name)
    assert "ACME" in out and "1200" in out
    # The scratch folder is cleaned up.
    assert not any((workspace / ".olano" / "convert").iterdir())


def test_conversion_failure_is_reported(workspace: Path, monkeypatch) -> None:
    (workspace / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0junk")
    monkeypatch.setattr(
        media_tools, "_run_converter", lambda argv, timeout: ("", "soffice is not installed here")
    )
    out = document_to_text.func(file_path="old.doc")
    assert out.startswith("[tool-error] document_to_text: could not convert old.doc")
    assert "not installed" in out


# ─────────────────────────────── scanned PDF ────────────────────────────────


def _scanned_pdf(path: Path, lines: list[str]) -> None:
    """A PDF that is only a picture of text, like a scan."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    for i, line in enumerate(lines):
        draw.text((100, 150 + i * 90), line, fill="black", font=font)
    img.save(path, "PDF", resolution=150)


@pytest.mark.skipif(not has_ocr, reason="pdftoppm/tesseract not installed")
def test_scanned_pdf_is_read_with_ocr(workspace: Path) -> None:
    _scanned_pdf(workspace / "scan.pdf", ["INVOICE NUMBER 4471", "TOTAL DUE 980 USD"])

    out = pdf_to_text.func(file_path="scan.pdf")

    assert "read with OCR" in out
    assert "--- Page 1 (OCR) ---" in out
    assert "4471" in out and "980" in out
    assert document_to_text.func(file_path="scan.pdf") == out


def test_scanned_pdf_without_ocr_says_how_to_read_it(workspace: Path, monkeypatch) -> None:
    _scanned_pdf(workspace / "scan.pdf", ["x"])
    monkeypatch.setattr(
        media_tools, "_run_converter", lambda argv, timeout: ("", "tesseract is not installed here")
    )

    out = pdf_to_text.func(file_path="scan.pdf")

    assert "no text layer" in out and "tesseract is not installed here" in out
    assert "image_describe" in out


def test_ocr_is_capped_and_says_so(workspace: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    pdf = workspace / "long.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    pages = [SimpleNamespace(extract_text=lambda: "")] * 30
    monkeypatch.setattr(
        "olano.tools._install_hint.optional_import",
        lambda *_a, **_k: (SimpleNamespace(PdfReader=lambda _p: SimpleNamespace(pages=pages)), ""),
    )
    seen = {}

    def fake_ocr(path, n):
        seen["pages"] = n
        return "--- Page 1 (OCR) ---\nhello", ""

    monkeypatch.setattr(media_tools, "_ocr_pdf", fake_ocr)
    out = pdf_to_text.func(file_path="long.pdf")
    assert seen["pages"] == media_tools._OCR_MAX_PAGES
    assert f"first {media_tools._OCR_MAX_PAGES} of 30 pages" in out
