"""Channel credentials live in the vault, never in ``agent.json5``.

A bot token typed into the dashboard's connector form used to be written to
``agent.json5`` verbatim, so a plain-text Telegram token sat on disk (and in
every backup, and in any config export) for as long as the agent existed. The
docs said "prefer an environment reference", but that asked the user to do the
work: invent a name, add it to the vault, then type ``${vault:NAME}`` back into
the form. Nobody does that, and the six agents on the prod CVM proved it.

The vault already supports exactly the shape this needs — a **common** value
plus **per-agent overrides** (``credential_overrides``), with resolution
``per-agent override → common`` scoped by agent name. Every connector type has
exactly one obvious key name, so there is nothing for a config to say:

    agent.json5 (any agent)   {"type": "telegram", "allowed_users": [...]}
    vault                     TELEGRAM_BOT_TOKEN @marisol = 8182…t5jQ
                              TELEGRAM_BOT_TOKEN @andrew  = 8858…Jrmc

Two halves, both owned here:

* **Write** (:func:`absorb_channel_secrets`) — a credential that reaches a
  config anyway (connector form, onboarding wizard, agentfather's config tools,
  a raw JSON5 edit) is moved into the vault as a per-agent override and the
  field is **removed** from the config. A ``${...}`` reference that just names
  the canonical key is redundant under the convention and is removed too; a
  reference to some *other* key is a deliberate choice and is left alone.
* **Read** (:func:`hydrate_channel_credentials`) — at channel boot the runtime
  puts the credential back into the adapter's config dict, resolved
  per-agent-override → common → ``os.environ``. Adapters keep receiving a
  plain ``token`` key and never learn the vault exists.

Both halves are idempotent, so re-saving or re-booting changes nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Any ``${...}`` — a vault reference or a bare env placeholder. Either way the
# value is already indirect and there is nothing to absorb.
_REFERENCE = re.compile(r"\$\{[^}]+\}")

# channel type → {config field: canonical vault key}.
#
# The keys match the placeholders the dashboard has always shown in these
# fields (``${TELEGRAM_BOT_TOKEN}``, ``${SLACK_APP_TOKEN}``, …), so the name a
# user was told to use by hand is the name they now get automatically.
# `platform:telegram` and `telegram` share an entry — the prefix is stripped
# before lookup, since the two adapters read the same credential.
CHANNEL_SECRET_KEYS: dict[str, dict[str, str]] = {
    "telegram": {"token": "TELEGRAM_BOT_TOKEN"},
    "discord": {"token": "DISCORD_BOT_TOKEN"},
    "slack": {
        "token": "SLACK_BOT_TOKEN",
        "bot_token": "SLACK_BOT_TOKEN",
        "app_token": "SLACK_APP_TOKEN",
    },
    # Twilio's auth token is the WhatsApp connector's only true secret: the
    # account SID is an identifier and the sender number is public, so neither
    # is vaulted. The key is the canonical Twilio name, shared with the SMS
    # connector, so an operator who already set it once does not set it twice.
    "whatsapp_twilio": {"auth_token": "TWILIO_AUTH_TOKEN"},
    # Meta's Cloud API connector has two secrets: the access token (it sends
    # as the business) and the app secret (it proves a webhook came from
    # Meta). The Business Account and phone number IDs are identifiers.
    "whatsapp_meta_api": {
        "access_token": "WHATSAPP_META_ACCESS_TOKEN",
        "app_secret": "WHATSAPP_META_APP_SECRET",
    },
    "matrix": {"token": "MATRIX_ACCESS_TOKEN"},
    "mattermost": {"token": "MATTERMOST_TOKEN"},
    "wecom": {"secret": "WECOM_SECRET"},
    "weixin": {"app_token": "WEIXIN_APP_TOKEN"},
    "bluebubbles": {"password": "BLUEBUBBLES_PASSWORD"},
    "qqbot": {"client_secret": "QQ_CLIENT_SECRET"},
    "debox": {"token": "DEBOX_APP_KEY", "api_secret": "DEBOX_APP_SECRET"},
    "email": {
        "password": "EMAIL_PASSWORD",
        "imap_password": "EMAIL_IMAP_PASSWORD",
        "smtp_password": "EMAIL_SMTP_PASSWORD",
    },
    "imap": {
        "password": "EMAIL_PASSWORD",
        "imap_password": "EMAIL_IMAP_PASSWORD",
        "smtp_password": "EMAIL_SMTP_PASSWORD",
    },
    # The one entry whose "service" is us: this key is minted for clients
    # calling the agent, not issued by a remote platform.
    "api_server": {"api_key": "OLANO_API_KEY"},
}

# Canonical key → the name it used to have. Read-only: a lookup falls back to
# the legacy row so a rename never silently un-authenticates a live connector,
# but a *write* only ever lands on the canonical name, so the two spellings
# cannot both accumulate values. `migrations/0018` moves existing rows over;
# this fallback covers the window before it runs and the case where it could
# not (a locked vault at migration time).
LEGACY_SECRET_KEYS: dict[str, str] = {
    "OLANO_API_KEY": "LIONCLAW_API_KEY",
}

# channel type → the config fields the runtime injects at boot.
#
# This is the *read* side of CHANNEL_SECRET_KEYS and deliberately narrower: it
# lists the field each adapter actually reads, so exactly one field is injected
# per vault key. Slack's ``token`` is absent because the adapter reads
# ``bot_token`` (channels/slack_channel.py) — injecting both would hand the
# adapter a key it never asked for.
CHANNEL_CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": ("token",),
    "discord": ("token",),
    "slack": ("bot_token", "app_token"),
    "whatsapp_twilio": ("auth_token",),
    "whatsapp_meta_api": ("access_token", "app_secret"),
    "matrix": ("token",),
    "mattermost": ("token",),
    "wecom": ("secret",),
    "weixin": ("app_token",),
    "bluebubbles": ("password",),
    "qqbot": ("client_secret",),
    "debox": ("token", "api_secret"),
    "email": ("password", "imap_password", "smtp_password"),
    "imap": ("password", "imap_password", "smtp_password"),
    "api_server": ("api_key",),
}

# Fields a connector cannot authenticate without. A missing one means "don't
# boot this connector, and tell the operator exactly which key to set" —
# everything else is injected when present and never blocks a boot (email auth
# can be anonymous, an api_server can be open, and the Chinese-platform
# adapters have alternate credential paths).
REQUIRED_CHANNEL_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "telegram": ("token",),
    "discord": ("token",),
    "slack": ("bot_token", "app_token"),
    "whatsapp_twilio": ("auth_token",),
    # The app secret is not required: without it the connector still sends,
    # and its diagnostics say incoming messages are off until it is set.
    "whatsapp_meta_api": ("access_token",),
    "matrix": ("token",),
    "mattermost": ("token",),
    "qqbot": ("client_secret",),
    "debox": ("token",),
}


@dataclass(frozen=True)
class PendingSecret:
    """A credential field sitting in a config, and where it belongs.

    ``literal`` distinguishes the two things absorb removes: a real secret
    (store it, then drop the field) from a now-redundant ``${...}`` reference
    to the canonical key (just drop the field — the value is already vaulted).
    """

    channel_type: str
    field_name: str
    vault_key: str
    value: str
    literal: bool = True


@dataclass
class AbsorbReport:
    """What one absorb pass did, for the UI toast and the CLI sweep."""

    # (channel type, field, vault key) triples moved into the vault this pass.
    vaulted: list[tuple[str, str, str]] = field(default_factory=list)
    # (channel type, field, vault key) triples that were already vaulted and
    # whose now-redundant ${...} reference was dropped from the config.
    stripped: list[tuple[str, str, str]] = field(default_factory=list)
    # Literal secrets left in place because the vault could not take them.
    left_in_config: list[tuple[str, str]] = field(default_factory=list)
    vault_available: bool = True

    @property
    def changed(self) -> bool:
        return bool(self.vaulted or self.stripped)


@dataclass
class CredentialResolution:
    """What :func:`hydrate_channel_credentials` put into an adapter's config."""

    # (field, vault key, source) for every field filled in from the vault/env.
    injected: list[tuple[str, str, str]] = field(default_factory=list)
    # (field, vault key) for every REQUIRED field that resolved to nothing.
    missing: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def base_channel_type(channel_type: str) -> str:
    """``platform:telegram`` → ``telegram``; anything else unchanged."""
    return channel_type.split(":", 1)[1] if channel_type.startswith("platform:") else channel_type


def canonical_secret_key(channel_type: str, field_name: str) -> str | None:
    """The vault key a given channel field should live under, if any."""
    return CHANNEL_SECRET_KEYS.get(base_channel_type(channel_type), {}).get(field_name)


def is_reference(value: Any) -> bool:
    """True when the value already points somewhere instead of holding a secret."""
    return isinstance(value, str) and bool(_REFERENCE.search(value))


def credential_fields(channel_type: str) -> tuple[str, ...]:
    """The config fields this connector's credentials are injected into."""
    return CHANNEL_CREDENTIAL_FIELDS.get(base_channel_type(channel_type), ())


def required_credential_fields(channel_type: str) -> tuple[str, ...]:
    """The fields this connector cannot boot without."""
    return REQUIRED_CHANNEL_CREDENTIALS.get(base_channel_type(channel_type), ())


def credential_keys(channel_type: str) -> dict[str, str]:
    """``{field: vault key}`` for the fields the runtime injects at boot.

    The dashboard's connector panel and ``olano_list_platforms`` both read
    this, so the key a user is shown is the key the runtime actually resolves.
    """
    base = base_channel_type(channel_type)
    mapping = CHANNEL_SECRET_KEYS.get(base, {})
    return {f: mapping[f] for f in CHANNEL_CREDENTIAL_FIELDS.get(base, ()) if f in mapping}


def is_redundant_reference(value: Any, vault_key: str) -> bool:
    """True for ``${vault:KEY}`` / ``${KEY}`` naming this field's own key.

    Such a reference says exactly what the convention already says, so it can be
    dropped. A reference to a *different* key (``${vault:MARISOL_TOKEN}``) is a
    deliberate override and must survive.
    """
    if not isinstance(value, str):
        return False
    return value.strip() in (f"${{vault:{vault_key}}}", f"${{{vault_key}}}")


def hydrate_channel_credentials(
    channel_type: str,
    cfg: dict[str, Any],
    *,
    agent: str,
) -> CredentialResolution:
    """Fill a channel config's credential fields from the vault, in place.

    Every connector has one canonical key per credential (``telegram.token`` →
    ``TELEGRAM_BOT_TOKEN``), so the config never has to name it. Resolution per
    field, first hit wins:

        explicit value already in ``cfg``  (back-compat: a literal or a
                                            ``${...}`` the resolver expanded)
        → vault per-agent override
        → vault common value
        → ``os.environ[KEY]``

    That is :func:`olano.vault.resolver.lookup_credential`'s order, so this
    matches how every other credential in the process resolves. Fields that
    resolve to nothing are reported in ``missing`` only when the connector
    genuinely cannot authenticate without them — the caller decides what to do,
    which for the runtime means "skip this connector and say why".
    """
    from olano.vault.resolver import lookup_credential

    result = CredentialResolution()
    keys = credential_keys(channel_type)
    required = set(required_credential_fields(channel_type))

    for field_name, vault_key in keys.items():
        existing = cfg.get(field_name)
        if isinstance(existing, str) and existing.strip():
            if not is_reference(existing):
                continue  # a real value already (back-compat literal, or resolved)
            if not is_redundant_reference(existing, vault_key):
                # An unresolved reference to a *different* key. The operator
                # deliberately pointed this connector somewhere else, so the
                # miss means a locked vault or a missing entry — NOT licence to
                # substitute the canonical credential, which would quietly
                # authenticate as a different bot. Leave it; the caller's
                # unresolved-placeholder check reports it and schedules the
                # reboot for when the vault opens.
                continue
        # Either absent, or an unresolved reference to this field's own key —
        # which is what the convention resolves anyway.
        cfg.pop(field_name, None)

        value = lookup_credential(vault_key, agent)
        source_key = vault_key
        if not value:
            # A renamed key: the operator's value may still sit under the old
            # name. Falling back keeps a working connector working — without
            # it the field resolves empty, and for a connector whose missing
            # credential is not "required" (api_server may legitimately be
            # open) that turns bearer auth off with nothing reporting it.
            legacy_key = LEGACY_SECRET_KEYS.get(vault_key)
            if legacy_key:
                value = lookup_credential(legacy_key, agent)
                if value:
                    source_key = legacy_key
        if value:
            cfg[field_name] = value
            result.injected.append((field_name, source_key, _source_of(source_key, agent)))
        elif field_name in required:
            result.missing.append((field_name, vault_key))

    return result


def _source_of(vault_key: str, agent: str) -> str:
    """Where a resolved credential came from — for logs and diagnostics only."""
    from olano.vault.resolver import get_vault

    vault = get_vault()
    if vault is not None:
        try:
            if vault.list_overrides(name=vault_key, agent=agent):
                return "vault:agent"
            if any(row.get("name") == vault_key for row in vault.list_names()):
                return "vault:common"
        except Exception:  # pragma: no cover - diagnostics must never raise
            logger.debug("Could not attribute the source of %s", vault_key, exc_info=True)
    return "env"


def _resolve_vault() -> Any:
    """The unlocked vault, auto-unlocking from keyring/env if it can. Never prompts."""
    from olano.vault.resolver import get_vault, set_vault

    vault = get_vault()
    if vault is not None:
        return vault
    try:
        from olano.vault.runtime import unlock_vault_for_session

        result = unlock_vault_for_session(allow_prompt=False)
        if result and result.vault is not None:
            set_vault(result.vault)
            return result.vault
    except Exception:  # pragma: no cover - defensive; a locked vault is normal
        logger.debug("Vault auto-unlock failed during secret absorb", exc_info=True)
    return None


def absorb_channel_secrets(
    agent: str,
    data: dict[str, Any],
    vault: Any = None,
    *,
    dry_run: bool = False,
) -> AbsorbReport:
    """Move channel credentials out of ``data`` and into the vault, in place.

    Each literal value is written as a **per-agent override** under its
    canonical key and the field is then **removed** from the config: the
    connector resolves it by convention at boot
    (:func:`hydrate_channel_credentials`), so a config has nothing to say about
    it. A ``${...}`` reference naming that same canonical key is redundant for
    the same reason and is dropped without a vault write; a reference to any
    other key is deliberate and survives untouched.

    Calling this twice is a no-op — the second pass finds no credential fields.

    A locked vault is not an error and never loses data: the literal stays
    exactly where it was and lands in ``left_in_config`` so the caller can say
    so. Same for a vault write that fails.
    """
    report = AbsorbReport()
    owners = _pending_with_owners(data)
    if not owners:
        return report

    literals = [(e, p) for e, p in owners if p.literal]

    if dry_run:
        report.vaulted = [(p.channel_type, p.field_name, p.vault_key) for _, p in literals]
        report.stripped = [
            (p.channel_type, p.field_name, p.vault_key) for _, p in owners if not p.literal
        ]
        return report

    # A redundant reference holds no secret, so it can be dropped whether or not
    # the vault is reachable.
    for entry, pending in owners:
        if not pending.literal:
            entry.pop(pending.field_name, None)
            report.stripped.append((pending.channel_type, pending.field_name, pending.vault_key))

    if not literals:
        _log_absorbed(agent, report)
        return report

    vault = vault if vault is not None else _resolve_vault()
    if vault is None:
        report.vault_available = False
        report.left_in_config = [(p.channel_type, p.field_name) for _, p in literals]
        _warn_unavailable(agent, len(literals))
        return report

    for entry, pending in literals:
        if not _store(vault, agent, pending):
            report.left_in_config.append((pending.channel_type, pending.field_name))
            continue
        entry.pop(pending.field_name, None)
        report.vaulted.append((pending.channel_type, pending.field_name, pending.vault_key))

    _log_absorbed(agent, report)
    return report


def reference_for(vault_key: str) -> str:
    """The config value that stands in for a vaulted credential."""
    return f"${{vault:{vault_key}}}"


def pending_channel_secrets(data: dict[str, Any]) -> list[PendingSecret]:
    """Every literal channel credential in a parsed config, in file order."""
    return [pending for _, pending in _pending_with_owners(data)]


def absorb_channel_secrets_in_text(
    agent: str,
    content: str,
    parsed: dict[str, Any],
    vault: Any = None,
) -> tuple[str, AbsorbReport]:
    """Absorb literals out of raw ``agent.json5`` *text*, preserving formatting.

    The raw editor writes the user's bytes verbatim so comments and key order
    survive, which rules out the parse→mutate→re-serialize path used everywhere
    else. Instead the credential's whole ``"token": "…",`` line is deleted from
    the text — a targeted edit that leaves the rest of the file exactly as
    typed. JSON5 permits trailing commas, so removing a line never strands the
    one before it.

    A field that isn't on a line of its own (exotic formatting or escaping) is
    left alone and reported, rather than vaulted into a file that still
    contains it.
    """
    report = AbsorbReport()
    pendings = pending_channel_secrets(parsed)
    if not pendings:
        return content, report

    literals = [p for p in pendings if p.literal]
    if vault is None and literals:
        vault = _resolve_vault()
    if literals and vault is None:
        report.vault_available = False
        report.left_in_config = [(p.channel_type, p.field_name) for p in literals]
        _warn_unavailable(agent, len(literals))
        # Redundant references still have nothing to lose — keep going for those.
        pendings = [p for p in pendings if not p.literal]

    for pending in pendings:
        removed = _remove_field_line(content, pending.field_name, pending.value)
        if removed is None:
            report.left_in_config.append((pending.channel_type, pending.field_name))
            logger.warning(
                "Could not locate %s.%s on its own line in the raw config text for agent "
                "'%s' — leaving it as typed.",
                pending.channel_type,
                pending.field_name,
                agent,
            )
            continue
        if pending.literal and not _store(vault, agent, pending):
            report.left_in_config.append((pending.channel_type, pending.field_name))
            continue
        content = removed
        bucket = report.vaulted if pending.literal else report.stripped
        bucket.append((pending.channel_type, pending.field_name, pending.vault_key))

    _log_absorbed(agent, report)
    return content, report


def _pending_with_owners(data: dict[str, Any]) -> list[tuple[dict[str, Any], PendingSecret]]:
    """Credential fields to remove, paired with the channel dict each came from."""
    channels = data.get("channels")
    if not isinstance(channels, list):
        return []

    found: list[tuple[dict[str, Any], PendingSecret]] = []
    for entry in channels:
        if not isinstance(entry, dict):
            continue  # a bare "telegram" string carries no credential
        ch_type = str(entry.get("type") or "")
        for field_name, value in list(entry.items()):
            if field_name == "type" or not isinstance(value, str) or not value:
                continue
            key = canonical_secret_key(ch_type, field_name)
            if key is None:
                continue
            if is_reference(value):
                # Redundant only when it names this field's own canonical key;
                # a pointer at some other key is the user's deliberate choice.
                if is_redundant_reference(value, key):
                    found.append((entry, PendingSecret(ch_type, field_name, key, value, False)))
                continue
            found.append((entry, PendingSecret(ch_type, field_name, key, value)))
    return found


def _remove_field_line(content: str, field_name: str, value: str) -> str | None:
    """Delete the ``"field": "value"`` pair from the raw text, or None if it can't.

    Two shapes, tried in order: the field alone on its own line (the formatted
    case — the whole line goes, leaving no blank behind), then the field inline
    among others (``{"type": "telegram", "token": "…"}`` — only the pair goes).
    JSON5 allows single or double quotes and trailing commas, so either quote
    style matches and neither removal can strand the surrounding punctuation.

    The result is re-parsed before it is returned: an edit that would leave the
    file unloadable is discarded rather than written.
    """
    q = "[\"']"
    key = rf"{q}?{re.escape(field_name)}{q}?[ \t]*:[ \t]*{q}{re.escape(value)}{q}"
    own_line = re.compile(rf"^[ \t]*{key}[ \t]*,?[ \t]*\r?\n", re.MULTILINE)
    inline = re.compile(rf"{key}[ \t]*,?[ \t]*")

    for pattern in (own_line, inline):
        candidate, count = pattern.subn("", content, count=1)
        if count == 0:
            continue
        if _parses_as_json5(candidate):
            return candidate
        logger.warning(
            "Removing '%s' from the raw config text left it unparseable — leaving it as typed.",
            field_name,
        )
    return None


def _parses_as_json5(content: str) -> bool:
    from olano.config.loader import _load_json5

    try:
        _load_json5(content)
    except Exception:
        return False
    return True


def _store(vault: Any, agent: str, pending: PendingSecret) -> bool:
    """Write one credential as a per-agent override. False if the vault refused."""
    try:
        vault.set(
            name=pending.vault_key,
            value=pending.value,
            description=f"{pending.channel_type} {pending.field_name} for agent '{agent}'",
            agent=agent,
        )
    except Exception as exc:
        logger.warning(
            "Could not vault %s for agent '%s' (%s) — leaving it in agent.json5.",
            pending.vault_key,
            agent,
            exc,
        )
        return False
    return True


def _warn_unavailable(agent: str, count: int) -> None:
    logger.warning(
        "Vault unavailable — %d channel secret(s) for agent '%s' stay in agent.json5 "
        "as plain text. Unlock the vault and re-save to secure them.",
        count,
        agent,
    )


def _log_absorbed(agent: str, report: AbsorbReport) -> None:
    if report.vaulted:
        logger.info(
            "Vaulted %d channel secret(s) for agent '%s': %s",
            len(report.vaulted),
            agent,
            ", ".join(sorted({k for _, _, k in report.vaulted})),
        )
    if report.stripped:
        logger.info(
            "Dropped %d redundant channel credential reference(s) from agent '%s': %s "
            "(resolved from the vault by convention at boot)",
            len(report.stripped),
            agent,
            ", ".join(sorted({k for _, _, k in report.stripped})),
        )
