"""HTTP fetch and download tools."""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Annotated, Any, cast

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.security.request_context import get_request_context
from olano.security.ssrf import SSRFBlockedError, assert_safe_outbound_url, redirect_guard
from olano.security.tool_metadata import tool_metadata
from olano.tools._sandbox_exec import handoff_to_sandbox

_INLINE_LIMIT = 2000  # bytes — responses smaller than this are returned inline
_SAVE_LIMIT = 10 * 1024 * 1024  # 10 MB max download


def _safe_output_path(url: str, suffix: str = ".txt") -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", url.split("?")[0])[-60:] or "response"
    return _web_scraping_dir() / f"{safe}{suffix}"


def _summarize_json_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        lines = [f"Top-level keys: {', '.join(list(payload.keys())[:8]) or '(none)'}"]
        for key, value in list(payload.items())[:5]:
            if isinstance(value, list):
                line = f"- {key}: list[{len(value)}]"
                if value:
                    first = json.dumps(value[0], ensure_ascii=False)[:120]
                    last = json.dumps(value[-1], ensure_ascii=False)[:120]
                    line += f" first={first}"
                    if len(value) > 1:
                        line += f" last={last}"
                lines.append(line)
            elif isinstance(value, dict):
                lines.append(f"- {key}: object with keys {', '.join(list(value.keys())[:6])}")
            else:
                preview = json.dumps(value, ensure_ascii=False)[:120]
                lines.append(f"- {key}: {preview}")
        return "\n".join(lines)
    if isinstance(payload, list):
        lines = [f"Top-level value: list[{len(payload)}]"]
        if payload:
            lines.append(f"- first: {json.dumps(payload[0], ensure_ascii=False)[:160]}")
            if len(payload) > 1:
                lines.append(f"- last: {json.dumps(payload[-1], ensure_ascii=False)[:160]}")
        return "\n".join(lines)
    return f"Top-level value: {type(payload).__name__} = {json.dumps(payload, ensure_ascii=False)[:160]}"


def _workspace_root() -> Path:
    """Resolve the *running agent's* workspace root from the request context.

    Tools execute inside a request context that carries the agent's real
    workspace root (set in ``AgentRunner._request_context_metadata``). Falling
    back to a bare ``get_workspace()`` would resolve to the legacy/global
    workspace instead of e.g. ``~/.olano/agents/<name>/workspace``.
    """
    workspace_root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(workspace_root) if workspace_root else None).root


def _downloads_dir() -> Path:
    d = _workspace_root() / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _web_scraping_dir() -> Path:
    d = _workspace_root() / "web_scraping"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _http_fetch_impl(
    url: Annotated[str, "URL to fetch"],
    method: Annotated[str, "HTTP method: GET, POST, PUT, PATCH, DELETE"] = "GET",
    headers: Annotated[str, "JSON string of request headers (optional)"] = "",
    body: Annotated[str, "Request body as string (optional, for POST/PUT/PATCH)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 60)"] = 30,
    inline: Annotated[bool, "Return the full body inline instead of saving it to a file"] = False,
) -> str:
    if httpx is None:
        return "Error: httpx not installed. Run: uv add httpx"

    ctx = get_request_context()
    security = dict(ctx.metadata.get("security", {}) or {})
    ssrf_allowlist = list(security.get("ssrf_allowlist", []) or [])
    ssrf_blocklist = list(security.get("ssrf_blocklist", []) or [])
    try:
        assert_safe_outbound_url(url, allowlist=ssrf_allowlist, blocklist=ssrf_blocklist)
    except SSRFBlockedError as exc:
        return f"Error: {exc}"

    timeout = min(timeout, 60)
    method = method.upper()

    parsed_headers: dict[str, str] = {}
    if headers:
        try:
            parsed_headers = json.loads(headers)
        except json.JSONDecodeError:
            return "Error: headers must be a valid JSON string."

    kwargs: dict[str, Any] = {
        "headers": parsed_headers,
        "timeout": timeout,
        "follow_redirects": True,
    }
    if body:
        # Auto-detect JSON body
        try:
            kwargs["json"] = json.loads(body)
        except json.JSONDecodeError:
            kwargs["content"] = body.encode()

    # Every redirect hop gets the same SSRF decision as the first URL.
    hooks = {"request": [redirect_guard(allowlist=ssrf_allowlist, blocklist=ssrf_blocklist)]}
    try:
        with httpx.Client(event_hooks=hooks) as client:
            response = getattr(client, method.lower())(url, **kwargs)
    except httpx.TimeoutException:
        return f"Error: request timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"

    status = response.status_code
    content_type = response.headers.get("content-type", "")
    body_bytes = response.content

    header_summary = (
        f"Status: {status}\nContent-Type: {content_type}\nSize: {len(body_bytes)} bytes"
    )

    if "json" in content_type and not inline:
        try:
            payload = response.json()
            out_path = _safe_output_path(url, suffix=".json")
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            handoff_to_sandbox(out_path, created=[out_path.parent])
            summary = _summarize_json_payload(payload)
            return f"{header_summary}\n\n{summary}\n\nFull JSON saved to: {out_path}"
        except Exception:
            pass

    if len(body_bytes) <= _INLINE_LIMIT and inline:
        try:
            text = body_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = repr(body_bytes)
        return f"{header_summary}\n\n{text}"

    # Save larger responses
    out_path = _safe_output_path(url)
    out_path.write_bytes(body_bytes)
    handoff_to_sandbox(out_path, created=[out_path.parent])
    if len(body_bytes) <= _INLINE_LIMIT:
        try:
            text = body_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = repr(body_bytes)
        preview = text[:300] + ("…" if len(text) > 300 else "")
        return f"{header_summary}\n\nPreview:\n{preview}\n\nFull response saved to: {out_path}"
    return f"{header_summary}\n\nResponse saved to: {out_path}"


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def http_request(
    url: Annotated[str, "URL to fetch"],
    method: Annotated[str, "HTTP method: GET, POST, PUT, PATCH, DELETE"] = "GET",
    headers: Annotated[str, "JSON string of request headers (optional)"] = "",
    body: Annotated[str, "Request body as string (optional, for POST/PUT/PATCH)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 60)"] = 30,
    inline: Annotated[bool, "Return the full body inline instead of saving it to a file"] = False,
) -> str:
    """
    Raw HTTP and API request tool.
    Use this for JSON endpoints, custom headers, auth, or non-GET methods rather than ordinary webpage reading.
    """
    return _http_fetch_impl(
        url=url,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        inline=inline,
    )


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def http_fetch_api(
    url: Annotated[str, "URL to fetch"],
    method: Annotated[str, "HTTP method: GET, POST, PUT, PATCH, DELETE"] = "GET",
    headers: Annotated[str, "JSON string of request headers (optional)"] = "",
    body: Annotated[str, "Request body as string (optional, for POST/PUT/PATCH)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 60)"] = 30,
    inline: Annotated[bool, "Return the full body inline instead of saving it to a file"] = False,
) -> str:
    """Compatibility alias for `http_request`."""
    return cast(
        str,
        http_request.func(
            url=url, method=method, headers=headers, body=body, timeout=timeout, inline=inline
        ),
    )


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False)
@tool
def http_fetch(
    url: Annotated[str, "URL to fetch"],
    method: Annotated[str, "HTTP method: GET, POST, PUT, PATCH, DELETE"] = "GET",
    headers: Annotated[str, "JSON string of request headers (optional)"] = "",
    body: Annotated[str, "Request body as string (optional, for POST/PUT/PATCH)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 60)"] = 30,
    inline: Annotated[bool, "Return the full body inline instead of saving it to a file"] = False,
) -> str:
    """Compatibility alias for `http_request`."""
    return cast(
        str,
        http_request.func(
            url=url, method=method, headers=headers, body=body, timeout=timeout, inline=inline
        ),
    )


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def http_download(
    url: Annotated[str, "URL of the file to download"],
    filename: Annotated[str, "Filename to save as (optional, inferred from URL if empty)"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 300)"] = 60,
) -> str:
    """
    Download a file from a URL to workspace/downloads/.
    Supports any file type: PDFs, CSVs, images, ZIPs, etc.
    """
    if httpx is None:
        return "Error: httpx not installed. Run: uv add httpx"

    timeout = min(timeout, 300)

    if not filename:
        # Infer from URL
        url_path = url.split("?")[0].rstrip("/")
        filename = url_path.split("/")[-1] or "download"
        # Add extension if missing
        if "." not in filename.split("/")[-1]:
            _, ext = mimetypes.guess_type(url)
            if ext:
                filename += ext

    out_path = _downloads_dir() / filename
    ctx = get_request_context()
    security = dict(ctx.metadata.get("security", {}) or {})
    ssrf_allowlist = list(security.get("ssrf_allowlist", []) or [])
    ssrf_blocklist = list(security.get("ssrf_blocklist", []) or [])
    try:
        assert_safe_outbound_url(url, allowlist=ssrf_allowlist, blocklist=ssrf_blocklist)
    except SSRFBlockedError as exc:
        return f"Error: {exc}"

    # Every redirect hop gets the same SSRF decision as the first URL.
    hooks = {"request": [redirect_guard(allowlist=ssrf_allowlist, blocklist=ssrf_blocklist)]}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, event_hooks=hooks) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                content_length = int(response.headers.get("content-length", 0))
                if content_length > _SAVE_LIMIT:
                    return (
                        f"Error: file too large ({content_length / 1024 / 1024:.1f} MB). Max 10 MB."
                    )

                total = 0
                with open(out_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        f.write(chunk)
                        total += len(chunk)
                        if total > _SAVE_LIMIT:
                            f.flush()
                            return (
                                f"Error: download exceeded 10 MB limit, partial file at {out_path}"
                            )

    except Exception as e:
        return f"Error downloading {url}: {e}"

    handoff_to_sandbox(out_path, created=[out_path.parent])
    size_kb = out_path.stat().st_size / 1024
    result = f"Downloaded {url}\nSaved to: {out_path}\nSize: {size_kb:.1f} KB"
    if out_path.suffix.lower() == ".pdf":
        result += "\n\nTo read or summarize this PDF, call pdf_to_text or document_to_text on the saved path."
    return result


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
