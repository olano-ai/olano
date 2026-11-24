"""Shared inbound handling for messaging platforms and classic channels.

Every platform adapter (Telegram, WhatsApp, Discord, Slack, WeCom, DingTalk,
Feishu, WeChat, QQ bot, …) and every classic channel funnels inbound messages
through a single handler in :mod:`olano.runtime.server`. Historically that
handler streamed *everything* — including ``/help``, ``/model`` and friends —
straight to the LLM, so slash commands silently did nothing on chat platforms.

This module is the one place that turns chat text into a command dispatch:

- :func:`command_name` recognises a real ``/command`` (and rejects file paths
  like ``/etc/hosts`` so they still reach the agent as plain text).
- :func:`maybe_handle_command` decides what a ``/command`` does based on the
  agent's :class:`~olano.config.agent_config.CommandsConfig`:

  * a **custom** command runs its configured prompt through the agent;
  * ``/start`` (Telegram's Start button, WeCom subscribe) runs a greeting
    prompt through the agent — unless a custom ``/start`` overrides it;
  * a **disabled** built-in is refused (never leaked to the model);
  * an **exposed** built-in is dispatched through the shared
    :class:`~olano.cli.commands.CommandDispatcher`, and any per-chat setting
    it changed (verbosity, thinking level, the thread forked by ``/new`` /
    ``/mode``) is persisted.

- :func:`get_session_settings` resolves the per-chat verbosity / think level /
  active thread for an ordinary (non-command) turn.

Per-chat state lives in the runner's thread-state JSON, keyed by the platform's
*base* thread id (the deterministic chat-derived id). Storing it on the base id
keeps verbosity stable across ``/new`` (which swaps the active thread).

Verbosity resolves in two layers: the chat's own ``/verbosity`` choice if it
ever made one, otherwise the agent's ``default_verbosity`` (agent.json5). A
chat that ran a verbosity command is **pinned** — the command layer flags the
choice via ``CommandContext.verbosity_explicit`` so it is persisted even when
it happens to equal the agent default, and a later change to that default
therefore never overrides a user who asked for something specific.

Command exposure is governed **only** by the per-agent config — there is no
per-user gate here; a platform's ``allowed_users`` already decides *who* may
message the bot, and the command config decides *which* commands they get.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olano.platforms.verbosity import (
    DEFAULT_VERBOSITY,
    normalize_verbosity,
)

logger = logging.getLogger(__name__)

# --- Inbound media limits -------------------------------------------------
# Extracted-document text injected into the prompt (mirrors the dashboard's
# 60k-char attachment inline cap in runtime/api.py:_build_multimodal_content).
DOC_INJECT_MAX_CHARS = 60_000
# Binary document formats we extract text from. Plain-text formats (.txt/.md/
# .csv/…) are already inlined by the platform adapters themselves.
_DOC_EXTRACT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm"}
# Skip audio-track transcription for videos larger than this (long videos =
# slow ffmpeg + expensive STT); the saved-path note still tells the agent.
VIDEO_TRANSCRIBE_MAX_BYTES = 64 * 1024 * 1024

# Thread-state keys (namespaced so they don't collide with other state such as
# ``session_shared_paths``).
VERBOSITY_KEY = "chat_verbosity"
THINK_KEY = "chat_think_level"
ACTIVE_THREAD_KEY = "active_thread"

# The conversational entry point on messaging platforms. Telegram sends
# ``/start`` automatically when a user taps the **Start** button on a bot
# (optionally with a deep-link payload: ``t.me/bot?start=ref123`` arrives as
# ``/start ref123``), and the WeCom adapter synthesizes it for subscribe
# events. It is deliberately **not** a catalog built-in: the welcome should
# come from the agent itself (in its persona and the user's language), and a
# catalog entry would make an owner-defined custom ``/start`` invalid
# (``CustomCommand`` rejects names that collide with built-ins). A custom
# ``/start`` therefore overrides this default greeting.
START_GREETING_PROMPT = (
    "[The user just opened this chat — their messaging app sent its /start "
    "command. Greet them briefly in your persona and their language, say "
    "what you can help with, and invite their first message. Do not mention "
    "the /start command itself.]"
)


@dataclass
class CommandOutcome:
    """Result of inspecting an inbound message for a slash command.

    - ``handled=False`` — not a command; stream the original text to the agent.
    - ``handled=True`` + ``run_prompt`` set — a custom command; stream
      ``run_prompt`` to the agent (as an ordinary turn).
    - ``handled=True`` + ``run_prompt is None`` — a built-in (or a refusal);
      send ``reply`` back verbatim, do not call the model.
    """

    handled: bool
    reply: str = ""
    run_prompt: str | None = None


@dataclass(frozen=True)
class SenderInfo:
    """Identity + trust of the chat user who sent an inbound message.

    Built by the runtime handlers (``runtime/server.py``) from the channel's
    ``allowed_users`` config and threaded into :class:`CommandContext` so
    owner-gated commands (``/platform``) can authorize per sender.

    ``is_trusted`` is True only when the sender matched an entry in the
    channel's ``allowed_users`` allowlist. ``allowlist_configured`` records
    whether that allowlist exists at all — an open bot (no allowlist) has
    ``is_trusted=False, allowlist_configured=False`` so gated commands can
    refuse with setup guidance rather than treating everyone as owner.
    """

    user_id: str = ""
    username: str = ""
    display_name: str = ""
    chat_id: str = ""
    channel: str = ""
    is_trusted: bool = False
    allowlist_configured: bool = False


def command_name(text: str) -> str | None:
    """Return the bare command name for a ``/command`` message, else ``None``.

    Mirrors :meth:`MessageEvent.get_command`: strips a ``@botname`` suffix and
    rejects anything containing ``/`` after the leading slash (file paths, URLs)
    so those continue on to the agent as ordinary text.
    """
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    first = stripped.split(maxsplit=1)[0][1:]
    if "@" in first:
        first = first.split("@", 1)[0]
    if not first or "/" in first:
        return None
    return first.lower()


def _command_args(text: str) -> str:
    """Everything the user typed after the command word."""
    parts = text.strip().split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _build_custom_prompt(prompt: str, args: str) -> str:
    """Expand a custom command's prompt with the user's extra text.

    ``{args}`` is substituted if present; otherwise the extra text is appended.
    """
    args = args.strip()
    if "{args}" in prompt:
        return prompt.replace("{args}", args)
    return f"{prompt}\n\n{args}" if args else prompt


def agent_default_verbosity(config: Any) -> str:
    """The verbosity a chat of this agent starts on.

    Owners set ``default_verbosity`` in agent.json5 (a support bot on a busy
    group chat wants ``quiet``; a dev agent you are debugging wants
    ``verbose``). Anything unset, unrecognized or unreadable — including a
    ``config`` that is not an AgentConfig at all, which the test doubles and
    the ACP surface both pass — falls back to the platform default.
    """
    return normalize_verbosity(getattr(config, "default_verbosity", None)) or DEFAULT_VERBOSITY


def get_session_settings(runner: Any, base_thread_id: str) -> dict[str, str]:
    """Resolve persisted per-chat verbosity / think level / active thread.

    Verbosity resolves **per chat first, agent default second**: a chat that
    ran ``/verbosity`` keeps its own choice forever, and only chats that never
    chose follow the agent's ``default_verbosity``. That ordering is what makes
    the config field safe to change on a live agent — it moves the chats that
    never expressed a preference and silently leaves the rest alone.
    """
    state: dict[str, Any] = {}
    try:
        state = runner.load_thread_state(base_thread_id) or {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("load_thread_state(%s) failed: %s", base_thread_id, exc)
        state = {}
    verbosity = normalize_verbosity(state.get(VERBOSITY_KEY)) or agent_default_verbosity(
        getattr(runner, "config", None)
    )
    think = str(state.get(THINK_KEY) or "off")
    active = str(state.get(ACTIVE_THREAD_KEY) or base_thread_id)
    return {"verbosity": verbosity, "think_level": think, "active_thread": active}


def _result_reply(result: Any, *, agent_name: str | None = None) -> str:
    """Translate a :class:`CommandResult` into a chat-friendly reply string."""
    from olano.platforms.media_paths import defang_local_file_refs

    # A command reply is a listing, never a file delivery — but it may echo
    # content that names real files (/memory prints MEMORY.md, which can
    # mention an absolute path to a .md/.pdf), and the adapter's implicit
    # attach path would upload every such file. Backtick-wrap those references
    # so they stay visible as text instead of becoming attachments.
    return defang_local_file_refs((result.output or "").strip(), agent_name=agent_name)


async def maybe_handle_command(
    text: str,
    *,
    runner: Any,
    config: Any,
    base_thread_id: str,
    sender: SenderInfo | None = None,
) -> CommandOutcome:
    """Resolve *text* as a slash command per the agent's command config.

    ``sender`` carries the chat user's identity/trust for owner-gated
    commands; ``None`` means a surface with no per-sender identity.

    See :class:`CommandOutcome` for the three possible results.
    """
    name = command_name(text)
    if name is None:
        return CommandOutcome(handled=False)

    from olano.config.commands_catalog import COMMAND_BY_NAME, canonical_command

    cmds = getattr(config, "commands", None)

    # 1) Custom command → run its prompt through the agent.
    custom = cmds.find_custom(name) if cmds is not None else None
    if custom is not None:
        prompt = _build_custom_prompt(custom.prompt, _command_args(text))
        return CommandOutcome(handled=True, run_prompt=prompt)

    # 2) /start — the platform entry point (Telegram's Start button, WeCom
    # subscribe). Greet through the agent so the welcome matches its persona
    # and language; it must never fall through to the dispatcher, which would
    # answer a user's very first contact with "Unknown command: /start".
    # Returning a ``run_prompt`` also lets the runtime's monetization layer
    # run its first-contact gate (trial start + welcome) before the greeting.
    if name == "start":
        segments = [START_GREETING_PROMPT]
        who = (sender.display_name or sender.username) if sender is not None else ""
        if who:
            segments.append(f'[The user\'s name is "{who}".]')
        payload = _command_args(text)
        if payload:
            segments.append(
                f'[The chat was opened via a deep link with start parameter: "{payload}".]'
            )
        return CommandOutcome(handled=True, run_prompt="\n".join(segments))

    # 3) Known built-in → honor the per-agent exposure config.
    canonical = canonical_command(name)
    if canonical in COMMAND_BY_NAME:
        exposed = cmds.is_exposed(canonical) if cmds is not None else True
        if not exposed:
            return CommandOutcome(
                handled=True,
                reply=f"🔒 `/{canonical}` is disabled for this agent.",
            )

    # 4) Dispatch (known-and-exposed, or an unknown command-shaped message —
    # the dispatcher reports the latter as "Unknown command").
    settings = get_session_settings(runner, base_thread_id)

    # Imported lazily: the command layer pulls in a lot of optional machinery,
    # and keeping it off this module's import path avoids cycles with the
    # platforms package.
    from olano.cli.commands import CommandContext, CommandDispatcher

    dispatcher = CommandDispatcher()
    ctx = CommandContext(
        config=config,
        runner=runner,
        thread_id=settings["active_thread"],
        sender=sender,
    )
    ctx.verbosity = settings["verbosity"]
    ctx.think_level = settings["think_level"]

    try:
        result = await dispatcher.dispatch(text, ctx)
    except Exception as exc:
        logger.error("chat command '/%s' failed: %s", name, exc, exc_info=True)
        return CommandOutcome(handled=True, reply=f"⚠️ `/{name}` failed: {exc}")

    # Persist any per-chat settings the command changed, on the *base* id so the
    # choices survive a /new that swaps the active thread.
    patch: dict[str, Any] = {}
    # ``verbosity_explicit`` matters when the user asks for the mode the chat is
    # already on: without it that command writes nothing, the chat stays
    # unpinned, and a later edit to the agent's ``default_verbosity`` would move
    # a chat that explicitly chose. Pinning on request keeps the choice theirs.
    if ctx.verbosity != settings["verbosity"] or getattr(ctx, "verbosity_explicit", False):
        patch[VERBOSITY_KEY] = ctx.verbosity
    if ctx.think_level != settings["think_level"]:
        patch[THINK_KEY] = ctx.think_level
    if result.new_thread_id:
        patch[ACTIVE_THREAD_KEY] = result.new_thread_id
    if patch:
        try:
            runner.update_thread_state(base_thread_id, **patch)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("persist chat settings for %s failed: %s", base_thread_id, exc)

    return CommandOutcome(
        handled=True,
        reply=_result_reply(result, agent_name=getattr(config, "name", None)),
    )


# ---------------------------------------------------------------------------
# Inbound media → model input
# ---------------------------------------------------------------------------
#
# Every platform adapter downloads inbound attachments to a local cache and
# records them on ``MessageEvent.media_urls`` / ``media_types`` (parallel
# lists). The runtime handler, however, historically streamed only
# ``event.text`` to the agent — so voice notes (which carry no text body)
# arrived as an *empty* message and images never reached the model's vision.
#
# :func:`build_agent_message` closes that gap for every platform at once:
#
#   * **audio / voice**  → transcribed via the provider-independent speech
#     layer (``olano/speech.py``, config.yaml ``speech.stt``, **disabled by
#     default**) and merged into the text the agent sees; when disabled/failed
#     a note is injected so the agent tells the sender to resend as text
#     instead of replying to nothing.
#   * **images / photos** → saved into the agent workspace's ``uploads/`` dir
#     and referenced by a ``[Image attached: … saved to <path>]`` note — the
#     same save-and-reference contract as dashboard chat attachments
#     (``runtime/chat_attachments.py``). The agent decides from the sender's
#     message what to do with the file: view it via a vision tool
#     (``image_describe(<path>)``), pass the path to another tool, or keep it.
#     Inlining pixels as vision blocks was dropped on purpose: non-vision
#     models silently saw nothing, and the checkpointer re-billed the vision
#     tokens on every later turn of the chat's thread.
#   * **video** → copied into ``uploads/`` + saved-path note + best-effort
#     audio-track transcript.
#   * **documents** → copied into ``uploads/`` like images, with PDF/Office/HTML
#     text also extracted inline. The copy matters as much as the text: the
#     extract caps a spreadsheet at 50 rows per sheet and drops its formulas,
#     and the adapter cache is outside the workspace, so a sandboxed agent
#     could read the summary of a file it could never open. Plain-text files
#     are already inlined by the adapters; anything else gets a path note to
#     its workspace copy.


# Injected when speech-to-text is switched off (the default). The agent relays
# the quoted phrase to the sender.
STT_DISABLED_NOTE = (
    "[A voice message was received but speech-to-text is disabled on this "
    'server. Tell the sender: "couldn\'t transcribe, please resend as text". '
    "(Operator: enable it in the dashboard Config → Speech section or "
    "config.yaml → speech.stt.)]"
)
# Injected when STT is enabled but the call failed (missing key, provider
# error, network). Distinct from disabled so operators can tell them apart.
STT_FAILED_NOTE = (
    "[A voice message was received but could not be transcribed (speech-to-text "
    "error — check the configured provider/credentials). Ask the sender to "
    "resend as text.]"
)


async def _transcribe_audio(path: str) -> tuple[str | None, str | None]:
    """Transcribe a cached audio file via the configured speech provider.

    Returns ``(transcript, note)`` — exactly one is non-``None``. Never raises:
    a transcription hiccup must not sink the whole message. Runs the blocking
    HTTP call in a worker thread; contextvars (per-agent credentials) propagate.
    """
    from olano.speech import SpeechDisabledError, transcribe_file

    try:
        text = await asyncio.to_thread(transcribe_file, path)
    except SpeechDisabledError:
        logger.info("voice note received but speech-to-text is disabled: %s", path)
        return None, STT_DISABLED_NOTE
    except Exception as exc:
        logger.warning("audio transcription failed for %s: %s", path, exc)
        return None, STT_FAILED_NOTE
    if not text:
        return None, STT_FAILED_NOTE
    return text, None


def _display_name(path: str) -> str:
    """Human filename for a cached attachment (strips the ``doc_<hex>_`` prefix)."""
    fname = Path(path).name
    parts = fname.split("_", 2)
    if len(parts) >= 3 and parts[1] and all(c in "0123456789abcdef" for c in parts[1]):
        return parts[2]
    return fname


# Inbound files larger than this are left in the adapter cache rather than
# copied into the workspace (the note then names the cache path). Every
# adapter already caps downloads well below it (WhatsApp 25 MB, Telegram's
# Bot API 20 MB), so in practice every attachment is copied.
STASH_MAX_BYTES = 100 * 1024 * 1024


def _stash_file(
    path: str,
    mime: str,
    workspace_root: Path | str | None,
    kind: str,
) -> tuple[Path, int] | None:
    """Copy an adapter-cached attachment into ``<workspace>/uploads/``.

    Returns ``(saved_path, size)``. The saved path is the workspace copy when
    one could be made, otherwise the cache path itself: no workspace known, a
    file over :data:`STASH_MAX_BYTES`, or a failed copy. The cached file
    exists locally, so the agent can still act on it this turn; the copy is
    what makes it survive cache cleanup and reachable by the agent's jailed
    file tools and its sandbox, which never mount the adapter cache.

    Returns ``None`` only when the cached file is missing or unreadable.
    """
    from olano.uploads import save_media_bytes

    p = Path(path)
    if not p.is_file():
        logger.info("inbound %s not found on disk: %s", kind, path)
        return None
    try:
        size = p.stat().st_size
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("failed to stat inbound %s %s: %s", kind, path, exc)
        return None
    if workspace_root is None or size > STASH_MAX_BYTES:
        if size > STASH_MAX_BYTES:
            logger.info("inbound %s %s is %d bytes; left in the cache", kind, path, size)
        return p, size
    try:
        raw = p.read_bytes()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("failed to read inbound %s %s: %s", kind, path, exc)
        return None
    try:
        saved, _reused = save_media_bytes(workspace_root, _display_name(path), mime, kind, raw)
    except OSError as exc:
        logger.warning("failed to copy inbound %s %s into workspace: %s", kind, path, exc)
        return p, len(raw)  # note the cache path instead: never lose the file
    return saved, len(raw)


def _stash_image(
    path: str,
    mime: str,
    workspace_root: Path | str | None,
    *,
    label: str = "Image attached",
) -> str | None:
    """Persist an inbound image into the workspace and return its path note.

    Copies the adapter-cached file into ``<workspace>/uploads/`` (shared
    :mod:`olano.uploads` store: sanitized name, byte-identical resends reuse
    the existing file) via :func:`_stash_file`, which also owns the fallback
    to the cache path. Returns ``None`` only when the cached file is
    missing/unreadable (mirrors the pre-existing skip behavior).
    """
    from olano.uploads import human_size

    media_type = (mime or "").strip().lower() or "image/jpeg"
    stashed = _stash_file(path, media_type, workspace_root, "image")
    if stashed is None:
        return None
    saved, size = stashed
    name = _display_name(path)
    return f"[{label}: {name} — saved to {saved} ({media_type}, {human_size(size)})]"


def _document_text(
    path: str, saved_path: Path | str | None = None, *, kept: bool = True
) -> str | None:
    """Extract text from a cached binary document (PDF / Office / HTML).

    Uses the same converters as the ``document_to_text`` agent tool, called
    directly because the tool wrapper is workspace-jailed and inbound
    attachments live in ``~/.olano/cache/documents``. Returns ``None`` when
    extraction fails or yields nothing (scanned PDFs, images-only decks).
    ``saved_path`` is where the full file now lives (the workspace copy), named
    in the truncation note; it defaults to ``path``. ``kept=False`` (a file
    that is extracted and then discarded) leaves the path out of that note.
    """
    p = Path(path)
    if not p.is_file():
        return None
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            import pypdf

            reader = pypdf.PdfReader(str(p))
            parts = []
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    parts.append(f"--- Page {i + 1} ---\n{page_text}")
            text = "\n\n".join(parts)
        elif ext == ".docx":
            from olano.tools.media_tools import _docx_to_text

            text = _docx_to_text(p)
        elif ext == ".pptx":
            from olano.tools.media_tools import _pptx_to_text

            text = _pptx_to_text(p, max_slides=0)
        elif ext == ".xlsx":
            from olano.tools.media_tools import _xlsx_to_text

            text = _xlsx_to_text(p, max_sheets=10, max_rows_per_sheet=50)
        elif ext in (".html", ".htm"):
            from olano.tools.media_tools import _html_to_text

            text = _html_to_text(p)
        else:
            return None
    except Exception as exc:
        logger.info("inbound document extraction failed for %s: %s", path, exc)
        return None
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > DOC_INJECT_MAX_CHARS:
        where = f" — full file saved at {saved_path or path}" if kept else ""
        text = (
            text[:DOC_INJECT_MAX_CHARS]
            + f"\n[... truncated, {len(text) - DOC_INJECT_MAX_CHARS} chars omitted{where}]"
        )
    return text


def _reader_hint(path: Path | str) -> str:
    """How to read a saved attachment the inline extract does not cover.

    Older and open office formats (.doc, .xls, .odt, ...) convert through
    ``document_to_text``; text of any extension reads with ``read_file``; other
    binaries need the shell. Never raises: a hint is optional.
    """
    from olano.uploads import file_category

    try:
        category = file_category(path)
    except Exception:  # pragma: no cover - defensive
        return ""
    return {
        "document": "; read it with document_to_text",
        "text": "; a text file, read it with read_file",
        "archive": "; an archive, list or unpack it with the shell if you have one",
    }.get(category, "")


async def _video_transcript_segments(path: str) -> list[str]:
    """Best-effort audio-track transcript for a local video file.

    Empty when the file is missing/oversized, ffmpeg is absent, STT is
    disabled, or extraction/transcription fails — the caller's saved-path note
    already tells the agent a video arrived. Shared by the platform inbound
    path above and dashboard chat uploads (``runtime/chat_attachments.py``).
    """
    import shutil
    import uuid

    segments: list[str] = []
    p = Path(path)
    if not p.is_file() or p.stat().st_size > VIDEO_TRANSCRIBE_MAX_BYTES:
        return segments
    if not shutil.which("ffmpeg"):
        return segments
    from olano.speech import stt_available

    if not stt_available():
        return segments

    from olano.platforms.base import get_audio_cache_dir

    audio_out = str(get_audio_cache_dir() / f"video_audio_{uuid.uuid4().hex[:12]}.mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(p),
            "-vn",
            "-acodec",
            "mp3",
            audio_out,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            transcript, _note = await _transcribe_audio(audio_out)
            if transcript:
                segments.append(f"[Video audio transcript] {transcript}")
    except Exception as exc:
        logger.info("video audio extraction failed for %s: %s", path, exc)
    finally:
        try:
            Path(audio_out).unlink(missing_ok=True)
        except Exception:  # pragma: no cover - defensive
            pass
    return segments


@dataclass(frozen=True)
class _MediaLabels:
    """Wording for one attachment's note, per the message it belongs to.

    A reply to a photo and a photo the sender just took produce the same file
    and call for different answers — "the image you're asking about" versus
    "the image you just sent". The agent's only cue is this wording, so the
    two sets are kept apart rather than sharing one neutral phrasing.
    """

    image: str
    transcript: str
    video: str
    document: str
    attachment: str
    received: str


_OWN_LABELS = _MediaLabels(
    image="Image attached",
    transcript="Voice message transcript",
    video="Video attachment saved",
    document="Content of",
    attachment="Attachment saved",
    received="Attachment received",
)
_QUOTED_LABELS = _MediaLabels(
    image="Image on the quoted message",
    transcript="Quoted voice message transcript",
    video="Video on the quoted message, saved",
    document="Content of the quoted message's",
    attachment="Quoted message attachment saved",
    received="Quoted message attachment received",
)


async def _media_extras(
    urls: list[str],
    types: list[str],
    *,
    workspace_root: Path | str | None,
    labels: _MediaLabels,
) -> tuple[list[str], int]:
    """Fold one message's attachments into prompt segments.

    Returns ``(segments, stashed_files)``: how many attachments were copied
    into the workspace, which decides whether the guidance note is added.
    Shared by a message's own media
    and the media on the message it quotes, so the two can never drift in what
    they extract — only in how they are labelled.
    """
    extras: list[str] = []
    stashed_files = 0

    for idx, url in enumerate(urls):
        mime = ((types[idx] if idx < len(types) else "") or "").lower()
        ext = Path(url).suffix.lower()
        if mime.startswith("audio/"):
            transcript, note = await _transcribe_audio(url)
            if transcript:
                extras.append(f"[{labels.transcript}] {transcript}")
            elif note:
                extras.append(note)
        elif mime.startswith("image/"):
            note = _stash_image(url, mime, workspace_root, label=labels.image)
            if note:
                extras.append(note)
                stashed_files += 1
        elif mime.startswith("video/"):
            stashed = _stash_file(url, mime, workspace_root, "video")
            saved = stashed[0] if stashed else url
            if stashed and saved != Path(url):
                stashed_files += 1
            extras.append(f"[{labels.video} at {saved} ({mime or 'video'})]")
            extras.extend(await _video_transcript_segments(url))
        elif ext in _DOC_EXTRACT_EXTENSIONS:
            # The workspace copy is what lets the agent work on the real file
            # (a spreadsheet's formulas, every row past the extract's cap, a
            # template to fill in); the inline text only lets it read one.
            stashed = _stash_file(url, mime, workspace_root, "document")
            saved = stashed[0] if stashed else url
            if stashed and saved != Path(url):
                stashed_files += 1
            doc_text = await asyncio.to_thread(_document_text, url, saved)
            if doc_text:
                extras.append(
                    f"[{labels.document} {_display_name(url)}]:\n{doc_text}\n"
                    f"[Original file saved to {saved} ({mime or ext})]"
                )
            else:
                # A scanned PDF has no text layer; document_to_text runs OCR on
                # the saved copy, which this inline extract does not.
                extras.append(
                    f"[{labels.attachment}: {_display_name(url)} at {saved} ({mime or ext}) — "
                    "no text could be extracted here; call document_to_text on it, which "
                    "runs OCR on scanned PDFs]"
                )
        elif mime in ("", "unknown") or not Path(url).is_file():
            # Nothing useful to do — but never let an attachment be silent.
            if url:
                extras.append(f"[{labels.received}: {url} ({mime or 'unknown type'})]")
        else:
            stashed = _stash_file(url, mime, workspace_root, "file")
            saved = stashed[0] if stashed else url
            if stashed and saved != Path(url):
                stashed_files += 1
            hint = _reader_hint(saved)
            extras.append(f"[{labels.attachment}: {_display_name(url)} at {saved} ({mime}){hint}]")

    return extras, stashed_files


async def build_agent_message(
    text: str, event: Any, workspace_root: Path | str | None = None
) -> str:
    """Turn an inbound platform message into the payload streamed to the agent.

    ``text`` is the already-composed prompt text (sender prefix + body + any
    per-chat directives); ``workspace_root`` is the receiving agent's workspace
    (images are copied into its ``uploads/`` dir when given). Inbound
    attachments on ``event.media_urls`` / ``event.media_types`` are folded in:

    - **audio/voice** → transcribed and appended as ``[Voice message transcript]``
      (with a can't-transcribe note otherwise, so the agent never sees an
      empty prompt);
    - **images** → saved to ``<workspace>/uploads/`` + an ``[Image attached:
      … saved to <path>]`` note and a one-shot guidance note — the agent
      views the file with a vision tool or uses the path directly (same
      contract as dashboard chat attachments);
    - **video** → saved to ``<workspace>/uploads/`` + a saved-path note +
      best-effort audio-track transcript;
    - **PDF / Office / HTML documents** → saved to ``<workspace>/uploads/``
      and their text extracted inline (adapters already inline plain-text
      files themselves), path note when extraction yields nothing (e.g.
      scanned PDFs);
    - **anything else** → saved to ``<workspace>/uploads/`` with a path note,
      so the attachment is never silent.

    Attachments on the message being *replied to*
    (``event.reply_to_media_urls``) go through the same extraction under
    quoted wording, ahead of the sender's own media — without it, replying to
    a photo reached the agent as "[replying to an earlier message] (no text)"
    and the picture the whole message was about never arrived.

    Returns the original ``text`` string unchanged when there is no media, so
    the common text-only path stays a cheap string.
    """
    media_urls = list(getattr(event, "media_urls", None) or [])
    media_types = list(getattr(event, "media_types", None) or [])
    quoted_urls = list(getattr(event, "reply_to_media_urls", None) or [])
    quoted_types = list(getattr(event, "reply_to_media_types", None) or [])
    if not media_urls and not quoted_urls:
        return text

    extras: list[str] = []  # text segments appended after the message body
    stashed_files = 0

    # Quoted media first: it is the thing the message is *about*, and the
    # reply-context block above the body has already announced it.
    if quoted_urls:
        quoted_extras, quoted_stashed = await _media_extras(
            quoted_urls, quoted_types, workspace_root=workspace_root, labels=_QUOTED_LABELS
        )
        extras.extend(quoted_extras)
        stashed_files += quoted_stashed

    own_extras, own_stashed = await _media_extras(
        media_urls, media_types, workspace_root=workspace_root, labels=_OWN_LABELS
    )
    extras.extend(own_extras)
    stashed_files += own_stashed

    if stashed_files:
        from olano.uploads import MEDIA_GUIDANCE_NOTE

        extras.append(MEDIA_GUIDANCE_NOTE)

    combined = text
    if extras:
        joined = "\n\n".join(extras)
        combined = f"{combined}\n\n{joined}" if combined.strip() else joined
    return combined
