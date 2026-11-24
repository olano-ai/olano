"""FileOpsAgent tools — file download, compression, conversion.

No API keys required. Uses standard Python + optional system tools.
"""

from __future__ import annotations

import logging
import tarfile
import zipfile
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

from olano.tools._sandbox_exec import (
    handoff_to_sandbox,
    missing_parents,
    sandbox_active_for_current_agent,
)

logger = logging.getLogger(__name__)


@tool
def file_download(
    url: Annotated[str, "URL of the file to download"],
    destination: Annotated[
        str, "Local destination path (optional, defaults to the workspace downloads/ folder)"
    ] = "",
    filename: Annotated[str, "Custom filename (optional, inferred from URL if empty)"] = "",
) -> str:
    """Download a file from a URL with progress reporting.

    When no destination is given, the file is saved to the workspace ``downloads/``
    folder so it's always easy and consistent to find.
    """
    import httpx

    try:
        # Decide before any filesystem side effect (the directories created
        # below are handed to the sandbox only on success): the first URL is
        # checked here, every redirect hop by the request hook below.
        from olano.security.request_context import get_request_context
        from olano.security.ssrf import assert_safe_outbound_url, redirect_guard

        security = dict(get_request_context().metadata.get("security", {}) or {})
        allowlist = list(security.get("ssrf_allowlist", []) or [])
        blocklist = list(security.get("ssrf_blocklist", []) or [])
        assert_safe_outbound_url(url, allowlist=allowlist, blocklist=blocklist)
        hooks = {"request": [redirect_guard(allowlist=allowlist, blocklist=blocklist)]}

        if not destination:
            from olano.config.workspace import get_workspace

            ws_root = get_request_context().metadata.get("workspace_root")
            workspace = get_workspace(str(ws_root) if ws_root else None)
            dest_dir = workspace.root / "downloads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            fname = filename or url.split("/")[-1].split("?")[0] or "download"
            destination = str(dest_dir / fname)

        dest_path = Path(destination)
        created = missing_parents(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        total_bytes = 0
        with (
            httpx.Client(follow_redirects=True, timeout=120, event_hooks=hooks) as client,
            client.stream("GET", url) as r,
        ):
            r.raise_for_status()
            with dest_path.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=8192):
                    f.write(chunk)
                    total_bytes += len(chunk)

        handoff_to_sandbox(dest_path, created=created)
        size_mb = total_bytes / (1024 * 1024)
        return f"Downloaded: {destination} ({size_mb:.1f} MB)"
    except Exception as exc:
        return f"[tool-error] file_download: {exc}"


@tool
def file_compress(
    source_path: Annotated[str, "Path to file or directory to compress"],
    output_path: Annotated[str, "Output archive path (.zip or .tar.gz)"] = "",
    format: Annotated[str, "Archive format: zip or tar.gz"] = "zip",
) -> str:
    """Compress a file or directory into a zip or tar.gz archive."""
    try:
        src = Path(source_path)
        if not src.exists():
            return f"[tool-error] file_compress: {source_path} not found."

        if not output_path:
            output_path = str(src) + (".zip" if format == "zip" else ".tar.gz")

        if format == "zip":
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if src.is_dir():
                    for file in src.rglob("*"):
                        if file.is_file():
                            zf.write(file, file.relative_to(src.parent))
                else:
                    zf.write(src, src.name)
        else:
            with tarfile.open(output_path, "w:gz") as tf:
                tf.add(str(src), arcname=src.name)

        size = Path(output_path).stat().st_size / (1024 * 1024)
        return f"Compressed to {output_path} ({size:.1f} MB)"
    except Exception as exc:
        return f"[tool-error] file_compress: {exc}"


@tool
def file_decompress(
    archive_path: Annotated[str, "Path to zip or tar.gz archive"],
    destination: Annotated[str, "Directory to extract to (optional)"] = "",
) -> str:
    """Decompress a zip or tar.gz archive."""
    try:
        src = Path(archive_path)
        if not src.exists():
            return f"[tool-error] file_decompress: {archive_path} not found."

        dest = destination or str(src.parent / src.stem)
        Path(dest).mkdir(parents=True, exist_ok=True)

        if archive_path.endswith(".zip"):
            with zipfile.ZipFile(str(src), "r") as zf:
                zf.extractall(dest)
        elif archive_path.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar")):
            with tarfile.open(str(src), "r:*") as tf:
                tf.extractall(dest)
        else:
            return f"Unsupported archive format: {src.suffix}. Supported: .zip, .tar.gz, .tgz"

        # An archive can hold thousands of entries, so this re-owns the tree in
        # one bounded walk rather than a chown per extracted file.
        if sandbox_active_for_current_agent():
            from olano.core.builder import _ensure_sandbox_shared_dir

            _ensure_sandbox_shared_dir(Path(dest), recursive=True)
        extracted = list(Path(dest).rglob("*"))
        return f"Extracted {len(extracted)} items to {dest}"
    except Exception as exc:
        return f"[tool-error] file_decompress: {exc}"


@tool
def file_convert(
    source_path: Annotated[str, "Source file path"],
    target_format: Annotated[str, "Target format: json, csv, yaml, txt, md"],
) -> str:
    """Convert between common file formats (JSON↔CSV, YAML↔JSON, etc.)."""
    try:
        import csv
        import json

        src = Path(source_path)
        if not src.exists():
            return f"[tool-error] file_convert: {source_path} not found."

        content = src.read_text(encoding="utf-8")
        src_ext = src.suffix.lower().lstrip(".")
        dest_path = src.with_suffix(f".{target_format}")

        if src_ext == "json" and target_format == "csv":
            data = json.loads(content)
            if isinstance(data, list) and data:
                with dest_path.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=data[0].keys())
                    writer.writeheader()
                    writer.writerows(data)
                handoff_to_sandbox(dest_path)
                return f"Converted JSON → CSV: {dest_path}"
            return "[tool-error] file_convert: JSON must be a list of objects for CSV conversion."

        if src_ext == "csv" and target_format == "json":
            with src.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            dest_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            handoff_to_sandbox(dest_path)
            return f"Converted CSV → JSON: {dest_path}"

        if src_ext in ("yaml", "yml") and target_format == "json":
            from olano.tools._install_hint import optional_import

            yaml, err = optional_import("yaml", purpose="YAML conversion")
            if err:
                return err
            assert yaml is not None
            data = yaml.safe_load(content)
            dest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            handoff_to_sandbox(dest_path)
            return f"Converted YAML → JSON: {dest_path}"

        if src_ext == "json" and target_format in ("yaml", "yml"):
            from olano.tools._install_hint import optional_import

            yaml, err = optional_import("yaml", purpose="YAML conversion")
            if err:
                return err
            assert yaml is not None
            data = json.loads(content)
            dest_path = src.with_suffix(".yaml")
            dest_path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
            handoff_to_sandbox(dest_path)
            return f"Converted JSON → YAML: {dest_path}"

        return f"Unsupported conversion: {src_ext} → {target_format}"
    except Exception as exc:
        return f"[tool-error] file_convert: {exc}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
