"""Home config (``config.yaml``) read + safe partial patch.

Wraps :mod:`olano.config.home` and the block setters in
:mod:`olano.config.system_settings`. ``update_home_config`` patches the
recognised :class:`~olano.config.home.HomeConfig` root fields *and* the named
settings blocks (``speech:``, ``cognition:``, ``disk_monitor:``, …), each
routed through its validated writer so what lands on disk carries the same
clamping/coercion guarantees a dashboard save gets. Unknown top-level keys are
preserved on disk and **reported** in the result's ``ignored`` list — the old
wrapper echoed every requested key as "updated" even when nothing was written,
which is exactly the kind of silent success an agent then repeats forever.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from olano.config import system_settings as ss
from olano.config.home import (
    PATCHABLE_HOME_KEYS,
    get_olano_paths,
    load_home_config,
)
from olano.config.home import update_home_config as _update_home_config

# Root keys that only take effect after a runtime restart (mirrors the
# dashboard's GET /api/system/config `restart_required` list).
_RESTART_REQUIRED = ("runtime_api_port", "dashboard_port", "multi_user_enabled")

_UNKNOWN_KEY_REASON = (
    "unknown config.yaml key — writable root keys and blocks are listed under "
    "'writable' in get_home_config()"
)


def _unknown_subkeys(section: str, value: dict, allowed: set[str]) -> list[dict[str, str]]:
    return [{"key": f"{section}.{k}", "reason": "unknown field"} for k in value if k not in allowed]


def _merge_writer(
    section: str, model: Any, setter: Callable[[dict], Any]
) -> Callable[[dict], tuple[bool, list[dict[str, str]]]]:
    """A writer for the plain merge-and-round-trip blocks: known fields go to
    the block's ``set_*`` (which validates through ``model``), unknown fields
    are reported rather than silently dropped."""

    def _apply(value: dict) -> tuple[bool, list[dict[str, str]]]:
        allowed = set(model.model_fields)
        known = {k: v for k, v in value.items() if k in allowed}
        ignored = _unknown_subkeys(section, value, allowed)
        if known:
            setter(known)
        return bool(known), ignored

    return _apply


def _apply_speech(value: dict) -> tuple[bool, list[dict[str, str]]]:
    ignored = _unknown_subkeys("speech", value, {"stt", "tts"})
    sections: dict[str, dict] = {}
    for name in ("stt", "tts"):
        sec = value.get(name)
        if sec is None:
            continue
        if not isinstance(sec, dict):
            ignored.append(
                {"key": f"speech.{name}", "reason": "must be an object of speech-service fields"}
            )
            continue
        provider = sec.get("provider")
        if provider is not None and provider not in ss.SPEECH_PROVIDERS:
            # The model would coerce an unknown provider to "disabled"; refuse
            # instead so a typo can't silently switch a service off.
            ignored.append(
                {
                    "key": f"speech.{name}.provider",
                    "reason": f"must be one of: {', '.join(ss.SPEECH_PROVIDERS)}",
                }
            )
            continue
        sections[name] = sec
    if sections:
        ss.set_speech_settings(sections.get("stt"), sections.get("tts"))
    return bool(sections), ignored


def _apply_media(value: dict) -> tuple[bool, list[dict[str, str]]]:
    allowed = {"vision", "image"}
    ignored = _unknown_subkeys("media", value, allowed)
    known = {k: value[k] for k in allowed if k in value}
    if known:
        ss.set_media_settings(known.get("vision"), known.get("image"))
    return bool(known), ignored


def _apply_web_search(value: dict) -> tuple[bool, list[dict[str, str]]]:
    ignored = _unknown_subkeys("web_search", value, {"provider"})
    if "provider" not in value:
        return False, ignored
    provider = value["provider"]
    if provider is not None and str(provider).strip().lower() not in ss.WEB_SEARCH_SOURCES:
        # The model would coerce an unknown value to "" (auto) — the unsafe
        # direction for a typo'd "disabled". Refuse instead.
        sources = ", ".join(repr(s) for s in ss.WEB_SEARCH_SOURCES)
        ignored.append({"key": "web_search.provider", "reason": f"must be one of: {sources}"})
        return False, ignored
    ss.set_web_search_settings(provider)
    return True, ignored


def _apply_rate_limits(value: dict) -> tuple[bool, list[dict[str, str]]]:
    ignored = _unknown_subkeys("rate_limits", value, {"messages", "per_platform"})
    messages = value.get("messages")
    per_platform = value.get("per_platform")
    for key, val in (("messages", messages), ("per_platform", per_platform)):
        if val is not None and not isinstance(val, dict):
            ignored.append({"key": f"rate_limits.{key}", "reason": "must be an object"})
            return False, ignored
    if messages is None and per_platform is None:
        return False, ignored
    ss.set_rate_limits(messages, per_platform)
    return True, ignored


def _apply_sandbox(value: dict) -> tuple[bool, list[dict[str, str]]]:
    # `host`/`effective`/`env_overrides` are read-only derived fields the
    # dashboard GET emits; drop them silently so a round-tripped object works.
    derived = {"host", "effective", "env_overrides"}
    allowed = set(ss.SandboxSettings.model_fields)
    ignored = _unknown_subkeys("sandbox", value, allowed | derived)
    known: dict[str, Any] = {}
    for k in allowed:
        if k not in value:
            continue
        v = value[k]
        known[k] = None if v in ("", None) else v  # empty clears back to auto-sizing
    if known:
        ss.set_sandbox_settings(known)
    return bool(known), ignored


def _apply_magic_links(value: dict) -> tuple[bool, list[dict[str, str]]]:
    derived = {"available", "unavailable_reason"}
    allowed = set(ss.MagicLinkSettings.model_fields)
    ignored = _unknown_subkeys("magic_links", value, allowed | derived)
    known = {k: v for k, v in value.items() if k in allowed}
    if "pin" in known and str(known["pin"]) not in ("required", "off"):
        ignored.append({"key": "magic_links.pin", "reason": "must be 'required' or 'off'"})
        known.pop("pin")
    if known:
        ss.set_magic_link_settings(known)
    return bool(known), ignored


def _apply_usage_budgets(value: dict) -> tuple[bool, list[dict[str, str]]]:
    ignored = [
        {
            "key": f"usage_budgets.{k}",
            "reason": (
                "cost budgets come from the deployment's signed settings and are "
                "not settable on this box (token budgets are)"
            ),
        }
        for k in value
        if k.startswith("cost_")
    ]
    allowed = set(ss.UsageBudgetSettings.model_fields)
    ignored += _unknown_subkeys(
        "usage_budgets", {k: v for k, v in value.items() if not k.startswith("cost_")}, allowed
    )
    known = {k: v for k, v in value.items() if k in allowed and not k.startswith("cost_")}
    if known:
        ss.set_usage_budget_settings(known)
    return bool(known), ignored


def _apply_playground(value: dict) -> tuple[bool, list[dict[str, str]]]:
    ignored = _unknown_subkeys("playground", value, {"enabled"})
    if "enabled" not in value:
        return False, ignored
    ss.set_playground_enabled(bool(value["enabled"]))
    return True, ignored


def _apply_credits(value: dict) -> tuple[bool, list[dict[str, str]]]:
    # Only `display` is writable — the rate and unit names are catalog data.
    ignored = [
        {"key": f"credits.{k}", "reason": "read-only (synced from the cloud catalog)"}
        for k in value
        if k != "display"
    ]
    if "display" not in value:
        return False, ignored
    if ss.cost_display_locked():
        ignored.append(
            {
                "key": "credits.display",
                "reason": (
                    "the cost display unit is set by this deployment's plan at "
                    "cloud.olano.ai and cannot be changed from the box"
                ),
            }
        )
        return False, ignored
    try:
        ss.set_cost_display_mode(str(value["display"] or ""))
    except ValueError as exc:
        ignored.append({"key": "credits.display", "reason": str(exc)})
        return False, ignored
    return True, ignored


def _apply_default_models(value: dict) -> tuple[bool, list[dict[str, str]]]:
    wrote = False
    ignored: list[dict[str, str]] = []
    for tier, target in value.items():
        if tier not in ss.DEFAULT_MODEL_TIERS:
            ignored.append(
                {
                    "key": f"default_models.{tier}",
                    "reason": f"unknown tier (known: {', '.join(ss.DEFAULT_MODEL_TIERS)})",
                }
            )
            continue
        try:
            ss.set_default_model(tier, str(target or "").strip() or None)
            wrote = True
        except ValueError as exc:
            ignored.append({"key": f"default_models.{tier}", "reason": str(exc)})
    return wrote, ignored


def _apply_cognition(value: dict) -> tuple[bool, list[dict[str, str]]]:
    handled = {
        "fast_model",
        "deep_model",
        "enabled",
        "autonomous",
        "auto_approve",
        "intensity",
        "worker_max_iterations",
        "worker_timeout_seconds",
        "features",
    }
    wrote = False
    ignored = _unknown_subkeys("cognition", value, handled)
    for which in ("fast_model", "deep_model"):
        if which in value:
            raw = value[which]
            ss.set_cognition_model(which, (str(raw).strip() or None) if raw else None)
            wrote = True
    if "enabled" in value:
        ss.set_cortex_master(bool(value["enabled"]))
        wrote = True
    if "autonomous" in value:
        ss.set_autonomy(bool(value["autonomous"]))
        wrote = True
    if "auto_approve" in value:
        ss.set_auto_approve(bool(value["auto_approve"]))
        wrote = True
    if "intensity" in value:
        try:
            ss.set_intensity(str(value["intensity"]))
            wrote = True
        except ValueError as exc:
            ignored.append({"key": "cognition.intensity", "reason": str(exc)})
    if "worker_max_iterations" in value or "worker_timeout_seconds" in value:
        ss.set_worker_budget(
            value.get("worker_max_iterations"), value.get("worker_timeout_seconds")
        )
        wrote = True
    features = value.get("features")
    if isinstance(features, dict):
        for feat, on in features.items():
            try:
                ss.set_cognition_feature(str(feat), bool(on))
                wrote = True
            except ValueError as exc:
                ignored.append({"key": f"cognition.features.{feat}", "reason": str(exc)})
    elif "features" in value:
        ignored.append({"key": "cognition.features", "reason": "must be an object of toggles"})
    return wrote, ignored


def _apply_browser(value: dict) -> tuple[bool, list[dict[str, str]]]:
    """The ``browser`` block, with the same enable checks as the dashboard PUT:
    turning the browser on (or onto a heavier mode) is refused when the plan,
    the hardware or the free memory cannot carry it, and live starts the
    image pull. Running sessions of the old kind are closed by the session
    sweep within a minute."""
    from olano.browser.policy import enable_refusal

    before = ss.load_browser_settings()
    merged = ss.BrowserSettings.model_validate({**before.model_dump(), **value})
    if merged.mode != "off" and (merged.mode, merged.provider) != (before.mode, before.provider):
        refusal = enable_refusal(merged.mode, merged.provider)
        if refusal:
            return False, [{"key": "browser.mode", "reason": refusal}]
    allowed = set(ss.BrowserSettings.model_fields)
    ignored = [{"key": f"browser.{k}", "reason": "unknown key"} for k in value if k not in allowed]
    ss.set_browser_settings({k: v for k, v in value.items() if k in allowed})
    if merged.mode == "live" and merged.provider == "local":
        from olano.browser import sidecar

        sidecar.start_pull()
    return True, ignored


#: block name → writer(value) -> (wrote_something, ignored entries). One entry
#: per config.yaml settings block; the routing mirrors PUT /api/system/config.
_SECTION_WRITERS: dict[str, Callable[[dict], tuple[bool, list[dict[str, str]]]]] = {
    "speech": _apply_speech,
    "media": _apply_media,
    "web_search": _apply_web_search,
    "rate_limits": _apply_rate_limits,
    "sandbox": _apply_sandbox,
    "magic_links": _apply_magic_links,
    "usage_budgets": _apply_usage_budgets,
    "playground": _apply_playground,
    "credits": _apply_credits,
    "default_models": _apply_default_models,
    "cognition": _apply_cognition,
    "agent_autonomy": _merge_writer(
        "agent_autonomy", ss.AgentAutonomySettings, ss.set_agent_autonomy_settings
    ),
    "self_serve_agents": _merge_writer(
        "self_serve_agents", ss.SelfServeAgentSettings, ss.set_self_serve_agent_settings
    ),
    "disk_monitor": _merge_writer(
        "disk_monitor", ss.DiskMonitorSettings, ss.set_disk_monitor_settings
    ),
    "catalog_sync": _merge_writer(
        "catalog_sync", ss.CatalogSyncSettings, ss.set_catalog_sync_settings
    ),
    "prompt_refresh": _merge_writer(
        "prompt_refresh", ss.PromptRefreshSettings, ss.set_prompt_refresh_settings
    ),
    "mcp_health": _merge_writer("mcp_health", ss.McpHealthSettings, ss.set_mcp_health_settings),
    "peer_threads": _merge_writer(
        "peer_threads", ss.PeerThreadSettings, ss.set_peer_thread_settings
    ),
    "scheduler": _merge_writer("scheduler", ss.SchedulerSettings, ss.set_scheduler_settings),
    "checkpoint_gc": _merge_writer(
        "checkpoint_gc", ss.CheckpointGcSettings, ss.set_checkpoint_gc_settings
    ),
    "pages": _merge_writer("pages", ss.PagesSettings, ss.set_pages_settings),
    "browser": _apply_browser,
    "groups": _merge_writer("groups", ss.GroupThreadSettings, ss.set_group_thread_settings),
    "issues": _merge_writer("issues", ss.IssuesSettings, ss.set_issues_settings),
    "revisions": _merge_writer("revisions", ss.RevisionsSettings, ss.set_revisions_settings),
    "heartbeat": _merge_writer("heartbeat", ss.HeartbeatSettings, ss.set_heartbeat_settings),
}


def _config_blocks() -> dict[str, Any]:
    """Resolved per-block settings, defaults applied — what the runtime actually
    enforces, not just what the file says. Every loader routes through the
    parse-cached ``read_home_config_doc``, so this is one stat, zero parses."""
    speech = ss.load_speech_settings()
    rate_limits = ss.load_rate_limit_settings()
    return {
        "speech": {"stt": speech.stt.model_dump(), "tts": speech.tts.model_dump()},
        "media": ss.load_media_settings().model_dump(),
        "web_search": ss.load_web_search_settings().model_dump(),
        "rate_limits": {
            "messages": rate_limits.messages.model_dump(exclude_none=True),
            "per_platform": {
                name: win.model_dump(exclude_none=True)
                for name, win in rate_limits.per_platform.items()
            },
        },
        "sandbox": ss.load_sandbox_settings().model_dump(),
        "magic_links": ss.load_magic_link_settings().model_dump(),
        "usage_budgets": ss.load_usage_budget_settings().model_dump(),
        "playground": ss.load_playground_settings().model_dump(),
        "credits": {
            "display": ss.cost_display_mode(),
            "display_locked": ss.cost_display_locked(),
        },
        "default_models": ss.default_models_report(),
        "cognition": ss.load_system_settings().model_dump(),
        "agent_autonomy": ss.load_agent_autonomy_settings().model_dump(),
        "self_serve_agents": ss.load_self_serve_agent_settings().model_dump(),
        "disk_monitor": ss.load_disk_monitor_settings().model_dump(),
        "catalog_sync": ss.load_catalog_sync_settings().model_dump(),
        "prompt_refresh": ss.load_prompt_refresh_settings().model_dump(),
        "mcp_health": ss.load_mcp_health_settings().model_dump(),
        "peer_threads": ss.load_peer_thread_settings().model_dump(),
        "scheduler": ss.load_scheduler_settings().model_dump(),
        "checkpoint_gc": ss.load_checkpoint_gc_settings().model_dump(),
        "pages": ss.load_pages_settings().model_dump(),
        "browser": ss.load_browser_settings().model_dump(),
        "groups": ss.load_group_thread_settings().model_dump(),
        "issues": ss.load_issues_settings().model_dump(),
        "revisions": ss.load_revisions_settings().model_dump(),
        "heartbeat": ss.load_heartbeat_settings().model_dump(),
    }


def get_home_config() -> dict[str, Any]:
    """Return the current home config plus the resolved config.yaml path.

    Root ``HomeConfig`` fields stay at the top level (back-compat); ``blocks``
    carries every settings block with defaults resolved, ``writable`` names
    what :func:`update_home_config` can patch, and ``restart_required`` the
    root keys that only apply after a runtime restart.
    """
    cfg = load_home_config()
    data = asdict(cfg)
    data["_path"] = str(get_olano_paths().config_path)
    data["blocks"] = _config_blocks()
    data["writable"] = {
        "root": sorted(PATCHABLE_HOME_KEYS),
        "blocks": sorted(_SECTION_WRITERS),
    }
    data["restart_required"] = list(_RESTART_REQUIRED)
    return data


def update_home_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Patch root home-config keys and settings blocks, preserving all other keys.

    Root keys merge via :func:`olano.config.home.update_home_config`; each
    block value must be a partial object and routes to that block's validated
    writer. The result reports honestly: ``applied`` lists the top-level keys
    that were actually written, ``ignored`` every requested key (or sub-key)
    that was not, each with a reason — never a success echo for a no-op.
    """
    applied: list[str] = []
    ignored: list[dict[str, str]] = []
    root_updates: dict[str, Any] = {}

    for key, value in (updates or {}).items():
        if key in PATCHABLE_HOME_KEYS:
            root_updates[key] = value
            continue
        writer = _SECTION_WRITERS.get(key)
        if writer is None:
            ignored.append({"key": key, "reason": _UNKNOWN_KEY_REASON})
            continue
        if not isinstance(value, dict):
            ignored.append(
                {"key": key, "reason": "config.yaml block — pass an object of its fields"}
            )
            continue
        try:
            wrote, sub_ignored = writer(value)
        except ValueError as exc:
            ignored.append({"key": key, "reason": str(exc)})
            continue
        ignored.extend(sub_ignored)
        if wrote:
            applied.append(key)

    if root_updates:
        _update_home_config(root_updates)
        applied.extend(root_updates)

    applied_sorted = sorted(applied)
    return {
        "applied": applied_sorted,
        "ignored": ignored,
        # Back-compat alias for readers of the old shape — now honest.
        "updated": applied_sorted,
        "restart_required": [k for k in applied_sorted if k in _RESTART_REQUIRED],
        "config": get_home_config(),
    }
