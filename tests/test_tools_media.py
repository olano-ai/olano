"""Tests for document and PDF media tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from olano.config.agent_config import AgentConfig
from olano.tools.media_tools import document_to_text, pdf_to_text


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path))
    return tmp_path


def test_document_to_text_reads_workspace_relative_file(workspace: Path) -> None:
    doc = workspace / "downloads" / "note.txt"
    doc.parent.mkdir()
    doc.write_text("hello from a downloaded document", encoding="utf-8")

    result = document_to_text.func(file_path="downloads/note.txt")

    assert result == "hello from a downloaded document"


def test_document_to_text_blocks_paths_outside_workspace(workspace: Path, tmp_path: Path) -> None:
    outside = workspace.parent / "outside-media.txt"
    outside.write_text("secret", encoding="utf-8")

    result = document_to_text.func(file_path=str(outside))

    assert "outside the workspace" in result


def test_pdf_to_text_extracts_text_with_pypdf(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = workspace / "downloads" / "report.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.4 fake test payload")

    fake_pages = [
        SimpleNamespace(extract_text=lambda: "Executive summary"),
        SimpleNamespace(extract_text=lambda: "Investment performance"),
    ]
    fake_pypdf = SimpleNamespace(PdfReader=lambda _path: SimpleNamespace(pages=fake_pages))
    monkeypatch.setattr(
        "olano.tools._install_hint.optional_import",
        lambda *_args, **_kwargs: (fake_pypdf, ""),
    )

    result = pdf_to_text.func(file_path=str(pdf))

    assert "--- Page 1 ---" in result
    assert "Executive summary" in result
    assert "Investment performance" in result


def test_document_to_text_extracts_docx_paragraphs_and_tables(workspace: Path) -> None:
    from docx import Document

    path = workspace / "brief.docx"
    doc = Document()
    doc.add_paragraph("Board update")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Metric"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "IRR"
    table.cell(1, 1).text = "7%"
    doc.save(path)

    result = document_to_text.func(file_path="brief.docx")

    assert "Board update" in result
    assert "| Metric | Value |" in result
    assert "| IRR | 7% |" in result


def test_document_to_text_extracts_pptx_slide_text(workspace: Path) -> None:
    from pptx import Presentation

    path = workspace / "deck.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Quarterly review"
    textbox = slide.shapes.add_textbox(100000, 100000, 4000000, 1000000)
    textbox.text = "Revenue grew across regions"
    presentation.save(path)

    result = document_to_text.func(file_path="deck.pptx")

    assert "# Slide 1" in result
    assert "Quarterly review" in result
    assert "Revenue grew across regions" in result


def test_document_to_text_extracts_xlsx_preview(workspace: Path) -> None:
    from openpyxl import Workbook

    path = workspace / "model.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Summary"
    sheet.append(["Metric", "Value"])
    sheet.append(["AUM", "100"])
    workbook.save(path)

    result = document_to_text.func(file_path="model.xlsx")

    assert "# Workbook: model.xlsx" in result
    assert "## Sheet: Summary" in result
    assert "| Metric | Value |" in result
    assert "| AUM | 100 |" in result


def test_pdf_reader_tools_are_default_agent_capabilities() -> None:
    cfg = AgentConfig(name="aurelius", model="anthropic:claude-haiku-4-5-20251001")

    assert "document_to_text" in cfg.tools
    assert "pdf_to_text" in cfg.tools
