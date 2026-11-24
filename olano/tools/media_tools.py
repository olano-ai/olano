"""MediaAgent tools — audio transcription, text-to-speech, document conversion.

Speech (``audio_to_text`` / ``text_to_speech``) routes through the
provider-independent layer in :mod:`olano.speech` — the provider (OpenAI /
Groq / custom OpenAI-compatible endpoint) is selected in ``config.yaml →
speech:`` (dashboard **Config → Speech**) and is **disabled by default**.
Credentials resolve per-agent (config.env overlay → vault → environment).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.security.request_context import get_request_context
from olano.security.ssrf import SSRFBlockedError, assert_safe_outbound_url
from olano.security.tool_metadata import tool_metadata
from olano.tools._sandbox_exec import handoff_to_sandbox

logger = logging.getLogger(__name__)

_DEFAULT_TEXT_LIMIT = 80_000
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".log",
}


def _workspace_root() -> Path:
    workspace_root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(workspace_root) if workspace_root else None).root


def _resolve_local_path(file_path: str) -> Path:
    """Resolve a local document path while keeping default tools workspace-scoped."""
    raw = file_path.removeprefix("file://")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _workspace_root() / path
    resolved = path.resolve(strict=False)
    root = _workspace_root().resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"Path is outside the workspace: {file_path}. Use a path under {root}.")
    return resolved


def _assert_safe_url(url: str) -> None:
    security = dict(get_request_context().metadata.get("security", {}) or {})
    assert_safe_outbound_url(
        url,
        allowlist=list(security.get("ssrf_allowlist", []) or []),
        blocklist=list(security.get("ssrf_blocklist", []) or []),
    )


def _limit_text(text: str, max_chars: int, label: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n\n[truncated: extracted text from {label} exceeded {max_chars} characters; "
        "call again with a narrower page range or a larger max_chars if needed]"
    )


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").strip()


def _markdown_table(rows: list[list[object]]) -> str:
    cleaned = [[_cell_text(cell).replace("|", "\\|") for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(cell for cell in row)]
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    padded = [row + [""] * (width - len(row)) for row in cleaned]
    header = padded[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n".join(lines)


def _docx_to_text(path: Path) -> str:
    from olano.tools._install_hint import optional_import

    docx, err = optional_import("docx", purpose="Word document extraction")
    if err:
        return err

    assert docx is not None  # narrowed: err is None ⇒ import succeeded
    doc = docx.Document(str(path))
    parts: list[str] = []
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for i, table in enumerate(doc.tables, start=1):
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        table_text = _markdown_table(rows)
        if table_text:
            parts.append(f"## Table {i}\n{table_text}")
    return "\n\n".join(parts) if parts else "No text extracted from Word document."


def _pptx_to_text(path: Path, max_slides: int) -> str:
    from olano.tools._install_hint import optional_import

    pptx, err = optional_import("pptx", purpose="PowerPoint document extraction")
    if err:
        return err

    assert pptx is not None  # narrowed: err is None ⇒ import succeeded
    presentation = pptx.Presentation(str(path))
    slides = list(presentation.slides)
    if max_slides > 0:
        slides = slides[:max_slides]
    parts: list[str] = []
    for slide_index, slide in enumerate(slides, start=1):
        slide_parts = [f"# Slide {slide_index}"]
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = "\n".join(
                    paragraph.text.strip()
                    for paragraph in shape.text_frame.paragraphs
                    if paragraph.text.strip()
                )
                if text:
                    slide_parts.append(text)
            if getattr(shape, "has_table", False):
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                table_text = _markdown_table(rows)
                if table_text:
                    slide_parts.append(table_text)
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                slide_parts.append(f"## Speaker Notes\n{notes}")
        except Exception:
            pass
        if len(slide_parts) > 1:
            parts.append("\n\n".join(slide_parts))
    return "\n\n---\n\n".join(parts) if parts else "No text extracted from PowerPoint deck."


def _xlsx_to_text(path: Path, max_sheets: int, max_rows_per_sheet: int) -> str:
    from olano.tools._install_hint import optional_import

    openpyxl, err = optional_import("openpyxl", purpose="Excel workbook extraction")
    if err:
        return err

    assert openpyxl is not None  # narrowed: err is None ⇒ import succeeded
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    sheet_names = workbook.sheetnames if max_sheets <= 0 else workbook.sheetnames[:max_sheets]
    parts: list[str] = [f"# Workbook: {path.name}", f"Sheets: {', '.join(workbook.sheetnames)}"]
    for sheet_name in sheet_names:
        sheet = workbook[sheet_name]
        rows: list[list[object]] = []
        for row in sheet.iter_rows(max_row=max_rows_per_sheet, values_only=True):
            rows.append(list(row))
        table_text = _markdown_table(rows)
        parts.append(
            "\n".join(
                [
                    f"## Sheet: {sheet.title}",
                    f"Dimensions: {sheet.max_row} rows x {sheet.max_column} columns",
                    table_text or "(empty sheet)",
                ]
            )
        )
    if max_sheets > 0 and len(workbook.sheetnames) > max_sheets:
        parts.append(
            f"[truncated: workbook has {len(workbook.sheetnames)} sheets; returned {max_sheets}]"
        )
    return "\n\n".join(parts)


def _html_to_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw, "html.parser")
        return soup.get_text("\n", strip=True)
    except Exception:
        return raw


@tool
def audio_to_text(
    file_path: Annotated[
        str, "Local path or URL to audio file (mp3, mp4, wav, m4a, ogg, flac, webm)"
    ],
    language: Annotated[str, "ISO-639-1 language code (optional, e.g. 'en', 'es', 'zh')"] = "",
    prompt: Annotated[str, "Optional context hint for better transcription"] = "",
) -> str:
    """Transcribe audio to text via the configured speech provider.

    The provider (OpenAI / Groq / a custom self-hosted endpoint) is selected in
    the dashboard Config → Speech section (config.yaml ``speech.stt``) and is
    disabled by default.
    """
    import httpx

    from olano.speech import (
        SpeechDisabledError,
        SpeechNotConfiguredError,
        transcribe_file,
    )

    try:
        if file_path.startswith("http"):
            r = httpx.get(file_path, timeout=60)
            r.raise_for_status()
            suffix = file_path.split(".")[-1][:4]
            tmp_path = Path(f"/tmp/lionclaw_audio.{suffix}")
            tmp_path.write_bytes(r.content)
            file_path = str(tmp_path)

        if not Path(file_path).exists():
            return f"[tool-error] audio_to_text: File not found: {file_path}"

        text = transcribe_file(file_path, language=language, prompt=prompt)
        return text or "No transcript returned."
    except SpeechDisabledError as exc:
        return f"[disabled] {exc}"
    except SpeechNotConfiguredError as exc:
        return f"[missing-credentials] {exc}"
    except Exception as exc:
        return f"[tool-error] audio_to_text: {exc}"


def _handoff_workspace_audio(out: Path) -> None:
    """Hand a voice clip to the sandboxed agent — only when it landed in the workspace.

    `handoff_to_sandbox` is deliberately not for ``/tmp`` or for files outside a
    workspace, and both are exactly what this tool's callers mostly produce: its
    default ``output_path`` is ``/tmp/lionclaw_speech.mp3`` (the container never
    sees it), and the messenger voice-reply path (``platforms/base.py``) writes
    into the runtime's own audio cache. A clip the agent aimed at ``reports/``
    is the other case — that file is the agent's, and without the hand-off it
    could read it and not move or delete it. So the gate is the path, not the
    caller, and the sniff-and-rename in `speech._write_audio` has already
    happened by the time this sees it.
    """
    ws_meta = get_request_context().metadata.get("workspace_root")
    if not ws_meta:
        return
    try:
        out.resolve().relative_to(Path(ws_meta).resolve())
    except ValueError:
        return
    handoff_to_sandbox(out)


@tool
def text_to_speech(
    text: Annotated[str, "Text to convert to speech (max 4096 chars)"],
    voice: Annotated[str, "Voice name (provider-specific; empty = configured default)"] = "",
    output_path: Annotated[
        str,
        "Local path to save the audio file. The extension is corrected to match "
        "the audio the provider actually returns, so use the path in the result.",
    ] = "/tmp/lionclaw_speech.mp3",
) -> str:
    """Generate speech audio from text via the configured speech provider.

    The provider (OpenAI / Groq / Qwen-TTS / a custom self-hosted endpoint) is
    selected in the dashboard Config → Speech section (config.yaml
    ``speech.tts``) and is disabled by default. The container comes from
    ``speech.tts.audio_format`` (blank = per-provider auto), not from
    ``output_path`` — the saved file is renamed to match what came back, so
    always quote the path from the result rather than the one you passed in.
    Pair the result with ``[[audio_as_voice]]`` to have a chat platform deliver
    it as a voice note rather than an audio file.
    """
    from olano.speech import (
        SpeechDisabledError,
        SpeechNotConfiguredError,
        synthesize_speech,
    )

    try:
        out = synthesize_speech(text, output_path, voice=voice)
        _handoff_workspace_audio(Path(out))
        return f"Speech saved to {out} ({Path(out).stat().st_size:,} bytes)"
    except SpeechDisabledError as exc:
        return f"[disabled] {exc}"
    except SpeechNotConfiguredError as exc:
        return f"[missing-credentials] {exc}"
    except Exception as exc:
        return f"[tool-error] text_to_speech: {exc}"


@tool
def video_to_text(
    file_path: Annotated[str, "Local path to video file (mp4, avi, mov, mkv)"],
    extract_audio_only: Annotated[bool, "Extract audio track then transcribe"] = True,
) -> str:
    """Extract and transcribe audio from a video file using ffmpeg + Whisper."""
    import subprocess

    from olano.security.request_context import get_request_context
    from olano.tools._sandbox_exec import (
        host_execution_refused,
        sandbox_active_for_current_agent,
        sandboxed_run,
    )

    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found: {file_path}"

    # Extract audio with ffmpeg. On a managed deployment ffmpeg runs INSIDE the
    # sandbox, so write the audio into the shared workspace (visible to both the
    # sandbox and the in-process transcriber below) rather than /tmp, and refer
    # to the input by its sandbox (/workspace) path.
    ws_meta = get_request_context().metadata.get("workspace_root")
    ws = Path(ws_meta) if ws_meta else path.parent
    audio_name = ".olano_video_audio.mp3"
    host_audio = ws / audio_name
    # ffmpeg runs INSIDE the sandbox on a managed box and writes over this path,
    # so a root-owned leftover from an earlier run blocks the next extraction.
    handoff_to_sandbox(host_audio)

    def _sandbox_path(p: Path) -> str:
        try:
            rel = p.resolve().relative_to(ws.resolve())
            return "/workspace" if str(rel) == "." else f"/workspace/{rel.as_posix()}"
        except Exception:
            return str(p)

    sandboxed = sandbox_active_for_current_agent()
    in_arg = _sandbox_path(path) if sandboxed else str(path)
    out_arg = f"/workspace/{audio_name}" if sandboxed else str(host_audio)

    try:
        result = sandboxed_run(
            ["ffmpeg", "-y", "-i", in_arg, "-vn", "-acodec", "mp3", out_arg],
            timeout=120,
        )
        if host_execution_refused(result):
            # Managed box, no live sandbox: ffmpeg did not run anywhere. The
            # reason is the whole message, so it is not cut to 200 chars.
            return f"Error: {result.stderr}"
        if result.returncode != 0:
            if "not found" in result.stderr.lower():
                return "Error: ffmpeg not available in the execution environment."
            return f"ffmpeg error: {result.stderr[:200]}"
    except FileNotFoundError:
        return "Error: ffmpeg not installed. Install with: brew install ffmpeg"
    except subprocess.TimeoutExpired:
        return "Error: ffmpeg timed out."

    # Transcribe the extracted audio (in-process, host side).
    return str(audio_to_text.invoke({"file_path": str(host_audio)}))


# Older and open-document office formats, converted to the OOXML sibling the
# native extractors read. LibreOffice is in the sandbox image (and on the host
# lane when `install.sh` put it there); it runs through `sandboxed_run`.
_LIBREOFFICE_TARGETS = {
    ".doc": "docx",
    ".odt": "docx",
    ".rtf": "docx",
    ".xls": "xlsx",
    ".ods": "xlsx",
    ".ppt": "pptx",
    ".odp": "pptx",
}

# A scanned PDF is OCR'd page by page (pdftoppm + tesseract, both in the sandbox
# image), roughly 5-15 s a page on a small sandbox, so the pages are capped.
_OCR_MAX_PAGES = 20
# A page yielding fewer characters than this has no text layer (a scan yields
# nothing, or a stray page number). A PDF with at least half its pages like that
# is OCR'd; a sparse page (a cover, a title slide) among real ones is not.
_EMPTY_PAGE_CHARS = 10


@contextmanager
def _scratch_dir() -> Iterator[Path]:
    """A throwaway folder both lanes can write to, removed afterwards.

    It lives under the workspace's state folder because the sandbox mounts the
    workspace at its host path and nothing else of the host (a host ``/tmp``
    path means nothing inside it), and it is handed to the sandbox user so a
    converter running there as uid 1000 can write into it.
    """
    import shutil
    import uuid

    from olano.config.workspace import WORKSPACE_STATE_DIRNAME
    from olano.tools._sandbox_exec import missing_parents

    folder = _workspace_root() / WORKSPACE_STATE_DIRNAME / "convert" / uuid.uuid4().hex[:12]
    created = missing_parents(folder)
    folder.mkdir(parents=True)
    handoff_to_sandbox(folder, created=created)
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _run_converter(argv: list[str], timeout: int) -> tuple[str, str]:
    """Run a converter CLI where the agent's commands run. ``(stdout, error)``."""
    import subprocess

    from olano.tools._sandbox_exec import host_execution_refused, sandboxed_run

    try:
        res = sandboxed_run(argv, timeout=timeout)
    except FileNotFoundError:
        return "", f"{argv[0]} is not installed here"
    except subprocess.TimeoutExpired:
        return "", f"{argv[0]} timed out after {timeout} s"
    if host_execution_refused(res):
        return "", res.stderr
    if res.returncode == 127:
        return "", f"{argv[0]} is not installed in this agent's environment"
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()[-300:]
        return res.stdout, detail or f"{argv[0]} exited with status {res.returncode}"
    return res.stdout, ""


def _convert_with_libreoffice(path: Path, target: str, out_dir: Path) -> tuple[Path | None, str]:
    """Convert ``path`` to ``target`` (docx/xlsx/pptx) into ``out_dir``."""
    _, err = _run_converter(
        [
            "soffice",
            "--headless",
            "--norestore",
            "--convert-to",
            target,
            "--outdir",
            str(out_dir),
            str(path),
        ],
        timeout=180,
    )
    out = out_dir / f"{path.stem}.{target}"
    if out.is_file():
        return out, ""
    return None, err or "LibreOffice produced no output"


def _ocr_pdf(path: Path, pages: int) -> tuple[str, str]:
    """OCR the first ``pages`` pages of a PDF. ``(text, error)``."""
    # dash has no 10#: `expr` strips pdftoppm's zero padding (p-01.png).
    script = (
        'pdftoppm -r 200 -png -l "$3" "$1" "$2/p" || exit 3; '
        'for f in "$2"/p-*.png; do n=${f##*-}; n=$(expr "${n%.png}" + 0); '
        'printf "\\n--- Page %s (OCR) ---\\n" "$n"; tesseract "$f" stdout 2>/dev/null; done'
    )
    with _scratch_dir() as folder:
        out, err = _run_converter(
            ["sh", "-c", script, "ocr", str(path), str(folder), str(pages)],
            timeout=60 + 20 * pages,
        )
    text = out.strip()
    body = "\n".join(line for line in text.splitlines() if not line.startswith("--- Page"))
    if body.strip():
        return text, ""
    return "", err or "OCR found no text on the pages"


def _scanned_pdf_text(path: Path, page_count: int, extracted: str = "") -> str:
    """The text of a PDF with no text layer: OCR, or how to read it otherwise.

    ``extracted`` is what the text layer did yield; it is kept, with the reason,
    when OCR cannot run.
    """
    pages = min(page_count, _OCR_MAX_PAGES)
    try:
        path.resolve().relative_to(_workspace_root().resolve())
        text, err = _ocr_pdf(path, pages)
    except ValueError:
        text, err = "", "the file is outside the workspace, where the OCR tools cannot see it"
    if text:
        more = (
            f"\n\n[OCR covered the first {pages} of {page_count} pages; "
            "ask for the rest by page if you need them.]"
            if page_count > pages
            else ""
        )
        return (
            "[This PDF has no text layer (it looks scanned), so the text below was "
            "read with OCR. Check names and figures against the page images if they "
            f"matter.]\n{text}{more}"
        )
    note = (
        f"[Most pages of this PDF have no text layer (it looks scanned) and OCR did "
        f"not work: {err}. To read them, render the pages to images (with pdftoppm, "
        "if you have a shell) and look at them with image_describe.]"
    )
    return f"{note}\n\n{extracted}" if extracted.strip() else note


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def pdf_to_text(
    file_path: Annotated[str, "Local path or URL to PDF file"],
    max_pages: Annotated[int, "Maximum number of pages to extract (0 = all)"] = 0,
    max_chars: Annotated[
        int,
        "Maximum characters to return (0 = no limit). Defaults to 80000 to keep tool output usable.",
    ] = _DEFAULT_TEXT_LIMIT,
) -> str:
    """Extract text content from a local or downloaded PDF file.

    Prefer this for files in workspace/downloads/. Use web scraping tools only
    for webpages, not for local `file://` PDFs.
    """
    import httpx

    try:
        parsed = urlparse(file_path)
        if parsed.scheme in {"http", "https"}:
            try:
                _assert_safe_url(file_path)
            except SSRFBlockedError as exc:
                return f"Error: {exc}"
            r = httpx.get(file_path, timeout=60)
            r.raise_for_status()
            tmp = Path("/tmp/lionclaw_doc.pdf")
            tmp.write_bytes(r.content)
            path = tmp
        else:
            path = _resolve_local_path(file_path)

        if not path.exists():
            return f"[tool-error] pdf_to_text: File not found: {file_path}"

        from olano.tools._install_hint import optional_import

        pypdf, err = optional_import("pypdf", purpose="PDF text extraction")
        if err:
            return err

        assert pypdf is not None  # narrowed: err is None ⇒ import succeeded
        reader = pypdf.PdfReader(str(path))
        pages = reader.pages if not max_pages else reader.pages[:max_pages]
        text_parts = []
        empty = 0
        for i, page in enumerate(pages):
            text = page.extract_text()
            if len((text or "").strip()) < _EMPTY_PAGE_CHARS:
                empty += 1
            if text:
                text_parts.append(f"--- Page {i + 1} ---\n{text}")
        extracted = "\n\n".join(text_parts)
        if pages and empty * 2 >= len(pages):
            extracted = _scanned_pdf_text(path, len(pages), extracted)
        return _limit_text(extracted or "No text extracted from PDF.", max_chars, path.name)
    except Exception as exc:
        return f"[tool-error] pdf_to_text: {exc}"


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def document_to_text(
    file_path: Annotated[
        str,
        "Local path to a document (.pdf, .docx, .pptx, .xlsx, .doc, .xls, .ppt, .odt, "
        ".ods, .odp, .rtf, .html) or any text file (.txt, .md, .csv, .json, .yaml, code, logs)",
    ],
    max_chars: Annotated[
        int,
        "Maximum characters to return (0 = no limit). Defaults to 80000.",
    ] = _DEFAULT_TEXT_LIMIT,
    max_sheets: Annotated[int, "Maximum spreadsheet sheets to extract (0 = all)."] = 10,
    max_rows_per_sheet: Annotated[int, "Maximum spreadsheet rows per sheet to extract."] = 50,
    max_slides: Annotated[int, "Maximum presentation slides to extract (0 = all)."] = 0,
) -> str:
    """Convert a workspace document to plain text/markdown.

    Supports PDF (with OCR for scanned ones), Word, PowerPoint and Excel, their
    older and OpenDocument formats (.doc/.xls/.ppt/.odt/.ods/.odp/.rtf, converted
    with LibreOffice), HTML, and any text file whatever its extension.
    """
    try:
        path = _resolve_local_path(file_path)
        if not path.exists():
            return f"[tool-error] document_to_text: File not found: {file_path}"

        suffix = path.suffix.lower()

        if suffix in _TEXT_EXTENSIONS:
            return _limit_text(
                path.read_text(encoding="utf-8", errors="replace"), max_chars, path.name
            )

        if suffix == ".pdf":
            return str(pdf_to_text.invoke({"file_path": str(path), "max_chars": max_chars}))

        if suffix in (".html", ".htm"):
            return _limit_text(_html_to_text(path), max_chars, path.name)

        if suffix == ".docx":
            return _limit_text(_docx_to_text(path), max_chars, path.name)

        if suffix == ".pptx":
            return _limit_text(_pptx_to_text(path, max_slides), max_chars, path.name)

        if suffix in (".xlsx", ".xlsm"):
            return _limit_text(
                _xlsx_to_text(path, max_sheets, max_rows_per_sheet), max_chars, path.name
            )

        target = _LIBREOFFICE_TARGETS.get(suffix)
        if target:
            with _scratch_dir() as folder:
                converted, err = _convert_with_libreoffice(path, target, folder)
                if converted is None:
                    return f"[tool-error] document_to_text: could not convert {path.name}: {err}"
                if target == "docx":
                    text = _docx_to_text(converted)
                elif target == "pptx":
                    text = _pptx_to_text(converted, max_slides)
                else:
                    text = _xlsx_to_text(converted, max_sheets, max_rows_per_sheet)
            return _limit_text(text, max_chars, path.name)

        from olano.uploads import ARCHIVE_EXTENSIONS, TEXT_SNIFF_BYTES, looks_like_text

        # Any other text file (code, config, logs, a made-up extension) reads as
        # text. The bytes decide, not a list of extensions.
        with path.open("rb") as fh:
            head = fh.read(TEXT_SNIFF_BYTES)
        if looks_like_text(head):
            return _limit_text(
                path.read_text(encoding="utf-8", errors="replace"), max_chars, path.name
            )
        if suffix in ARCHIVE_EXTENSIONS:
            return (
                f"{path.name} is an archive, not a document. List or unpack it with the "
                f"shell if you have one (unzip -l / tar -tf), then read the files inside."
            )
        return (
            f"{path.name} is a binary file that document_to_text cannot read. Inspect it "
            "with the shell if you have one (for example `file` on its path)."
        )
    except Exception as exc:
        return f"[tool-error] document_to_text: {exc}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
