"""Conversation threads: list, read, rename, compact, delete.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from olano.core.runs import REGISTRY as _RUNS
from olano.core.thread_labels import CHANNEL_DASHBOARD, derive_label
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer
from olano.runtime import thread_pages, thread_summaries

# Background Langfuse trace deletions started by DELETE …/threads/{id}, held so
# the loop does not drop them mid-flight.
_TRACE_DELETES: set[asyncio.Task[Any]] = set()


def register_threads_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the threads routes to ``app``."""
    # ── Threads (per-agent persisted conversations) ────────────────────────────

    def _agent_sessions_dir(name: str) -> Path:
        from olano.config.home import get_olano_paths

        return get_olano_paths().sessions_dir / name

    def _thread_meta_path(name: str, thread_id: str) -> Path:
        return _agent_sessions_dir(name) / f"{thread_id}.meta.json"

    def _scheduler_task_names(name: str) -> dict[str, str]:
        """Map a scheduler task's short id (``<task8>``) → its human name.

        Covers both task sources: DB tasks (``tasks.name``) and config schedules
        (``ScheduleConfig.name``). Best-effort — a missing scheduler DB just
        means those rows fall back to their prompt text.
        """
        names: dict[str, str] = {}
        try:
            for task in _api.SchedulerDB().list_tasks(agent_name=name):
                label = str(task.get("name") or "").strip()
                if label:
                    names[str(task.get("id") or "")[:8]] = label
        except Exception:
            _api.logger.debug("thread labels: scheduler task names unavailable", exc_info=True)
        return names

    def _thread_states(name: str) -> dict[str, dict]:
        """Every ``<thread>.state.json`` for this agent, parsed once per request.

        The rail already reads one state file per row (``_thread_inherited_cost``),
        so doing the whole directory in a single pass costs the same reads and
        additionally yields the ``active_thread`` links a ``/new`` leaves behind —
        which is what lets a forked platform conversation inherit its parent's
        channel below.
        """
        out: dict[str, dict] = {}
        sessions = _agent_sessions_dir(name)
        if not sessions.exists():
            return out
        for path in sessions.glob("*.state.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                out[path.name[: -len(".state.json")]] = data
        return out

    def _platform_by_thread(name: str, states: dict[str, dict]) -> dict[str, str]:
        """thread id → platform, recovered for transcripts written before the stamp.

        A conversation that arrived on a platform is persisted under the bare
        platform chat id (``runtime/server.py``: ``source.thread_id or
        source.chat_id``), so its file name says nothing about Telegram and
        ``classify_thread`` can only fall back to "dashboard". The chat directory
        (``platforms/chat_directory.py``, written on every inbound message by
        ``record_message_event``) is the one store that knows which platform a
        given chat id belongs to, so the id joins straight onto it.

        This is a **heuristic recovery for history**, not a source of truth — the
        caller must never let it override a channel the transcript itself carries.
        Two known gaps, both covered going forward by the write-time stamp:
        Slack/forum rows persist under a thread ts rather than a chat id and so
        do not join, and a chat id seen under two platforms keeps the first
        (most recently active) row, since ``list_chats`` orders by ``last_seen``.
        """
        out: dict[str, str] = {}
        try:
            from olano.config.home import get_olano_paths

            # Constructing ChatDirectory runs its CREATE TABLE migration, which
            # would leave an empty sqlite file on a box that has never connected
            # a platform. Nothing to recover there anyway — bail on the path.
            if not get_olano_paths().chat_directory_db_path.exists():
                return out
            from olano.platforms.chat_directory import get_chat_directory

            # include_inactive: a chat the bot was removed from still has rail
            # history. The default limit is 50 — far below a busy agent's chat
            # count, and a silently truncated map reads as "the fix didn't work".
            rows = get_chat_directory().list_chats(name, limit=5000, include_inactive=True)
        except Exception:
            _api.logger.debug("chat-directory channel backfill skipped", exc_info=True)
            return out
        for row in rows:
            chat_id = str(row.get("chat_id") or "")
            platform = str(row.get("platform") or "")
            if chat_id and platform:
                out.setdefault(chat_id, platform)
        # A `/new` on a platform swaps the streaming thread to a fresh uuid4 and
        # records it as `active_thread` on the parent's state file
        # (platforms/inbound.py). The child has no chat id of its own, so without
        # this it falls back to "dashboard" the moment a user starts a second
        # conversation in the same Telegram chat.
        for base, state in states.items():
            child = str(state.get("active_thread") or "")
            if child and child != base and base in out:
                out.setdefault(child, out[base])
        return out

    def _channel_hint(name: str, thread_id: str) -> str:
        """The same recovery as :func:`_platform_by_thread`, for ONE thread.

        The rail resolves the whole map once per request because it labels every
        row; opening a single conversation must not pay for that. Tries the
        direct chat-id join first and only walks the state files when it misses,
        so a stamped thread — and a box with no chat directory at all — costs
        nothing.
        """
        try:
            from olano.config.home import get_olano_paths

            if not get_olano_paths().chat_directory_db_path.exists():
                return ""
            from olano.platforms.chat_directory import get_chat_directory

            directory = get_chat_directory()
            # `find_chats` ranks an exact `chat_id` match first and keeps it even
            # when the chat has gone inactive.
            for row in directory.find_chats(name, thread_id, limit=1):
                if str(row.get("chat_id") or "") == thread_id:
                    return str(row.get("platform") or "")
            # Not a chat id of its own — it may be the uuid a `/new` forked off
            # a platform conversation, which lives on the PARENT's state file.
            for base, state in _thread_states(name).items():
                if str(state.get("active_thread") or "") == thread_id:
                    for row in directory.find_chats(name, base, limit=1):
                        if str(row.get("chat_id") or "") == base:
                            return str(row.get("platform") or "")
                    break
        except Exception:
            _api.logger.debug("chat-directory channel lookup skipped", exc_info=True)
        return ""

    def _thread_inherited_cost(name: str, thread_id: str, state: dict | None = None) -> dict:
        """Carried-forward token/cost baseline for an auto-compaction successor.

        Auto-compaction forks a successor whose checkpoint has no usage metadata, so
        mining it yields $0. The runner records the predecessor lineage's accumulated
        cost in ``<thread_id>.state.json`` (``inherited_*`` keys). The rail/header add
        this on top of the thread's own mined cost; Insights never reads it, so the
        conversation isn't double-counted. Returns zeros when absent.
        """
        if state is None:
            path = _agent_sessions_dir(name) / f"{thread_id}.state.json"
            if not path.exists():
                return {}
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {
            "usd": float(state.get("inherited_usd") or 0.0),
            "total_tokens": int(state.get("inherited_total_tokens") or 0),
            "input_tokens": int(state.get("inherited_input_tokens") or 0),
            "output_tokens": int(state.get("inherited_output_tokens") or 0),
            # Not inherited: the thread's own accumulated Olano-gateway charge
            # (runner._bump_thread_credits). Riding this helper because it is
            # the single state-file read both thread endpoints already do —
            # credits are minable from no other store (no thread key in the
            # ledger, no charge in checkpoints).
            "credits": float(state.get("credits_total") or 0.0),
        }

    def _last_model(entries: list[dict]) -> dict:
        """``{model, reasoning_effort}`` of the newest assistant row that
        carries a model stamp (``runner._model_stamp``) — the model the
        conversation last ran on, which is what a reopened chat continues on.
        Empty for a thread recorded before the stamp existed, or one that has
        not answered yet; the chat then opens on the agent's default.
        """
        for e in reversed(entries):
            if e.get("role") == "assistant" and e.get("model"):
                out = {"model": str(e["model"])}
                if e.get("reasoning_effort"):
                    out["reasoning_effort"] = str(e["reasoning_effort"])
                return out
        return {}

    # Reading a transcript lives in `thread_summaries` so the boot warm reads
    # one the way the list does; these names stay for the handlers below.
    _read_text = thread_summaries.read_transcript_text
    _parse_jsonl = thread_summaries.parse_jsonl

    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            text = _read_text(path)
        except OSError:
            if path.suffix == ".gz":
                return []
            raise
        return _parse_jsonl(text)

    def _resolve_session_file(name: str, thread_id: str) -> Path | None:
        """Return the session file for a thread, preferring plain over gzipped."""
        sessions = _agent_sessions_dir(name)
        plain = sessions / f"{thread_id}.jsonl"
        if plain.exists():
            return plain
        gz = sessions / f"{thread_id}.jsonl.gz"
        return gz if gz.exists() else None

    _summarize_transcript = thread_summaries.summarize_transcript
    _session_files = thread_summaries.transcripts_in

    def _files_newest_first(sessions: Path) -> list[tuple[int, str, Path]]:
        """``(mtime_ns, thread_id, path)`` for every transcript, newest first.

        One stat per file and no read: this is what lets a page cost its own
        N transcripts rather than the agent's whole history.
        """
        out: list[tuple[int, str, Path]] = []
        for thread_id, path in _session_files(sessions).items():
            try:
                out.append((path.stat().st_mtime_ns, thread_id, path))
            except OSError:
                continue
        out.sort(reverse=True)
        return out

    def _thread_row(
        name: str,
        thread_id: str,
        jsonl: Path,
        summary: thread_summaries.TranscriptSummary,
        task_names: dict[str, str],
        platform_by_thread: dict[str, str],
    ) -> dict:
        preview = (summary.preview_source or {}).get("content", "")
        preview = (preview[:120] + "…") if len(preview) > 120 else preview
        # Readable label: strips the machine directives that producers
        # concatenate onto the message before it is persisted (sender tags,
        # the autonomous-task preamble, [think:…], media notes …) and names
        # a scheduler run by its task instead of its prompt. `preview` stays
        # on the wire untouched as the back-compat fallback.
        label = derive_label(
            thread_id,
            summary.head,
            task_names=task_names,
            channel_hint=platform_by_thread.get(thread_id, ""),
        )
        stat = jsonl.stat()

        # The meta file sits beside the transcript: resolving the home per row
        # (`_thread_meta_path`) cost a config read and ten mkdirs each time.
        meta_path = jsonl.parent / f"{thread_id}.meta.json"
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

        return {
            "id": thread_id,
            "name": meta.get("name", ""),
            "preview": preview,
            "title": label.title,
            "origin": label.origin,
            "channel": label.channel,
            "sender": label.sender,
            "message_count": summary.message_count,
            "created_at": summary.created_at,
            "updated_at": summary.last_ts
            or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            # The model the thread last answered on ("" when unknown),
            # so the rail can say it without opening the thread.
            "model": summary.model,
        }

    def _finish_rows(
        name: str,
        result: list[dict],
        thread_states: dict[str, dict],
        *,
        states_complete: bool = False,
    ) -> list[dict]:
        """The per-list work after the rows are built: the live dot, the
        caller's visibility, cached cost, newest first.

        ``states_complete`` says ``thread_states`` holds every state file of
        the directory, so a thread missing from it has none and the cost
        lookup never touches the disk for it.
        """
        # Which of these the agent is working on right now (`olano/core/runs.py`),
        # so the rail can show a live dot on a thread this tab never started —
        # a turn left running on a phone, or one another device kicked off.
        live_threads = {
            r["thread_id"]
            for r in _RUNS.snapshot().values()
            if r["kind"] == "chat" and r["agent"] == name and r["running"]
        }
        # Everyone who may chat with the agent sees every thread it has had; the
        # filter drops the list for an anonymous caller and keeps a demo
        # visitor to its own. Resolved ONCE: asking per row re-read the home
        # config for each of thousands of rows and was two thirds of a warm
        # list's time. It runs before the live dot because resolving a thread
        # a demo visitor does not own is refused.
        prefix = auth_layer.thread_visibility_prefix()
        result = [t for t in result if auth_layer.thread_id_visible(t["id"], prefix)]
        for t in result:
            t["running"] = auth_layer.namespace_thread_id(t["id"]) in live_threads
        # Attach cached per-thread token/cost (cache-only — never mines inline, so the
        # rail stays fast; threads not yet indexed simply omit these → UI shows "—").
        try:
            from olano.analytics import threadcost
            from olano.core.checkpoints import scope_thread

            # threadcost keys its cache by the agent-scoped checkpoint id; the chat
            # rail carries the human-facing stem, so scope on the way in and back.
            costs = threadcost.thread_cost_map([scope_thread(name, t["id"]) for t in result])
            for t in result:
                c = costs.get(scope_thread(name, t["id"])) or {}
                # Own mined cost (cache) + carried-forward baseline from a prior
                # compaction lineage. A compaction successor's own cost is ~0 until it
                # runs new turns, so the inherited baseline is what makes it show a price.
                state = thread_states.get(t["id"])
                inh = (
                    {}
                    if state is None and states_complete
                    else _thread_inherited_cost(name, t["id"], state)
                )
                if c or inh:
                    t["usd"] = (c.get("usd") or 0.0) + inh.get("usd", 0.0)
                    t["total_tokens"] = (c.get("total_tokens") or 0) + inh.get("total_tokens", 0)
                    t["input_tokens"] = (c.get("input_tokens") or 0) + inh.get("input_tokens", 0)
                    t["output_tokens"] = (c.get("output_tokens") or 0) + inh.get("output_tokens", 0)
                    if inh.get("credits"):
                        t["credits"] = inh["credits"]
        except Exception:
            _api.logger.debug("threadcost: thread-list enrichment skipped", exc_info=True)
        # Most recent first.
        result.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
        return result

    @app.get("/api/agents/{name}/threads")
    async def list_threads(
        name: str,
        limit: int | None = Query(None, ge=1, le=200),
        before: str | None = Query(None, pattern=r"^\d{1,20}$"),
        kind: str | None = Query(None, pattern=f"^{thread_pages.KIND_CONVERSATION}$"),
    ) -> Any:
        """The agent's threads.

        Without parameters: every thread, as a list (the dashboard's read).
        With any of ``limit`` / ``before`` / ``kind``: one page, newest first,
        as ``{threads, has_more, next_before}``; ``before`` is the cursor the
        previous page returned and ``kind=conversation`` keeps only the
        conversations a person had (what tapping an agent opens). A page reads
        only its own transcripts, so it costs the same on an agent with ten
        threads and one with ten thousand.
        """
        auth_layer.assert_agent_access(name)
        # Reads session JSONL + scheduler DB + threadcost sqlite. The
        # per-member visibility filter rides along: contextvars carry the
        # request identity into the worker thread.
        if limit is None and before is None and kind is None:
            return await asyncio.to_thread(_list_threads_sync, name)
        return await asyncio.to_thread(
            _thread_page_sync, name, limit or thread_pages.DEFAULT_PAGE, before, kind
        )

    def _list_threads_sync(name: str) -> list:
        sessions = _agent_sessions_dir(name)
        if not sessions.exists():
            return []

        result: list[dict] = []
        # Scheduler task names, resolved ONCE per request (not per row). A
        # `scheduled_<agent>_<task8>@<stamp>` thread predates the structured
        # `task_name` transcript field, so this is what lets already-recorded
        # runs show "Daily tender sweep" instead of the shared autonomous
        # preamble every one of them starts with.
        task_names = _scheduler_task_names(name)
        # Provenance for transcripts written before the channel stamp existed:
        # one directory pass for the state files (which the per-row cost lookup
        # below reuses instead of re-reading) and one sqlite query, both resolved
        # ONCE per request rather than per row.
        thread_states = _thread_states(name)
        platform_by_thread = _platform_by_thread(name, thread_states)
        # When a thread is auto-compacted (input tokens >= 80k), the runner forks
        # a fresh successor thread seeded with a summary and a short recent tail.
        # The successor records its origin as "Prior thread: <old_id>" in its seed
        # entry. Without filtering, both the frozen predecessor and the live
        # successor surface in the panel as a sudden "duplicate" mid-conversation.
        # Collect predecessors so we can hide them and show only the live thread.
        superseded: set[str] = set()
        by_thread = _session_files(sessions)
        for thread_id, jsonl in by_thread.items():
            # Only a transcript that changed since the last list is read again
            # (`thread_summaries`): reading every one in full on each call made
            # the most-used agent the slowest to open.
            summary = thread_summaries.cached_summary(jsonl, _summarize_transcript)
            if summary.empty:
                continue
            if summary.supersedes:
                superseded.add(summary.supersedes)
            result.append(
                _thread_row(name, thread_id, jsonl, summary, task_names, platform_by_thread)
            )
        thread_summaries.forget_missing(sessions, by_thread.values())
        # Hide compaction predecessors so a forked thread doesn't appear twice.
        if superseded:
            result = [t for t in result if t["id"] not in superseded]
        return _finish_rows(name, result, thread_states, states_complete=True)

    def _thread_page_sync(name: str, limit: int, before: str | None, kind: str | None) -> dict:
        """One page of the newest threads (``thread_pages``): reads only the
        transcripts it lists, plus the ones a filter skipped on the way."""
        sessions = _agent_sessions_dir(name)
        if not sessions.exists():
            return {"threads": [], "has_more": False, "next_before": None}
        files = _files_newest_first(sessions)
        cutoff = thread_pages.cursor_ns(before)
        task_names = _scheduler_task_names(name)
        # No whole-directory state pass here: the page's own rows read theirs.
        platform_by_thread = _platform_by_thread(name, {})
        rows: list[dict] = []
        superseded: set[str] = set()
        last_ns: int | None = None
        # Files looked at, so a page that stopped short of the end knows it.
        consumed = 0
        for mtime_ns, thread_id, jsonl in files:
            if cutoff is not None and mtime_ns >= cutoff:
                consumed += 1
                continue
            if len(rows) >= limit:
                break
            consumed += 1
            last_ns = mtime_ns
            summary = thread_summaries.cached_summary(jsonl, _summarize_transcript)
            if summary.empty:
                continue
            if summary.supersedes:
                superseded.add(summary.supersedes)
            # A compaction successor is newer than its predecessor, so the
            # predecessor is always reached after the row that hides it.
            if thread_id in superseded:
                continue
            row = _thread_row(name, thread_id, jsonl, summary, task_names, platform_by_thread)
            if kind == thread_pages.KIND_CONVERSATION and not thread_pages.is_conversation(
                row["origin"], row["channel"]
            ):
                continue
            rows.append(row)
        has_more = consumed < len(files)
        return {
            "threads": _finish_rows(name, rows, {}),
            "has_more": has_more,
            "next_before": thread_pages.cursor_of(last_ns) if has_more and last_ns else None,
        }

    @app.get("/api/agents/{name}/threads/search")
    async def search_threads(
        name: str,
        q: str = Query(..., min_length=2, max_length=200),
        limit: int = Query(thread_pages.DEFAULT_PAGE, ge=1, le=100),
        before: str | None = Query(None, pattern=r"^\d{1,20}$"),
    ) -> dict:
        """Threads whose messages hold ``q`` (case-insensitive), newest first.

        Each row carries ``match`` (the role and a snippet around the first
        hit). One request reads at most ``SEARCH_MAX_SCAN`` transcripts; when
        more remain, ``has_more`` is set and ``before`` continues from there.
        """
        auth_layer.assert_agent_access(name)
        return await asyncio.to_thread(_search_threads_sync, name, q, limit, before)

    def _search_threads_sync(name: str, q: str, limit: int, before: str | None) -> dict:
        sessions = _agent_sessions_dir(name)
        if not sessions.exists():
            return {"threads": [], "has_more": False, "next_before": None, "scanned": 0}
        files = _files_newest_first(sessions)
        cutoff = thread_pages.cursor_ns(before)
        task_names = _scheduler_task_names(name)
        platform_by_thread = _platform_by_thread(name, {})
        needle = q.lower()
        prefilter = thread_pages.text_prefilter_applies(q)
        rows: list[dict] = []
        scanned = 0
        last_ns: int | None = None
        exhausted = True
        for mtime_ns, thread_id, jsonl in files:
            if cutoff is not None and mtime_ns >= cutoff:
                continue
            if scanned >= thread_pages.SEARCH_MAX_SCAN or len(rows) >= limit:
                exhausted = False
                break
            scanned += 1
            last_ns = mtime_ns
            try:
                text = _read_text(jsonl)
            except OSError:
                continue
            # The raw JSON text first, so a transcript without the word is
            # never parsed; a needle json.dumps would escape goes straight to
            # the rows.
            if prefilter and needle not in text.lower():
                continue
            entries = _parse_jsonl(text)
            hit = thread_pages.find_match(entries, q)
            if hit is None:
                continue
            summary = thread_summaries.cached_summary(jsonl, _summarize_transcript)
            if summary.empty:
                continue
            row = _thread_row(name, thread_id, jsonl, summary, task_names, platform_by_thread)
            row["match"] = hit
            rows.append(row)
        return {
            "threads": _finish_rows(name, rows, {}),
            "has_more": not exhausted,
            "next_before": thread_pages.cursor_of(last_ns) if not exhausted and last_ns else None,
            "scanned": scanned,
        }

    @app.get("/api/agents/{name}/threads/{thread_id}")
    async def get_thread(
        name: str,
        thread_id: str,
        limit: int | None = Query(None, ge=1, le=1000),
        before: int | None = Query(None, ge=0),
        tool_results: str = Query(
            thread_pages.TOOL_RESULTS_FULL,
            pattern=f"^({thread_pages.TOOL_RESULTS_FULL}|{thread_pages.TOOL_RESULTS_STATUS})$",
        ),
    ) -> dict:
        """One conversation: the whole transcript, or a page of it.

        ``limit`` returns the newest ``limit`` messages (or those ``before`` a
        position) rounded out to whole turns, with ``start`` (the first one's
        position, the next ``before``) and ``has_more``. ``tool_results=status``
        leaves each tool step's output out (``thread_pages``). Without them the
        response is the whole transcript, as it always was.
        """
        auth_layer.assert_agent_access(name)
        if not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        path = _resolve_session_file(name, thread_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        # JSONL read + media scan + the single-thread checkpoint mine are all
        # disk-bound (same offload as /analytics/threadcost/threads/{id}/calls).
        out = await asyncio.to_thread(
            _get_thread_sync, name, thread_id, path, limit, before, tool_results
        )
        # The caller's own thumbs on each reply (core/feedback.py), so the
        # vote reads back as cast after a reload. Nobody sees anyone else's.
        user = auth_layer.current_user()
        if user is not None:
            from olano.core.feedback import votes_for

            votes = await asyncio.to_thread(votes_for, name, thread_id, user.id)
            for m in out.get("messages") or []:
                vote = votes.get(m.get("turn_id") or "")
                if vote is not None:
                    m["feedback"] = {"value": vote.value, "comment": vote.comment}
        # A turn may be running on this thread right now — started by this tab
        # before it reloaded, by another device, or by a phone that has since
        # locked. The transcript alone cannot say so (it holds only what has
        # been persisted), so advertise the live run and its cursor: opening the
        # thread then attaches instead of showing a conversation that looks
        # finished and silently is not. `null` when nothing is running.
        live = _RUNS.for_thread("chat", name, auth_layer.namespace_thread_id(thread_id))
        out["run"] = live.snapshot() if live is not None else None
        return out

    def _get_thread_sync(
        name: str,
        thread_id: str,
        path: Path,
        limit: int | None = None,
        before: int | None = None,
        tool_results: str = thread_pages.TOOL_RESULTS_FULL,
    ) -> dict:
        # Hidden prompt rows are dropped before the label derivation too, so a
        # greeting thread is never titled by the instruction that produced it.
        entries = [e for e in _read_jsonl(path) if not e.get("hidden")]
        # One projection for a reload and for the live `thread_updated` frame
        # (`_transcript_message`): chat-visible rows only, sender tags peeled,
        # inline media re-derived. Anything the reload shows, the live tab
        # shows, and nothing else.
        try:
            media_root: Path | None = _api._workspace_root(name)
        except Exception:
            media_root = None
        visible = [e for e in entries if _api._chat_visible(e)]
        # A paged read projects only its page: the projection scans each row
        # for media, which is most of what a long conversation costs to open.
        start, end = (
            thread_pages.page_bounds(visible, limit, before)
            if limit is not None
            else (0, len(visible))
        )
        messages = [
            m
            for m in (
                _api._transcript_message(name, e, media_root=media_root) for e in visible[start:end]
            )
            if m is not None
        ]
        if tool_results == thread_pages.TOOL_RESULTS_STATUS:
            messages = [thread_pages.omit_tool_output(m) for m in messages]
        label = derive_label(thread_id, entries)
        if label.channel == CHANNEL_DASHBOARD:
            # Only pay for the recovery when the transcript itself has no answer;
            # a stamped thread (every new one) never reaches this.
            hint = _channel_hint(name, thread_id)
            if hint:
                label = derive_label(thread_id, entries, channel_hint=hint)
        out: dict = {
            "id": thread_id,
            "messages": messages,
            # The thread's own surface, so the chat header can badge it without
            # waiting on (or disagreeing with) the rail's separate request.
            "origin": label.origin,
            "channel": label.channel,
            "sender": label.sender,
            # The model (and effort) the conversation last ran on, so opening
            # it continues on that model rather than whatever the picker last
            # said for some other chat. Absent keys mean "unknown": a thread
            # recorded before the stamp existed opens on the agent's default.
            **_last_model(entries),
        }
        if limit is not None:
            out["start"] = start
            out["has_more"] = start > 0
        # Attach the cumulative token/cost so the chat header can restore the
        # running tally when an old thread is reopened (it keeps accumulating from
        # here as new turns stream). Sourced from the durable threads.db checkpoint
        # store (not the rail's cache), so it's available even for a thread that the
        # threadcost cache hasn't indexed yet — a single-thread mine is cheap enough
        # for this on-demand open (same path the cost drill-down uses).
        try:
            from olano.analytics import threadcost
            from olano.core.checkpoints import scope_thread

            # Scope the human-facing stem to this agent's checkpoint key before mining.
            totals = threadcost.thread_calls(scope_thread(name, thread_id)).get("totals") or {}
            # Add any carried-forward baseline from a prior compaction lineage so the
            # header matches the rail for a compacted successor (whose own mined cost
            # is ~0 until it runs new turns).
            inh = _thread_inherited_cost(name, thread_id)
            out["usd"] = (totals.get("usd") or 0.0) + inh.get("usd", 0.0)
            out["total_tokens"] = (totals.get("total_tokens") or 0) + inh.get("total_tokens", 0)
            out["input_tokens"] = (totals.get("input_tokens") or 0) + inh.get("input_tokens", 0)
            out["output_tokens"] = (totals.get("output_tokens") or 0) + inh.get("output_tokens", 0)
            # Model calls (mined per sub-call) so the chat header can restore
            # the running count, alongside the tool calls this thread's own
            # transcript already recorded (MCP tools included — the transcript
            # stores whatever name the model invoked).
            out["calls"] = int(totals.get("calls") or 0)
            out["tool_calls"] = sum(1 for e in entries if e.get("role") == "tool_call")
            if inh.get("credits"):
                out["credits"] = inh["credits"]
        except Exception:
            _api.logger.debug("threadcost: thread-detail enrichment skipped", exc_info=True)
        return out

    @app.patch("/api/agents/{name}/threads/{thread_id}")
    async def rename_thread(name: str, thread_id: str, body: dict) -> dict:
        auth_layer.assert_agent_access(name)
        if not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        sessions = _agent_sessions_dir(name)
        jsonl = sessions / f"{thread_id}.jsonl"
        if not jsonl.exists():
            raise HTTPException(status_code=404, detail="Thread not found")
        sessions.mkdir(parents=True, exist_ok=True)
        meta_path = _thread_meta_path(name, thread_id)
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        new_name = (body or {}).get("name", "")
        meta["name"] = str(new_name)[:200]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return {"ok": True}

    @app.post("/api/agents/{name}/threads/{thread_id}/compact")
    async def compact_thread(name: str, thread_id: str) -> dict:
        """Compact a thread on demand (the `/compact` slash command).

        Asks the agent to invoke deepagents' ``compact_conversation`` tool, which
        summarizes older messages, offloads the full history to
        ``conversation_history/<thread>.md``, and keeps the recent tail (including
        tool results). Returns ``compacted=False`` when there was nothing to
        compact (e.g. the conversation is still within the token budget).
        """
        auth_layer.assert_agent_access(name)
        if not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        runner = runtime._runners.get(name)
        if not runner:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        compacted = await runner.compact_thread(thread_id)
        out: dict[str, Any] = {"ok": True, "compacted": compacted}
        # The chat header's context bar is `usage.context` from the last
        # *streamed* turn, and this endpoint streams nothing — so after a
        # compaction the bar kept showing the pre-compaction fill until another
        # turn ran, which read as "/compact did nothing". Report the estimated
        # post-compaction fill (static footprint + the effective message list)
        # so the client can seed the bar with it.
        try:
            from olano.runtime.context_report import build_context_report

            messages_tokens = await runner.estimate_thread_context_tokens(thread_id)
            if messages_tokens is not None:
                report = await asyncio.to_thread(build_context_report, name)
                static_tokens = sum(
                    int(c.get("tokens", 0) or 0) for c in report.get("categories") or []
                )
                out["messages_tokens"] = messages_tokens
                out["context_tokens"] = static_tokens + messages_tokens
        except Exception as exc:  # the estimate is a nicety, never a failure
            _api.logger.debug("post-compaction context estimate for %s failed: %s", name, exc)
        return out

    @app.delete("/api/agents/{name}/threads/{thread_id}")
    async def delete_thread(name: str, thread_id: str) -> dict:
        # Every thread of an agent is visible to everyone who may chat with it,
        # so deleting one erases a conversation — a scheduled run's record, a
        # customer's Telegram chat, a colleague's thread — plus its checkpoint,
        # for all of them. That stays with the people who administer the agent
        # (admins and its owner); renaming and compacting remain open to anyone
        # with access.
        auth_layer.assert_agent_admin(name)
        if not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        sessions = _agent_sessions_dir(name)
        # Read before the transcript goes: the Langfuse traces of this
        # thread's traced turns are deleted too (best-effort, background).
        from olano.observability.deletion import trace_ids_in_transcript

        trace_ids = await asyncio.to_thread(
            trace_ids_in_transcript, sessions / f"{thread_id}.jsonl"
        )
        from olano.core.feedback import FEEDBACK_SUFFIX

        removed = False
        for p in (
            sessions / f"{thread_id}.jsonl",
            sessions / f"{thread_id}.jsonl.gz",
            sessions / f"{thread_id}.state.json",
            _thread_meta_path(name, thread_id),
        ):
            if p.exists():
                p.unlink()
                removed = True
        # Its thumbs go with it (core/feedback.py). Not counted as "the thread
        # existed": votes alone are not a conversation.
        feedback_file = sessions / f"{thread_id}{FEEDBACK_SUFFIX}"
        if feedback_file.exists():
            feedback_file.unlink()
        # Also purge the LangGraph checkpoint from the shared threads.db. The JSONL
        # and the checkpoint are separate stores; deleting only the files left an
        # orphaned checkpoint that kept the conversation (and its token cost) alive
        # in threads.db and counted by threadcost/Insights.
        try:
            from olano.core.checkpoints import adelete_thread as _adelete_checkpoint

            await _adelete_checkpoint(name, thread_id)
        except Exception:
            _api.logger.debug("checkpoint purge skipped for %s/%s", name, thread_id, exc_info=True)
        if not removed:
            raise HTTPException(status_code=404, detail="Thread not found")
        if trace_ids:
            from olano.observability.deletion import delete_traces

            task = asyncio.get_running_loop().create_task(
                asyncio.to_thread(delete_traces, name, trace_ids)
            )
            _TRACE_DELETES.add(task)
            task.add_done_callback(_TRACE_DELETES.discard)
        return {"ok": True}
