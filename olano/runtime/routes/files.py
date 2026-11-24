"""Workspace files, search, MEMORY.md and wiki notes.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer
from olano.tools._sandbox_exec import handoff_to_sandbox, missing_parents, sandbox_active_for

# Per-file cap for the Files view's Upload button. Mirrors the inbound-media
# stash limit (`platforms/inbound.STASH_MAX_BYTES`): big enough for a phone
# video or a large workbook, small enough that one upload cannot fill a disk.
# The dashboard checks it before sending (`views/files/upload.ts`).
FILES_UPLOAD_MAX_BYTES = 100 * 1024 * 1024

# Content types a cross-site HTML form can POST without a CORS preflight.
_FORM_CONTENT_TYPES = frozenset(
    {"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"}
)


def _upload_too_large() -> str:
    return f"File exceeds the {FILES_UPLOAD_MAX_BYTES // (1024 * 1024)} MB upload limit"


# How much of a file's head decides "text or not", and how much of it may be
# undecodable before it stops being text. Git's rule (a NUL in the first 8000
# bytes) catches archives, executables and most media; the ratio catches the
# NUL-free remainder without failing a text file in a legacy 8-bit encoding,
# which carries a few undecodable bytes, not a few in ten.
BINARY_SNIFF_BYTES = 8192
_BINARY_JUNK_RATIO = 0.10
_TEXT_CONTROLS = frozenset("\t\n\r\f\b\x1b")


def looks_binary(head: bytes) -> bool:
    """True when ``head`` (a file's first bytes) is not something to show as text."""
    sample = head[:BINARY_SNIFF_BYTES]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    text = sample.decode("utf-8", errors="replace")
    # A head cut inside a multibyte character ends in one replacement char;
    # that is the read's seam, not the file's content.
    junk = sum(1 for ch in text[:-1] if ch == "\ufffd" or (ch < " " and ch not in _TEXT_CONTROLS))
    return junk > len(text) * _BINARY_JUNK_RATIO


def register_files_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the files routes to ``app``."""
    # ── Files ─────────────────────────────────────────────────────────────────

    def _assert_workspace_access(agent: str | None) -> None:
        """Workspace reads are agent-scoped: the ?agent= param (or the default
        agent it falls back to) must be one the caller can access. Writes are
        already blocked for members centrally (_readonly_member_forbidden).

        Guards the ``_workspace_root`` default-agent fallback: if a named agent
        is authorized but its config no longer exists (a deleted agent whose
        access grant outlived it), we must NOT silently serve the *default*
        agent's workspace — that would hand the member a workspace they were
        never granted. Return 404 for the vanished agent instead.
        """
        from olano.config.loader import find_agent_config_path

        auth_layer.assert_agent_access(agent or _api._default_agent_name())
        if agent and find_agent_config_path(agent) is None:
            raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")

    @app.get("/api/files")
    async def list_files(
        path: str = "",
        agent: str | None = None,
        hidden: bool = False,
        limit: int | None = None,
    ) -> list | dict:
        """List one directory of the agent's workspace.

        Dot-entries are omitted unless ``?hidden=true``: a workspace root holds
        `.olano/`, often a `.git/`, and the tree is a browsing surface, so
        showing them by default buries the files people actually work on. The
        flag is a *listing* preference, not an access boundary — every other
        Files endpoint (read, raw, write, move, delete) already resolves a
        dot-path like `.env` for whoever may reach the workspace at all, so
        opting in reveals nothing that was not already reachable by typing the
        path. The dashboard's Files view drives it from its own toggle.

        ``?limit=N`` caps the listing and switches the response to an envelope
        ``{"entries": [...], "truncated": bool, "total": int}``. Without it the
        response stays the bare list every existing consumer reads: a directory
        with 50,000 entries is a real thing (a scraped corpus, a node_modules an
        agent unpacked) and a tree that renders 50,000 rows is not, so the
        dashboard asks for the first ``N`` and says how many it left out. The
        cap bounds the ``stat`` work too — entries past it are counted from the
        readdir names alone, never stat'ed.
        """
        _assert_workspace_access(agent)
        root = _api._workspace_root(agent)
        target = (root / path) if path else root
        # Containment check (parity with read_file/put/move/delete below): a
        # `path` of `../..` or an absolute path must not escape the workspace to
        # enumerate the product source (/app), /opt/olano, or the home tree.
        # `shared/*` projections resolve outside the workspace too, but are
        # admitted — see _resolve_in_workspace.
        _api._resolve_in_workspace(root, path, agent=agent)
        if not target.exists() or not target.is_dir():
            raise HTTPException(status_code=404, detail=f"Directory not found: {path}")
        if limit is not None and limit < 1:
            raise HTTPException(status_code=400, detail="limit must be a positive integer")

        def _scan() -> list | dict:
            from olano.config.home import get_olano_home
            from olano.runtime.disk_scan import dir_size_lookup

            home = get_olano_home()
            # One resolve() per listing, not one per entry: a plain entry's real
            # path is its parent's real path plus its name, and resolve() walks
            # every component with an lstat each — on a 10k-entry directory that
            # was 10k×depth syscalls for a field only "Copy full path" reads.
            # Symlinks are the exception and keep the full resolve.
            try:
                real_dir = target.resolve()
            except OSError:
                real_dir = target
            files: list[dict] = []
            total = 0
            truncated = False
            for item in sorted(target.iterdir()):
                if not hidden and item.name.startswith("."):
                    continue
                total += 1
                if limit is not None and len(files) >= limit:
                    truncated = True
                    continue  # keep counting, stop stat-ing
                try:
                    st = item.stat()  # once, not twice — size and mtime both come from it
                    is_dir = item.is_dir()
                    abs_path = item.resolve() if item.is_symlink() else real_dir / item.name
                except OSError:
                    # Broken symlink, or the entry vanished between readdir and
                    # stat. Skip it rather than failing the whole listing (this
                    # used to surface as a 500 on any dangling link).
                    total -= 1
                    continue
                entry: dict = {
                    "name": item.name,
                    "path": str(item.relative_to(root)),
                    "abs_path": str(abs_path),
                    "type": "directory" if is_dir else "file",
                    "size": 0 if is_dir else st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                }
                if is_dir:
                    # Directory sizes come from the daily disk scan's snapshot
                    # (cached in-process, keyed on the snapshot mtime) — never
                    # from walking here. Absent = outside the scanned tree,
                    # deeper than the bucket cap, or no scan yet.
                    bucket = dir_size_lookup(home, item)
                    if bucket is not None:
                        entry["scan_bytes"] = int(bucket.get("bytes") or 0)
                        entry["scan_files"] = int(bucket.get("files") or 0)
                files.append(entry)
            if limit is None:
                return files
            return {"entries": files, "truncated": truncated, "total": total}

        # Off the event loop: a listing is a few syscalls per entry, and the
        # dashboard's file tree re-hydrates every expanded directory at once, so
        # these arrive in bursts. On the loop, that burst stalls chat streaming
        # and every other request queued behind it.
        return await asyncio.to_thread(_scan)

    # Directory names a name search never descends into, and the marker files
    # that identify an install tree / embedded repo by content (shared with the
    # cartography walker so the two prune identically).
    _SEARCH_MIN_QUERY = 2
    _SEARCH_MAX_LIMIT = 500
    _SEARCH_BUDGET_SECONDS = 2.0

    def _search_one_workspace(
        root: Path,
        agent_label: str,
        q: str,
        *,
        hidden: bool,
        limit: int,
        deadline: float,
        out: list[dict],
    ) -> bool:
        """Append name matches under ``root`` to ``out``; return True if it stopped
        early (result cap or time budget) so the caller can mark the response
        truncated. An iterative scandir walk, never following symlinks (a
        ``shared/*`` projection or a loop must not pull a foreign tree in), and
        pruned the way the cartography walker prunes: VCS/dependency/cache
        directories by name, install trees and embedded repositories by their
        marker files, dot-entries unless asked for."""
        from olano.cartography.walker import (
            _INSTALL_TREE_MARKERS,
            _REPO_MARKER,
            SKIP_DIR_NAMES,
        )

        stack: list[tuple[Path, str]] = [(root, "")]
        while stack:
            if time.monotonic() > deadline or len(out) >= limit:
                return True
            directory, rel_prefix = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                continue
            names = {e.name for e in entries}
            if rel_prefix and _REPO_MARKER in names:
                continue  # an embedded repository: its tree is not the workspace's
            if not names.isdisjoint(_INSTALL_TREE_MARKERS):
                continue
            for entry in entries:
                if len(out) >= limit:
                    return True
                if not hidden and entry.name.startswith("."):
                    continue
                rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir and entry.name in SKIP_DIR_NAMES:
                        continue
                    if not is_dir and not entry.is_file(follow_symlinks=False):
                        continue
                    if q in entry.name.lower() or q in rel.lower():
                        st = entry.stat(follow_symlinks=False)
                        out.append(
                            {
                                "agent": agent_label,
                                "name": entry.name,
                                "path": rel,
                                "type": "directory" if is_dir else "file",
                                "size": 0 if is_dir else st.st_size,
                                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                            }
                        )
                    if is_dir:
                        stack.append((Path(entry.path), rel))
                except OSError:
                    continue
        return False

    @app.get("/api/files/search")
    async def search_files(
        q: str = "",
        agent: str | None = None,
        hidden: bool = False,
        limit: int = 200,
    ) -> dict:
        """Find files and folders by name across the workspaces the caller may read.

        The dashboard's Files tree lists lazily and its filter box only ever
        narrows what the browser has already loaded — so with every agent row
        collapsed, typing ``MEMOR`` matched nothing although every agent has a
        ``MEMORY.md``. This is the network half of that box: a bounded,
        case-insensitive substring match on the entry name or its
        workspace-relative path, over ``agent``'s workspace when given and over
        every agent the caller can access otherwise (the single-user default
        agent when the roster is empty). Same pruning as the workspace map
        (VCS, dependency and cache trees, embedded repositories), dot-entries
        only with ``?hidden=true``. Bounded three ways — a minimum query length,
        ``limit`` results (≤500) and a wall-clock budget shared across agents —
        and the response says when a bound was hit so the UI can ask for a
        narrower query instead of pretending the list is complete.
        """
        q_norm = q.strip().lower()
        if len(q_norm) < _SEARCH_MIN_QUERY:
            raise HTTPException(
                status_code=400, detail=f"q must be at least {_SEARCH_MIN_QUERY} characters"
            )
        limit = max(1, min(int(limit), _SEARCH_MAX_LIMIT))

        if agent:
            _assert_workspace_access(agent)
            targets: list[tuple[str, str | None]] = [(agent, agent)]
        else:
            from olano.config.loader import find_agent_config_path, list_agents

            allowed = auth_layer.allowed_agent_names()
            names = [
                n
                for n in list_agents()
                if (allowed is None or n in allowed) and find_agent_config_path(n) is not None
            ]
            if allowed is not None and not names:
                # A member granted nothing (or only vanished agents) has no
                # workspace to search; never fall back to the default agent's.
                targets = []
            elif names:
                targets = [(n, n) for n in names]
            else:
                # Nothing on disk yet (a fresh single-user box): the default
                # agent's workspace is the only one there is.
                _assert_workspace_access(None)
                targets = [(_api._default_agent_name(), None)]

        def _scan() -> dict:
            results: list[dict] = []
            truncated = False
            searched: list[str] = []
            deadline = time.monotonic() + _SEARCH_BUDGET_SECONDS
            for label, name in targets:
                try:
                    root = _api._workspace_root(name)
                except Exception:  # an agent whose workspace cannot be resolved
                    continue
                if not root.is_dir():
                    continue
                searched.append(label)
                if _search_one_workspace(
                    root, label, q_norm, hidden=hidden, limit=limit, deadline=deadline, out=results
                ):
                    truncated = True
                    break
            results.sort(key=lambda r: (r["agent"], r["path"]))
            return {"results": results, "truncated": truncated, "agents_searched": searched}

        return await asyncio.to_thread(_scan)

    @app.get("/api/files/{file_path:path}")
    async def read_file(
        file_path: str, agent: str | None = None, max_bytes: int | None = None
    ) -> dict:
        """Read a workspace file as text for the preview/editor.

        Reads at most ``max_bytes`` (a client ask, itself clamped to the server
        cap ``LIONCLAW_FILES_READ_MAX_BYTES``, default 8 MiB). This used to be
        ``read_text()`` on whatever the path named: a 2 GB log an agent left
        behind was read into the runtime's memory whole, JSON-encoded, and
        shipped to a browser that then tried to render it. The response says
        when it is a head (``truncated``) and how big the file really is
        (``total_size``, bytes) so the client can offer the download instead —
        and must not offer *Edit*, since saving a head would destroy the tail.
        ``size`` keeps its historical meaning (characters returned).

        A file whose head is not text (``binary``: a zip, an Office document, an
        executable) comes back with empty ``content``: decoding it with
        ``errors="replace"`` only ever produced a screen of mojibake, shipped a
        megabyte to do it, and offered *Edit*, whose save would have written the
        replacement characters over the real bytes.
        """
        _assert_workspace_access(agent)
        root = _api._workspace_root(agent)
        target = root / file_path
        _api._resolve_in_workspace(root, file_path, agent=agent)

        if not target.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {file_path}")
        if max_bytes is not None and max_bytes < 1:
            raise HTTPException(status_code=400, detail="max_bytes must be a positive integer")

        cap = _api._files_read_max_bytes()
        if max_bytes is not None:
            cap = min(cap, max_bytes)

        def _read() -> dict:
            total_size = target.stat().st_size
            with target.open("rb") as fh:
                head = fh.read(cap)
            binary = looks_binary(head)
            content = "" if binary else head.decode("utf-8", errors="replace")
            return {
                "path": file_path,
                "content": content,
                "size": len(content),
                "truncated": total_size > len(head),
                "total_size": total_size,
                "binary": binary,
            }

        try:
            return await asyncio.to_thread(_read)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.put("/api/files/{file_path:path}")
    async def write_file(
        file_path: str,
        body: _api.FileWriteRequest,
        agent: str | None = None,
    ) -> dict:
        """Overwrite or create a workspace file with new UTF-8 text content.

        Used by the dashboard's inline editor. Creates the file (and any
        missing parent directories) if it does not already exist.
        Path traversal outside the workspace root is forbidden.
        """
        root = _api._workspace_root(agent)
        target = root / file_path
        _api._resolve_in_workspace(root, file_path, agent=agent, write=True)

        if target.exists() and not target.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {file_path}")

        try:
            created = missing_parents(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body.content, encoding="utf-8")
            # This request runs outside agent_runtime_context, so the hand-off is
            # given the agent by name — without it the sandboxed agent could not
            # edit a file the Files view just wrote into its own workspace.
            handoff_to_sandbox(target, created=created, agent=agent)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        size = target.stat().st_size
        _api._push_event("file_updated", {"agent": agent, "path": file_path, "size": size})
        # Editing a prompt-bearing workspace file (Files view / editor) changes
        # the stable prompt sections — nudge the refresh sweep. Basename match
        # covers zone copies too (extensions/prompts/AGENTS.md etc.). Without
        # an ?agent= hint the workspace may be shared, so check everyone.
        if Path(file_path).name in _api._PROMPT_BEARING_BASENAMES:
            try:
                nudge = getattr(runtime, "request_prompt_refresh", None)
                if nudge is not None:
                    nudge([agent] if agent else None)
            except Exception:  # pragma: no cover - defensive
                pass
        return {"path": file_path, "size": size}

    @app.get("/api/files-raw/{file_path:path}")
    async def read_file_raw(
        file_path: str, agent: str | None = None, download: bool = False
    ) -> Any:
        """Stream the raw bytes of a workspace file with a best-guess Content-Type.

        The dashboard uses this for image / PDF / video / audio previews so the
        browser can render them natively (no base64 round-trip), and for the
        Download action on chat file cards (``?download=1`` → attachment).

        Members read here too (scoped by ``_assert_workspace_access``), so the
        response is hardened for every caller: ``nosniff`` stops a browser
        promoting bytes to a script-capable type, and a script-capable type
        (HTML, SVG, XML) opened directly is served under a CSP ``sandbox`` — a
        unique origin with no scripts and no cookie access — so a workspace
        file can never run as the dashboard. An ``<img>`` embedding of an SVG
        never executed scripts in the first place, so inline previews are
        unaffected.
        """
        import mimetypes

        from starlette.responses import FileResponse

        _assert_workspace_access(agent)
        root = _api._workspace_root(agent)
        target = root / file_path
        _api._resolve_in_workspace(root, file_path, agent=agent)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {file_path}")
        mime, _ = mimetypes.guess_type(str(target))
        media_type = mime or "application/octet-stream"
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private"}
        if media_type in _api._SCRIPT_CAPABLE_MIME_TYPES:
            headers["Content-Security-Policy"] = "sandbox"
        return FileResponse(
            str(target),
            media_type=media_type,
            headers=headers,
            filename=target.name if download else None,
        )

    @app.post("/api/files/move")
    async def move_file(body: _api.FileMoveRequest, agent: str | None = None) -> dict:
        """Move or rename a workspace file/directory. Both paths are
        workspace-relative; parent directories of the destination are created
        as needed. Used by the dashboard's drag-and-drop and context menu."""
        import shutil

        root = _api._workspace_root(agent)
        src = root / body.source
        dst = root / body.dest
        src_r = _api._resolve_in_workspace(root, body.source, agent=agent, write=True)
        dst_r = _api._resolve_in_workspace(root, body.dest, agent=agent, write=True)

        if not src.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {body.source}")
        if dst.exists():
            raise HTTPException(status_code=409, detail=f"Already exists: {body.dest}")
        # Forbid moving a directory into itself or one of its descendants.
        if src.is_dir() and (dst_r == src_r or src_r in dst_r.parents):
            raise HTTPException(status_code=400, detail="Cannot move a folder into itself")

        try:
            created = missing_parents(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            # A same-device move keeps the entry's ownership, so this is really
            # about the directories the move had to create.
            handoff_to_sandbox(dst, created=created, agent=agent)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        _api._push_event("file_updated", {"agent": agent, "path": body.dest})
        return {"ok": True, "source": body.source, "path": body.dest}

    @app.post("/api/files/create")
    async def create_file(body: _api.FileCreateRequest, agent: str | None = None) -> dict:
        """Create an empty file or a new directory in the workspace. Fails with
        409 if the target already exists. Used by the dashboard's New File /
        New Folder actions."""
        root = _api._workspace_root(agent)
        target = root / body.path
        _api._resolve_in_workspace(root, body.path, agent=agent, write=True)
        if target.exists():
            raise HTTPException(status_code=409, detail=f"Already exists: {body.path}")

        try:
            created = missing_parents(target)
            if body.type == "directory":
                target.mkdir(parents=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("", encoding="utf-8")
            handoff_to_sandbox(target, created=created, agent=agent)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        _api._push_event("file_updated", {"agent": agent, "path": body.path})
        return {"ok": True, "path": body.path, "type": body.type}

    @app.post("/api/files/upload")
    async def upload_file(
        request: Request,
        name: str,
        folder: str = Query("", alias="dir"),
        agent: str | None = None,
        overwrite: bool = False,
    ) -> dict:
        """Upload one file from the user's device into a workspace directory.

        The body is the file's raw bytes (``application/octet-stream``), read
        as a stream and written to a temp file beside the target, then renamed
        into place, so a dropped connection never leaves half a file under the
        real name and a large upload never sits in memory. ``name`` must be a
        single path component; ``dir`` is workspace-relative and is created
        when missing. An existing file is a 409 unless ``overwrite`` is set.
        Read-only members never reach this (``auth._readonly_member_forbidden``
        blocks every write under ``/api/files``).

        The three content types an HTML form can send cross-site without a
        CORS preflight are refused, so this raw-body endpoint cannot be driven
        by another site's form.
        """
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type in _FORM_CONTENT_TYPES:
            raise HTTPException(
                status_code=415, detail="Send the file's bytes as application/octet-stream"
            )
        clean = name.strip()
        if (
            not clean
            or clean in (".", "..")
            or any(c in clean for c in ("/", "\\", "\x00"))
            or len(clean.encode("utf-8")) > 255
        ):
            raise HTTPException(status_code=400, detail=f"Invalid file name: {name!r}")
        dir_rel = folder.strip().strip("/")
        rel = f"{dir_rel}/{clean}" if dir_rel else clean

        root = _api._workspace_root(agent)
        target = root / rel
        _api._resolve_in_workspace(root, rel, agent=agent, write=True)
        if target.exists():
            # Structured, so the dashboard can tell "ask to replace" from
            # "cannot replace" without matching on wording.
            if not target.is_file():
                raise HTTPException(
                    status_code=409,
                    detail={"code": "folder_exists", "message": f"A folder named {clean} exists"},
                )
            if not overwrite:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "file_exists", "message": f"Already exists: {rel}"},
                )
        if target.parent.exists() and not target.parent.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a folder: {dir_rel}")

        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > FILES_UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail=_upload_too_large())

        created = missing_parents(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        tmp = target.parent / f".{clean[:200]}.{os.getpid()}.{time.monotonic_ns()}.upload"
        size = 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > FILES_UPLOAD_MAX_BYTES:
                        raise HTTPException(status_code=413, detail=_upload_too_large())
                    await asyncio.to_thread(fh.write, chunk)
            tmp.replace(target)
            handoff_to_sandbox(target, created=created, agent=agent)
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(e))

        _api._push_event("file_updated", {"agent": agent, "path": rel, "size": size})
        if clean in _api._PROMPT_BEARING_BASENAMES:
            try:
                nudge = getattr(runtime, "request_prompt_refresh", None)
                if nudge is not None:
                    nudge([agent] if agent else None)
            except Exception:  # pragma: no cover - defensive
                pass
        return {"ok": True, "path": rel, "size": size}

    @app.post("/api/files/duplicate")
    async def duplicate_file(body: _api.FileDuplicateRequest, agent: str | None = None) -> dict:
        """Duplicate a workspace file/directory next to the original, naming the
        copy "<name> copy[.ext]" (incrementing if that already exists)."""
        import shutil

        root = _api._workspace_root(agent)
        src = root / body.path
        _api._resolve_in_workspace(root, body.path, agent=agent, write=True)
        if not src.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {body.path}")

        stem = src.stem if src.is_file() else src.name
        suffix = src.suffix if src.is_file() else ""
        dst = src.parent / f"{stem} copy{suffix}"
        i = 2
        while dst.exists():
            dst = src.parent / f"{stem} copy {i}{suffix}"
            i += 1

        # copytree of a large directory would block the loop for its duration.
        def _copy() -> None:
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
                # copy2 preserves mode, never uid: a tree copied by the runtime
                # is root-owned throughout. Re-own it in the same thread rather
                # than after the await — this can be thousands of entries.
                if sandbox_active_for(agent):
                    from olano.core.builder import _ensure_sandbox_shared_dir

                    _ensure_sandbox_shared_dir(dst, recursive=True)
            else:
                shutil.copy2(str(src), str(dst))
                handoff_to_sandbox(dst, agent=agent)

        try:
            await asyncio.to_thread(_copy)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        rel = str(dst.relative_to(root))
        _api._push_event("file_updated", {"agent": agent, "path": rel})
        return {"ok": True, "path": rel}

    @app.delete("/api/files/{file_path:path}")
    async def delete_file(file_path: str, agent: str | None = None) -> dict:
        """Delete a workspace file or directory (recursively). Path traversal
        outside the workspace root — and deleting the root itself — is forbidden."""
        import shutil

        root = _api._workspace_root(agent)
        target = root / file_path
        target_r = _api._resolve_in_workspace(root, file_path, agent=agent, write=True)
        if target_r == root.resolve():
            raise HTTPException(status_code=403, detail="Cannot delete the workspace root")
        # Same rule one level out: `shared/<name>` is a managed projection of a
        # share source, so deleting it would take the whole cross-agent share
        # (every other agent's view of it included), not one agent's file.
        if any(target_r == shared for shared in _api._shared_projection_roots(agent, write=True)):
            raise HTTPException(status_code=403, detail="Cannot delete a shared folder root")
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Not found: {file_path}")

        # rmtree of a large directory would block the loop for its duration.
        def _delete() -> None:
            if target.is_dir():
                shutil.rmtree(str(target))
            else:
                target.unlink()

        try:
            await asyncio.to_thread(_delete)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        _api._push_event("file_updated", {"agent": agent, "path": file_path})
        return {"ok": True, "path": file_path}

    # ── Memory ────────────────────────────────────────────────────────────────

    @app.get("/api/memory")
    async def read_memory(agent: str | None = None) -> dict:
        from olano.memory.markdown_memory import MarkdownMemory

        _assert_workspace_access(agent)
        mem = MarkdownMemory(_api._workspace_root(agent))
        content = mem.read()
        return {"content": content, "size": len(content)}

    @app.post("/api/memory")
    async def append_memory(body: _api.MemoryAppendRequest, agent: str | None = None) -> dict:
        from olano.memory.markdown_memory import MarkdownMemory

        mem = MarkdownMemory(_api._workspace_root(agent))
        if body.section:
            mem.append_fact(body.content, section=body.section)
        else:
            mem.append_daily(body.content)
        _api._push_event("memory_updated", {"agent": agent, "size": len(mem.read())})
        return {"ok": True}

    @app.put("/api/memory")
    async def write_memory(body: _api.MemoryWriteRequest, agent: str | None = None) -> dict:
        """Overwrite MEMORY.md wholesale — backs the dashboard's inline editor.

        Unlike POST (which appends a single fact/daily note), this replaces the
        entire document with the editor's content via MarkdownMemory.write(),
        which stages to a sibling .tmp and atomically renames so a failed write
        never leaves MEMORY.md half-rewritten.
        """
        from olano.memory.markdown_memory import MarkdownMemory

        mem = MarkdownMemory(_api._workspace_root(agent))
        mem.write(body.content)
        # write() is a verbatim atomic passthrough, so the on-disk length equals
        # the body we just wrote — use it directly rather than re-reading the file
        # (avoids redundant blocking I/O and a TOCTOU window). Char count, not
        # bytes, to match GET /api/memory's size convention.
        size = len(body.content)
        _api._push_event("memory_updated", {"agent": agent, "size": size})
        return {"ok": True, "size": size}

    # ── Notes (workspace markdown wiki) ───────────────────────────────────────
    # Dashboard surface for the notes_* agent tools: the [[wikilink]]-aware
    # markdown wiki at <workspace>/notes/ (olano/memory/knowledge_vault.py).

    def _notes_store(agent: str | None):
        """NotesStore over the agent's workspace ``notes/`` dir. Construction
        syncs on-disk .md files into the SQLite index, so notes written by the
        agent tools (or by hand) list correctly even if this process never ran
        them. Blocking sqlite + file IO — call via asyncio.to_thread."""
        from olano.memory.notes_store import NotesStore

        return NotesStore(notes_dir=_api._workspace_root(agent) / "notes")

    def _note_meta(entry) -> dict:
        return {
            "title": entry.title,
            "tags": entry.tags,
            "aliases": entry.aliases,
            "auto_inject": entry.auto_inject,
            "created": entry.created,
            "updated": entry.updated,
            "snippet": entry.content[:200],
        }

    @app.get("/api/notes")
    async def list_notes(agent: str | None = None, q: str = "", limit: int = 200) -> dict:
        """List the agent's wiki notes (most recently updated first), or search
        them when ``q`` is given. Search is FTS5 keyword match only — the
        hybrid semantic path the notes_search tool uses would pull Chroma +
        an embedding model onto the request path, far too heavy for typing."""
        _assert_workspace_access(agent)

        def _load() -> dict:
            store = _notes_store(agent)
            if q.strip():
                entries = store.search(q, limit=min(max(limit, 1), 50))
            else:
                entries = [e for t in store.list_all(limit=limit) if (e := store.get(t))]
            return {
                "notes": [_note_meta(e) for e in entries],
                "count": store.entry_count(),
                "link_count": store.link_count(),
                "query": q,
            }

        return await asyncio.to_thread(_load)

    @app.get("/api/notes/entry")
    async def read_note(title: str, agent: str | None = None) -> dict:
        """One note by title (or alias), with its full content and link graph
        neighbourhood — what the notes_get tool shows the agent."""
        _assert_workspace_access(agent)

        def _load() -> dict | None:
            store = _notes_store(agent)
            entry = store.get(title)
            if entry is None:
                return None
            meta = _note_meta(entry)
            meta.update(
                {
                    "content": entry.content,
                    "sources": entry.sources,
                    "outlinks": store.get_outlinks(entry.title),
                    "backlinks": store.get_backlinks(entry.title),
                }
            )
            return meta

        result = await asyncio.to_thread(_load)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Note '{title}' not found")
        return result

    @app.post("/api/notes")
    async def upsert_note(body: _api.NoteUpsertRequest, agent: str | None = None) -> dict:
        """Create or update a note — the human side of notes_add. Member writes
        are blocked centrally (_readonly_member_forbidden), same as /api/memory."""
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Note title is required")

        def _save() -> list[str]:
            from olano.memory.notes_store import NotesEntry, NotesStore

            store = _notes_store(agent)
            existing = store.get(title)
            entry = NotesEntry(
                title=title,
                content=body.content,
                tags=[t.strip() for t in body.tags if t.strip()],
                aliases=[a.strip() for a in body.aliases if a.strip()],
                sources=[s.strip() for s in body.sources if s.strip()],
                # Keep the original creation stamp on update; __post_init__
                # fills "" with now for a brand-new note.
                created=existing.created if existing else "",
                auto_inject=body.auto_inject,
            )
            store.upsert(entry)
            return NotesStore.parse_wikilinks(body.content)

        links = await asyncio.to_thread(_save)
        _api._push_event("notes_updated", {"agent": agent, "title": title})
        return {"ok": True, "title": title, "links": links}

    @app.delete("/api/notes/entry")
    async def delete_note(title: str, agent: str | None = None) -> dict:
        def _delete() -> bool:
            store = _notes_store(agent)
            # Resolve aliases to the canonical title so deleting by alias
            # removes the right file + index rows.
            entry = store.get(title)
            if entry is None:
                return False
            store.delete(entry.title)
            return True

        found = await asyncio.to_thread(_delete)
        if not found:
            raise HTTPException(status_code=404, detail=f"Note '{title}' not found")
        _api._push_event("notes_updated", {"agent": agent, "title": title})
        return {"ok": True, "title": title}
