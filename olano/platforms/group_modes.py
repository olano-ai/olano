"""Uniform group listening / responding modes for messenger connectors.

Historically each adapter coupled "what the bot hears" and "what the bot
answers" into one boolean (``require_mention``), with WhatsApp adding
``listen_only`` and Telegram adding ``passive_listen``. These helpers split
that into two orthogonal knobs with the same names and values on every
group-capable connector:

- ``group_respond`` — which group messages trigger an agent turn (a reply):
    * ``"all"``       every group message the access lists let through
    * ``"mentions"``  only @mentions / replies-to-bot / commands / wake words
    * ``"none"``      never — the bot stays silent in groups
- ``group_listen`` — which group messages that did NOT trigger a turn are
  recorded to the persistent observed-messages store
  (``chat_directory.record_observed_event``):
    * ``"off"``       record nothing (messages are dropped, as before)
    * ``"mentions"``  record only messages that addressed the bot
    * ``"all"``       record every group message that passed the access lists

DMs are never affected — both knobs govern group/channel traffic only.

Back-compat is strict: when a knob is unset, the adapter's legacy setting
decides — ``require_mention`` maps to ``group_respond`` ("mentions"/"all"),
Telegram's ``passive_listen`` maps to ``group_listen: "all"``. A set knob
always wins over its legacy counterpart.
"""

from __future__ import annotations

from typing import Any

RESPOND_MODES = ("all", "mentions", "none")
LISTEN_MODES = ("off", "mentions", "all")

#: Accepted spellings → canonical value. "none"/"never" read naturally for a
#: silent bot; "true"/"false" guard against a bool leaking into the new key.
_RESPOND_SYNONYMS = {
    "all": "all",
    "everything": "all",
    "mentions": "mentions",
    "mention": "mentions",
    "none": "none",
    "never": "none",
    "silent": "none",
}
_LISTEN_SYNONYMS = {
    "off": "off",
    "none": "off",
    "disabled": "off",
    "mentions": "mentions",
    "mention": "mentions",
    "all": "all",
    "everything": "all",
}


def _configured(extra: dict[str, Any] | None, key: str, synonyms: dict[str, str]) -> str:
    raw = (extra or {}).get(key)
    if raw is None:
        return ""
    return synonyms.get(str(raw).strip().lower(), "")


def resolve_group_respond(extra: dict[str, Any] | None, *, legacy_require_mention: bool) -> str:
    """The effective ``group_respond`` mode for a connector.

    ``legacy_require_mention`` is the adapter's existing resolution of its
    ``require_mention`` knob (each adapter has its own default and env var);
    it only applies while ``group_respond`` is unset or unrecognized.
    """
    configured = _configured(extra, "group_respond", _RESPOND_SYNONYMS)
    if configured:
        return configured
    return "mentions" if legacy_require_mention else "all"


def resolve_group_listen(
    extra: dict[str, Any] | None, *, legacy_passive_listen: bool = False
) -> str:
    """The effective ``group_listen`` mode for a connector."""
    configured = _configured(extra, "group_listen", _LISTEN_SYNONYMS)
    if configured:
        return configured
    return "all" if legacy_passive_listen else "off"


def should_respond(mode: str, mentioned: bool) -> bool:
    """Whether a group message triggers an agent turn under *mode*."""
    if mode == "all":
        return True
    if mode == "mentions":
        return mentioned
    return False


def should_record(mode: str, mentioned: bool) -> bool:
    """Whether a NON-responded group message is recorded under *mode*."""
    if mode == "all":
        return True
    if mode == "mentions":
        return mentioned
    return False
