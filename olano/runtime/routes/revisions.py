"""Home revision history: browse commits and diffs, and sync with GitHub.

The dashboard never touches ``revisions.git`` directly. Reads (log, one
commit's file list, one file's diff, a file at a commit) are open to anyone
who can open the Revisions page, as before. Everything under
``/api/revisions/remote`` is admin-only (``auth._ADMIN_ONLY_PREFIXES``):
connecting a repository sends the history off the box, and a pull writes
into live configuration.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

if TYPE_CHECKING:
    from olano.revisions.engine import RevisionsEngine

logger = logging.getLogger(__name__)

# RemoteError codes that are the user's to fix (a bad name, a public repo, a
# pending merge) answer 400/409; the rest are an upstream problem (502).
_CLIENT_CODES = {
    "invalid_repo",
    "invalid_branch",
    "invalid_sync",
    "invalid_auth",
    "no_token",
    "no_connection",
    "public_repo",
    "not_empty",
    "no_push",
    "not_connected",
    "vault_locked",
    "markers_left",
    "unresolved",
    "invalid_file",
    "unsafe_path",
}
_CONFLICT_CODES = {
    "busy",
    "rejected",
    "merge_pending",
    "no_merge",
    "not_conflicted",
    "changed_during_pull",
    "unrelated",
}


def _actor() -> str:
    """The dashboard human as a commit author (``human-<user>``)."""
    from olano.runtime.issues_routes import _actor as issues_actor

    return issues_actor()


def _http(exc: Exception) -> HTTPException:
    from olano.revisions.remote import RemoteError

    if isinstance(exc, RemoteError):
        status = 400 if exc.code in _CLIENT_CODES else 409 if exc.code in _CONFLICT_CODES else 502
        return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.warning("revisions: request failed", exc_info=exc)
    return HTTPException(status_code=500, detail=f"revisions: {exc}")


def _request_push() -> None:
    """A pull or merge moved the head: let the worker push it if sync is automatic."""
    try:
        from olano.revisions.worker import get_revisions_worker

        worker = get_revisions_worker(start=False)
        if worker is not None:
            worker.request_push()
    except Exception:
        pass


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _engine() -> RevisionsEngine:
    from olano.revisions.engine import home_engine

    return home_engine()


def register_revisions_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach /api/revisions* to ``app``."""

    # ── history (read) ────────────────────────────────────────────────────

    @app.get("/api/revisions/log")
    async def revisions_log(path: str = "", limit: int = 50, skip: int = 0) -> dict:
        """One page of the history: ``limit`` commits after the newest ``skip``,
        plus ``total`` (the whole history, or one file's) for the pager."""
        engine = _engine()
        entries = await asyncio.to_thread(lambda: engine.log(path=path, limit=limit, skip=skip))
        return {
            "entries": entries,
            "total": await asyncio.to_thread(lambda: engine.count(path)),
            "skip": max(0, skip),
            "repo_bytes": await asyncio.to_thread(engine.repo_size_bytes),
            # Tracked files whose current version is not being recorded
            # (over max_file_kb, carrying a credential, or an unparseable
            # agent.json5); history keeps their last recorded version.
            "held_back": await asyncio.to_thread(engine.held_back),
            "max_file_kb": engine.max_file_bytes // 1024,
        }

    @app.get("/api/revisions/file")
    async def revisions_file(path: str, rev: str = "HEAD") -> dict:
        """A file at a commit. Text comes back as ``content``; a document,
        image or recording comes back as ``binary: true`` with its size, and
        is read through ``/api/revisions/raw``."""
        try:
            data = await asyncio.to_thread(lambda: _engine().show_blob(path, rev))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if data is None:
            raise HTTPException(status_code=404, detail=f"{path} not found at {rev}")
        text = None if b"\0" in data[:8192] else _decode(data)
        if text is None:
            return {"path": path, "rev": rev, "binary": True, "size": len(data), "content": ""}
        return {"path": path, "rev": rev, "binary": False, "size": len(data), "content": text}

    @app.get("/api/revisions/raw")
    async def revisions_raw(path: str, rev: str = "HEAD") -> Response:
        """A file's exact bytes at a commit, always as a download: content
        from history must never render as the dashboard's own page."""
        try:
            data = await asyncio.to_thread(lambda: _engine().show_blob(path, rev))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if data is None:
            raise HTTPException(status_code=404, detail=f"{path} not found at {rev}")
        name = path.rsplit("/", 1)[-1] or "file"
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(
            content=data,
            media_type=media,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/api/revisions/commit")
    async def revisions_commit(rev: str) -> dict:
        """One commit: metadata, the files it changed and the line totals."""
        try:
            details = await asyncio.to_thread(lambda: _engine().commit_details(rev))
        except Exception as exc:
            raise _http(exc) from exc
        if details is None:
            raise HTTPException(status_code=404, detail=f"no commit {rev}")
        return details

    @app.get("/api/revisions/diff")
    async def revisions_diff(rev: str, path: str, context: int = 3, base: str = "") -> dict:
        """``git diff`` of one file in one commit (against its first parent,
        or ``base``). ``context`` above the file length shows the whole file."""
        try:
            diff = await asyncio.to_thread(
                lambda: _engine().file_diff(rev, path, context=context, base=base)
            )
        except Exception as exc:
            raise _http(exc) from exc
        if diff is None:
            raise HTTPException(status_code=404, detail=f"no commit {rev}")
        return diff

    @app.post("/api/revisions/snapshot")
    async def revisions_snapshot(body: dict | None = None) -> dict:
        """Explicit snapshot (dashboard button / pre-upgrade hook)."""
        from olano.revisions.worker import get_revisions_worker

        tag = str((body or {}).get("tag") or "")
        reason = str((body or {}).get("reason") or "manual snapshot")
        worker = get_revisions_worker(start=False)
        if worker is not None:
            result = await asyncio.to_thread(lambda: worker.commit_now(reason=reason, tag=tag))
        else:
            from olano.revisions.engine import snapshot_home

            result = await asyncio.to_thread(lambda: snapshot_home(reason=reason, tag=tag))
        if result.error:
            raise HTTPException(status_code=503, detail=result.error)
        return result.to_dict()

    # ── GitHub remote (admin) ─────────────────────────────────────────────

    @app.get("/api/revisions/remote")
    async def revisions_remote_status(fetch: bool = False) -> dict:
        """Connection, sync mode, ahead/behind, last push/pull, any pending
        merge, and ``busy`` (the operation running now, and who started it).
        ``fetch=true`` asks GitHub first ("Check for updates")."""
        from olano.revisions import remote

        by = _actor()
        try:
            return await asyncio.to_thread(lambda: remote.status(fetch=fetch, by=by))
        except Exception as exc:
            raise _http(exc) from exc

    @app.post("/api/revisions/remote/connect")
    async def revisions_remote_connect(body: dict) -> dict:
        from olano.revisions import remote

        by = _actor()
        try:
            result = await asyncio.to_thread(
                lambda: remote.connect(
                    repo=str(body.get("repo") or ""),
                    auth=str(body.get("auth") or "token"),
                    token=str(body.get("token") or ""),
                    branch=str(body.get("branch") or "main"),
                    sync=str(body.get("sync") or "manual"),
                    allow_public=bool(body.get("allow_public")),
                    by=by,
                )
            )
        except Exception as exc:
            raise _http(exc) from exc
        return {"result": result, "remote": await asyncio.to_thread(remote.status)}

    @app.put("/api/revisions/remote")
    async def revisions_remote_update(body: dict) -> dict:
        from olano.revisions import remote

        sync = body.get("sync")
        try:
            await asyncio.to_thread(
                lambda: remote.update_remote(sync=str(sync) if sync is not None else None)
            )
        except Exception as exc:
            raise _http(exc) from exc
        if sync in ("on_commit", "daily"):
            _request_push()
        return await asyncio.to_thread(remote.status)

    @app.delete("/api/revisions/remote")
    async def revisions_remote_disconnect() -> dict:
        from olano.revisions import remote

        by = _actor()
        try:
            await asyncio.to_thread(lambda: remote.disconnect(by))
        except Exception as exc:
            raise _http(exc) from exc
        return await asyncio.to_thread(remote.status)

    @app.post("/api/revisions/remote/push")
    async def revisions_remote_push(body: dict | None = None) -> dict:
        """Push now. ``force`` replaces the remote branch with this box's
        history (the dashboard asks for confirmation first)."""
        from olano.revisions import remote

        force = bool((body or {}).get("force"))
        by = _actor()

        def _push(engine: Any) -> dict:
            from olano.revisions.worker import get_revisions_worker

            # Commit what is pending first, so "Push" means "push what I see".
            worker = get_revisions_worker(start=False)
            if worker is not None:
                worker.commit_now(reason="snapshot before pushing to GitHub")
            else:
                engine.snapshot(reason="snapshot before pushing to GitHub")
            return remote.sync_from_settings(engine).push(force=force)

        try:
            result = await asyncio.to_thread(lambda: remote.run_op("push", _push, by=by))
        except Exception as exc:
            raise _http(exc) from exc
        return {"result": result, "remote": await asyncio.to_thread(remote.status)}

    @app.post("/api/revisions/remote/pull")
    async def revisions_remote_pull() -> dict:
        """Fetch and bring GitHub's changes into the live home. Returns
        ``status: conflicts`` with the files to resolve when a merge needs a
        human; nothing is written to the live home in that case."""
        from olano.revisions import remote

        author = _actor()
        try:
            result = await asyncio.to_thread(
                lambda: remote.run_op(
                    "pull",
                    lambda engine: remote.sync_from_settings(engine).pull(author=author),
                    by=author,
                )
            )
        except Exception as exc:
            raise _http(exc) from exc
        if result.get("status") == "pulled":
            _request_push()
        return {"result": result, "remote": await asyncio.to_thread(remote.status)}

    @app.get("/api/revisions/remote/merge/file")
    async def revisions_merge_file(path: str) -> dict:
        """One conflicted file: base, this box's and GitHub's versions, the
        text with conflict markers to edit, and a diff of the two sides."""
        from olano.revisions import remote

        try:
            return await asyncio.to_thread(lambda: remote.merge_file(path))
        except Exception as exc:
            raise _http(exc) from exc

    @app.post("/api/revisions/remote/merge/resolve")
    async def revisions_merge_resolve(body: dict) -> dict:
        from olano.revisions import remote

        resolutions = body.get("resolutions")
        if not isinstance(resolutions, dict):
            raise HTTPException(status_code=400, detail="resolutions must be an object")
        author = _actor()

        try:
            result = await asyncio.to_thread(
                lambda: remote.run_op(
                    "merge",
                    lambda engine: remote.resolve_merge(resolutions, author=author, engine=engine),
                    by=author,
                )
            )
        except Exception as exc:
            raise _http(exc) from exc
        if result.get("status") == "pulled":
            _request_push()
        return {"result": result, "remote": await asyncio.to_thread(remote.status)}

    @app.post("/api/revisions/remote/merge/abort")
    async def revisions_merge_abort() -> dict:
        """Drop a pending merge. Nothing was applied, so nothing is undone."""
        from olano.revisions import remote

        by = _actor()
        try:
            await asyncio.to_thread(lambda: remote.abort_merge(by))
        except Exception as exc:
            raise _http(exc) from exc
        return await asyncio.to_thread(remote.status)
