"""Edit-file — change one file's contents from a phone, over a magic link.

The sibling of ``edit-prompt``: a PIN-gated page holding one textarea, except
what it edits is a file the agent named at mint time. The point is the files a
non-technical owner is otherwise asked to reach by SSH — a config file, a
``.env``, a checklist an agent maintains.

**Two scopes, decided at mint, never by the browser.**

``workspace``
    A path relative to the agent's own workspace. Any agent may mint one.
    Containment, size caps and sandbox ownership all come from
    :mod:`olano.admin.workspace_files`, so this flow can reach exactly what the
    agent itself can already edit — the link grants no new authority, it just
    moves the keyboard.

``host``
    An absolute path anywhere the runtime can write. Reserved for the operator
    surfaces (dashboard, CLI, ``/olano connect`` — each already owner- or
    admin-gated) and for an admin-capable agent, i.e. AgentFather. The marker
    for "admin-capable" is the ``olano_write_agent_file`` grant: cross-agent
    file writing is the authority this scope extends, so the agent that holds
    it is the agent that may extend it.

The minting agent is read from :data:`olano.tools.talk_to.current_agent`, the
per-turn identity the runner binds from the executing agent's own config — the
same source per-agent credentials resolve through. It is never a tool argument,
so a model cannot name a different principal. ``validate`` recomputes the scope
and returns a fresh params dict, which matters because the dashboard mint route
forwards its request body's ``params`` verbatim: a ``scope: "host"`` smuggled
in there is discarded rather than trusted.

What the link holder sees IS the file. For a ``.env`` that is the entire point,
and it means the link is exactly as sensitive as the file — which is why the
page is PIN-gated, single-use and short-lived. The chat notice it produces
carries the path and a byte count, never a line of content.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from olano.magic_links.flows import (
    APPLY_SELF,
    STATUS_DONE,
    BaseFlow,
    FlowField,
    register,
)
from olano.magic_links.store import LinkRecord

logger = logging.getLogger(__name__)

#: Mirrors the admin read cap in :mod:`olano.admin.workspace_files` — a file
#: too big to load is a file too big to hand to a textarea on a phone.
MAX_FILE_BYTES = 256 * 1024

#: The grant that marks an agent admin-capable. An agent trusted to write into
#: another agent's workspace is already past the boundary this scope crosses.
ADMIN_MARKER_TOOL = "olano_write_agent_file"

#: Basenames whose content feeds the assembled system prompt. Saving one nudges
#: the runtime's refresh sweep, exactly as ``PUT /api/files`` does.
_PROMPT_BEARING = frozenset(
    {
        "AGENTS.md",
        "IDENTITY.md",
        "CORE_VIBES.md",
        "SAFETY_BOUNDARIES.md",
        "EXECUTION_BIAS.md",
        "TOOLS.md",
    }
)

#: Content as it stood when the page first rendered, per link id.
#:
#: ``link_state`` re-runs ``descriptor()`` on every 2.5s poll, and the page
#: redraws whenever the state payload changes. Re-reading the file each time
#: would wipe a half-typed edit the moment anything else touched the file —
#: which for a ``.env`` a running agent also writes is not hypothetical. The
#: snapshot is what the editor was opened on; the save writes what the human
#: submits.
_SNAPSHOT: dict[str, str] = {}

#: A link that is saved or revoked drops its snapshot; one that simply expires
#: never runs either path, so the cache is bounded too. Insertion-ordered, so
#: the oldest entry is the first key.
_SNAPSHOT_MAX = 64


def _remember(link_id: str, content: str) -> None:
    while len(_SNAPSHOT) >= _SNAPSHOT_MAX:
        _SNAPSHOT.pop(next(iter(_SNAPSHOT)), None)
    _SNAPSHOT[link_id] = content


def _minting_agent() -> str:
    """The agent whose turn is minting, or ``""`` on an operator surface."""
    from olano.tools.talk_to import current_agent

    return (current_agent.get() or "").strip()


def _may_edit_host_paths(minter: str) -> bool:
    """Whether this minting principal may point a link at an absolute path."""
    if not minter:
        # No agent turn: the dashboard route (assert_admin), the CLI at a
        # terminal, or /olano connect (owner-gated). All are the operator.
        return True
    try:
        from olano.config.loader import load_agent_config

        return ADMIN_MARKER_TOOL in (load_agent_config(minter).tools or [])
    except Exception:
        # An agent we cannot read is an agent we cannot vouch for.
        logger.debug("edit-file: could not read config for %s", minter, exc_info=True)
        return False


def _read_host_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_host_file(path: Path, content: str) -> None:
    # Deliberately no chown_for_sandbox: a host path like /opt/olano/olano.env
    # belongs to the runtime, and handing it to the sandbox uid would be a
    # permission change nobody asked for.
    path.write_text(content, encoding="utf-8")


def _check_editable(target: Path, shown: str) -> None:
    """Refuse anything a textarea cannot honestly represent."""
    if not target.exists():
        raise ValueError(f"'{shown}' does not exist. Create the file first, then send the link.")
    if not target.is_file():
        raise ValueError(f"'{shown}' is a directory. Give the path of a file to edit.")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"'{shown}' is {size:,} bytes — larger than the {MAX_FILE_BYTES:,}-byte limit "
            "for an editable file. Edit it on the server instead."
        )
    try:
        chunk = target.read_bytes()[:4096]
    except OSError as exc:
        raise ValueError(
            f"Could not read '{shown}': it may not be readable by the server."
        ) from exc
    if b"\x00" in chunk:
        raise ValueError(f"'{shown}' looks like a binary file. Only text files can be edited here.")


def _resolve_workspace(agent: str, rel: str) -> Path:
    from olano.admin.workspace_files import _resolve_inside, _workspace_root

    return _resolve_inside(_workspace_root(agent), rel)


class EditFileFlow(BaseFlow):
    id = "edit-file"
    label = "Edit file"
    kind = "form"
    # Same as edit-prompt: a workspace write plus a refresh-sweep nudge, and
    # every other file the agent reads at tool time anyway.
    applies = APPLY_SELF

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(params.get("path") or "").strip()
        if not raw_path:
            raise ValueError("A file path is required for an edit-file link.")
        agent = str(params.get("agent") or "").strip()

        if raw_path.startswith("/"):
            minter = _minting_agent()
            if not _may_edit_host_paths(minter):
                raise ValueError(
                    "Only an admin agent (or the dashboard) can send an edit link for an "
                    "absolute path. Give a path inside the agent's workspace instead."
                )
            target = Path(raw_path).resolve()
            _check_editable(target, raw_path)
            return {
                "agent": agent or minter,
                "path": str(target),
                "scope": "host",
                "service": "edit-file",
            }

        if not agent:
            raise ValueError(
                "An agent name is required for a workspace path. Pass an absolute path "
                "to edit a file outside any workspace."
            )
        try:
            target = _resolve_workspace(agent, raw_path)
        except PermissionError as exc:
            raise ValueError(
                f"'{raw_path}' resolves outside {agent}'s workspace. Give a path inside it, "
                "or an absolute path if you have admin rights."
            ) from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not resolve '{raw_path}' for agent '{agent}': {exc}") from exc
        _check_editable(target, raw_path)
        return {
            "agent": agent,
            "path": raw_path,
            "scope": "workspace",
            "service": "edit-file",
        }

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        agent = str(link.params.get("agent") or "")
        shown = str(link.params.get("path") or "")
        host = str(link.params.get("scope") or "") == "host"
        name = Path(shown).name or shown

        base["service"] = {"id": "edit-file", "name": name}
        base["title"] = f"Edit {name}"
        base["instructions"] = (
            f"You're editing {shown}"
            + (f" in {agent}'s workspace" if agent and not host else "")
            + ". Saving replaces the whole file with what's in the box below."
        )
        base["cta"] = "Save file"
        base["fields"] = [
            FlowField(
                name="value",
                label=shown,
                multiline=True,
                required=False,
                value=self._snapshot(link),
                placeholder="",
                help=(
                    "The box holds the file as it was when this page opened. Saving "
                    "replaces the file with exactly what is here — an empty box saves "
                    "an empty file."
                ),
            ).public()
        ]
        return base

    def _snapshot(self, link: LinkRecord) -> str:
        """The file as it was when this page first rendered (see _SNAPSHOT)."""
        cached = _SNAPSHOT.get(link.id)
        if cached is not None:
            return cached
        try:
            target = self._target(link)
            current = _read_host_file(target)
        except Exception as exc:
            # An unreadable file shows an empty box, never a 500 — the page must
            # still explain itself, and the save re-checks everything anyway.
            logger.warning("edit-file: could not load %s: %s", link.params.get("path"), exc)
            current = ""
        _remember(link.id, current)
        return current

    def _target(self, link: LinkRecord) -> Path:
        """Re-resolve the path at use time.

        Never trusted from a browser — ``link.params`` is what ``validate``
        returned at mint. Resolving again matters anyway: between mint and save
        the path may have become a symlink pointing somewhere else, and for a
        workspace scope that has to fail the containment check now, not then.
        """
        shown = str(link.params.get("path") or "")
        if not shown:
            raise ValueError("This edit link has no file path.")
        if str(link.params.get("scope") or "") == "host":
            return Path(shown).resolve()
        agent = str(link.params.get("agent") or "")
        if not agent:
            raise ValueError("This edit link has no agent to resolve its path against.")
        return _resolve_workspace(agent, shown)

    def submit(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        shown = str(link.params.get("path") or "")
        agent = str(link.params.get("agent") or "")
        host = str(link.params.get("scope") or "") == "host"
        # An empty save is legitimate here (unlike edit-prompt): a config file
        # or a .env is allowed to be empty, and refusing would make the flow
        # unable to express a real state.
        value = str(fields.get("value") or "")
        if len(value.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(
                f"That's too long ({len(value):,} characters — the limit is "
                f"{MAX_FILE_BYTES:,} bytes). Trim it down and save again."
            )
        try:
            target = self._target(link)
            if target.exists() and target.is_dir():
                raise ValueError(f"'{shown}' is a directory now. Nothing was saved.")
            if host:
                _write_host_file(target, value)
            else:
                from olano.admin.workspace_files import write_agent_workspace_file

                write_agent_workspace_file(agent, shown, value)
        except PermissionError as exc:
            raise ValueError(
                f"'{shown}' is no longer inside {agent}'s workspace. Nothing was saved."
            ) from exc
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("edit-file: save failed for %s: %s", shown, exc)
            raise ValueError(
                "Could not save the file. Ask the operator to check the server logs."
            ) from exc

        _SNAPSHOT.pop(link.id, None)
        if not host and agent and Path(shown).name in _PROMPT_BEARING:
            _request_prompt_refresh(agent)
        written = len(value.encode("utf-8"))
        return {
            "status": STATUS_DONE,
            "result": f"{shown} was saved ({written:,} bytes).",
        }

    def cancel(self, link: LinkRecord) -> None:
        _SNAPSHOT.pop(link.id, None)


def _request_prompt_refresh(agent: str) -> None:
    """Nudge the runtime's prompt-refresh sweep (no-op outside the daemon)."""
    try:
        from olano.tools.talk_to import get_bound_runtime

        runtime = get_bound_runtime()
        if runtime is not None:
            runtime.request_prompt_refresh([agent])
    except Exception:  # pragma: no cover - a nudge must never fail the save
        logger.debug("edit-file: prompt refresh nudge failed", exc_info=True)


register(EditFileFlow())
