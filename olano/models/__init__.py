"""
Model catalog and discovery for olano.

Supports all LangChain-native providers via init_chat_model("provider:model").
For Ollama, discovers locally pulled models via `ollama list`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from langchain_core.language_models import BaseChatModel

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ── Provider catalog ──────────────────────────────────────────────────────────


@dataclass
class ProviderInfo:
    key: str  # prefix used in "provider:model" strings
    name: str  # human-readable name
    package: str  # pip package to install
    env_var: str | None  # API key env var (None = no key needed)
    default_models: list[str] = field(default_factory=list)  # suggested models
    notes: str = ""


PROVIDERS: dict[str, ProviderInfo] = {
    # Olano-managed tiers. Not a provider the customer holds a key for — the
    # "models" here are service levels Olano runs, reached through the cloud
    # gateway with this box's deployment token and billed in olano credits.
    # `env_var=None` because the credential is the deployment token, not a
    # provider key: shipping a provider key to customer-controlled hardware is
    # precisely what the gateway exists to avoid (models/olano_gateway.py).
    # Listed first so it leads the model pickers. `default_models` is every
    # tier that RESOLVES; what gets offered comes from get_provider_models(),
    # which drops olano_gateway.HIDDEN_TIERS (today: `auto`).
    "olano": ProviderInfo(
        key="olano",
        name="Olano (managed)",
        package="langchain-openai",  # the gateway is OpenAI-compatible
        env_var=None,
        default_models=["auto", "turbo", "pro", "max"],
        notes="Billed in olano credits. Needs a deployment connected to cloud.olano.ai.",
    ),
    "anthropic": ProviderInfo(
        key="anthropic",
        name="Anthropic",
        package="langchain-anthropic",
        env_var="ANTHROPIC_API_KEY",
        default_models=[
            "claude-opus-4-6",
            "claude-opus-5-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-sonnet-4",
            "claude-3-7-sonnet",
        ],
    ),
    "openai": ProviderInfo(
        key="openai",
        name="OpenAI",
        package="langchain-openai",
        env_var="OPENAI_API_KEY",
        default_models=[
            "gpt-5.4",
            "gpt-6-luna",
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.4-pro",
            "gpt-5.3-codex",
        ],
        notes="deepagents enables use_responses_api=True automatically for OpenAI",
    ),
    "openai-codex": ProviderInfo(
        key="openai-codex",
        name="OpenAI Codex (ChatGPT OAuth)",
        package="langchain-openai",
        env_var=None,
        default_models=[
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
            "gpt-5.5",
            "gpt-5.4-mini",
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
        ],
        notes="Connect from the dashboard or run `olano auth openai-codex`; uses ChatGPT/Codex OAuth, not OPENAI_API_KEY",
    ),
    "google_genai": ProviderInfo(
        key="google_genai",
        name="Google Gemini (GenAI)",
        package="langchain-google-genai",
        env_var="GOOGLE_API_KEY",
        default_models=[
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-pro",
        ],
    ),
    "google_vertexai": ProviderInfo(
        key="google_vertexai",
        name="Google Vertex AI",
        package="langchain-google-vertexai",
        env_var="GOOGLE_APPLICATION_CREDENTIALS",
        default_models=[
            "gemini-2.0-flash",
            "gemini-2.5-pro",
            "gemini-1.5-pro",
        ],
        notes="Requires gcloud authentication",
    ),
    "ollama": ProviderInfo(
        key="ollama",
        name="Ollama (local)",
        package="langchain-ollama",
        env_var=None,
        default_models=[],  # populated dynamically via ollama list
        notes="Requires Ollama running locally (https://ollama.com)",
    ),
    "groq": ProviderInfo(
        key="groq",
        name="Groq",
        package="langchain-groq",
        env_var="GROQ_API_KEY",
        default_models=[
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ],
    ),
    "mistralai": ProviderInfo(
        key="mistralai",
        name="Mistral AI",
        package="langchain-mistralai",
        env_var="MISTRAL_API_KEY",
        default_models=[
            "mistral-large-latest",
            "mistral-small-latest",
            "codestral-latest",
            "mistral-nemo",
        ],
    ),
    "deepseek": ProviderInfo(
        key="deepseek",
        name="DeepSeek",
        package="langchain-deepseek",
        env_var="DEEPSEEK_API_KEY",
        default_models=[
            "deepseek-flash",  # V4.1 Flash (2026-09-10; 1017 update 2026-10-17)
            "deepseek-v4-pro",
            "deepseek-v4-flash",  # retired 2026-09-10, DeepSeek serves deepseek-flash
            "deepseek-chat",  # deprecated 2026-07-24
            "deepseek-reasoner",  # deprecated 2026-07-24
        ],
    ),
    "xai": ProviderInfo(
        key="xai",
        name="xAI (Grok)",
        package="langchain-xai",
        env_var="XAI_API_KEY",
        default_models=[
            "grok-3",
            "grok-3-mini",
            "grok-2",
        ],
    ),
    "qwq": ProviderInfo(
        key="qwq",
        name="Qwen (Alibaba DashScope)",
        package="langchain-qwq",
        env_var="DASHSCOPE_API_KEY",
        default_models=[
            "qwq-plus",
            "qwen3-235b-a22b",
            "qwen3-32b",
            "qwen-max",
            "qwen-plus",
        ],
        notes="QwQ reasoning + Qwen3 series via Alibaba DashScope. `qwq*` model "
        "names route to ChatQwQ, everything else to ChatQwen. Set "
        "DASHSCOPE_API_BASE for the international endpoint.",
    ),
    "moonshot": ProviderInfo(
        key="moonshot",
        name="Moonshot AI (Kimi)",
        package="langchain-moonshot",
        env_var="MOONSHOT_API_KEY",
        default_models=[
            "kimi-k2.5-turbo",
            "kimi-k2.5",
            "kimi-latest",
            "moonshot-v1-128k",
            "moonshot-v1-32k",
        ],
        notes="Kimi K2 series. Defaults to api.moonshot.ai; set "
        "MOONSHOT_API_BASE=https://api.moonshot.cn/v1 for the China endpoint.",
    ),
    "openrouter": ProviderInfo(
        key="openrouter",
        name="OpenRouter",
        package="langchain-openrouter",
        env_var="OPENROUTER_API_KEY",
        default_models=[
            "moonshotai/kimi-k2",
            "meta-llama/llama-4-maverick",
            "google/gemini-2.5-pro",
            "anthropic/claude-sonnet-4-6",
        ],
        notes="Access to 200+ models via a single API key",
    ),
    "cohere": ProviderInfo(
        key="cohere",
        name="Cohere",
        package="langchain-cohere",
        env_var="COHERE_API_KEY",
        default_models=[
            "command-r-plus",
            "command-r",
            "command-light",
        ],
    ),
    "fireworks": ProviderInfo(
        key="fireworks",
        name="Fireworks AI",
        package="langchain-fireworks",
        env_var="FIREWORKS_API_KEY",
        default_models=[
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/deepseek-r1",
        ],
    ),
    "together": ProviderInfo(
        key="together",
        name="Together AI",
        package="langchain-together",
        env_var="TOGETHER_API_KEY",
        default_models=[
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ],
    ),
    "huggingface": ProviderInfo(
        key="huggingface",
        name="Hugging Face",
        package="langchain-huggingface",
        env_var="HUGGINGFACEHUB_API_TOKEN",
        default_models=[
            "meta-llama/Llama-3.3-70B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
        ],
        notes="Serverless HF Inference API (torch-free). Set HUGGINGFACEHUB_ENDPOINT_URL "
        "for a dedicated TGI server; `langchain-huggingface[full]` adds local models.",
    ),
    "azure_openai": ProviderInfo(
        key="azure_openai",
        name="Azure OpenAI",
        package="langchain-openai",
        env_var="AZURE_OPENAI_API_KEY",
        default_models=["gpt-4o", "gpt-4o-mini"],
        notes="Also requires AZURE_OPENAI_ENDPOINT",
    ),
    "bedrock": ProviderInfo(
        key="bedrock",
        name="AWS Bedrock",
        package="langchain-aws",
        env_var="AWS_ACCESS_KEY_ID",
        default_models=[
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "amazon.nova-pro-v1:0",
            "meta.llama3-70b-instruct-v1:0",
        ],
        notes="Also requires AWS_SECRET_ACCESS_KEY and AWS_DEFAULT_REGION",
    ),
    "nvidia": ProviderInfo(
        key="nvidia",
        name="NVIDIA NIM",
        package="langchain-nvidia-ai-endpoints",
        env_var="NVIDIA_API_KEY",
        default_models=[
            "meta/llama-3.1-70b-instruct",
            "nvidia/nemotron-4-340b-instruct",
        ],
    ),
    "perplexity": ProviderInfo(
        key="perplexity",
        name="Perplexity",
        package="langchain-perplexity",
        env_var="PPLX_API_KEY",
        default_models=[
            "sonar-pro",
            "sonar",
            "sonar-reasoning",
        ],
    ),
    "litellm": ProviderInfo(
        key="litellm",
        name="LiteLLM (proxy)",
        package="langchain-litellm",
        env_var=None,
        default_models=[],
        notes="Universal proxy — set LITELLM_BASE_URL",
    ),
    "vercel": ProviderInfo(
        key="vercel",
        name="Vercel AI Gateway",
        package="langchain-openai",  # OpenAI-compatible endpoint
        env_var="AI_GATEWAY_API_KEY",
        default_models=[
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-opus-4-6",
            "openai/gpt-5.4",
            "google/gemini-2.5-pro",
            "xai/grok-3",
        ],
        notes="OpenAI-compatible gateway — routes to 100+ models via one key. "
        "Override the endpoint with AI_GATEWAY_BASE_URL.",
    ),
}

_OLLAMA_SUGGESTED_MODELS = [
    "qwen3:8b",
    "kimi-k2.5:cloud",
    "glm-5:cloud",
    "minimax-m2.7:cloud",
    "gemma4:31b-cloud",
]


# ── Ollama model discovery ────────────────────────────────────────────────────


def list_ollama_models() -> list[str]:
    """
    Query the local Ollama instance for available (pulled) models.
    Returns a list of model name strings like ["llama3.2:latest", "mistral:latest"].
    Returns [] if Ollama is not installed or not running.
    """
    if not shutil.which("ollama"):
        return []
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        models = []
        for line in result.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            if parts:
                models.append(parts[0])  # first column is NAME
        return models
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return []


def get_provider_models(provider_key: str) -> list[str]:
    """Get models for a provider, with live Ollama discovery."""
    if provider_key == "ollama":
        local = list_ollama_models()
        if local:
            return local
        return list(_OLLAMA_SUGGESTED_MODELS)
    if provider_key == "openai-codex":
        from olano.models.codex_auth import get_codex_model_ids

        return get_codex_model_ids()
    if provider_key == "olano":
        # Retired tiers stay in OLANO_TIERS (agents configured with them keep
        # resolving) but are never *offered* — see olano_gateway.HIDDEN_TIERS.
        from olano.models.olano_gateway import offered_tiers

        return list(offered_tiers())
    info = PROVIDERS.get(provider_key)
    return info.default_models if info else []


# ── Model string parsing ──────────────────────────────────────────────────────


def parse_model_string(model_str: str) -> tuple[str | None, str]:
    """
    Parse "provider:model" → (provider, model).
    "anthropic:claude-sonnet-4-6" → ("anthropic", "claude-sonnet-4-6")
    "gpt-4o" → (None, "gpt-4o")
    """
    if ":" in model_str:
        provider, _, model = model_str.partition(":")
        if provider in PROVIDERS:
            return provider, model
    return None, model_str


def is_provider_available(provider_key: str) -> bool:
    """Check if the provider's Python package is installed (without importing it)."""
    import importlib.util

    info = PROVIDERS.get(provider_key)
    if not info:
        return False
    pkg = info.package.replace("-", "_")
    return importlib.util.find_spec(pkg) is not None


# Providers that are known to fail when actually imported (e.g. proto conflicts).
# We keep them off the strict-import path so the rest of the catalog stays usable.
_PROVIDER_STRICT_SKIP = {"fireworks"}

# Cache of (status, detail) per provider key so we don't re-import on every call.
# Keys removed only via reset_provider_health().
_PROVIDER_HEALTH_CACHE: dict[str, tuple[str, str]] = {}


def reset_provider_health() -> None:
    """Drop cached provider health diagnostics (used by tests)."""
    _PROVIDER_HEALTH_CACHE.clear()


def check_provider_health(provider_key: str) -> tuple[str, str]:
    """Return ``(status, detail)`` for a provider.

    Statuses:
        ok        — package importable and (if applicable) API key present.
        missing   — package not installed.
        broken    — package present but raises on import.
        no_key    — package OK but env var unset (still returns "ok"-ish for
                    Ollama / LiteLLM since they don't require a key).
        unknown   — provider key not registered.
        skipped   — strict import deliberately skipped (proto conflict, etc.).

    Result is cached; call :func:`reset_provider_health` to recompute.
    """
    info = PROVIDERS.get(provider_key)
    if info is None:
        return ("unknown", f"no provider entry for {provider_key!r}")

    cached = _PROVIDER_HEALTH_CACHE.get(provider_key)
    if cached is not None:
        return cached

    pkg = info.package.replace("-", "_")

    import importlib
    import importlib.util

    spec = importlib.util.find_spec(pkg)
    if spec is None:
        result = ("missing", f"`{info.package}` not installed (uv add {info.package})")
        _PROVIDER_HEALTH_CACHE[provider_key] = result
        return result

    if provider_key in _PROVIDER_STRICT_SKIP:
        result = (
            "skipped",
            f"strict import skipped for {provider_key} (known import-time conflict)",
        )
        _PROVIDER_HEALTH_CACHE[provider_key] = result
        return result

    try:
        importlib.import_module(pkg)
    except BaseException as exc:
        # Don't crash; surface the diagnostic so the caller can present a
        # structured "provider unavailable" message instead of a stack trace
        # at first model call.
        result = ("broken", f"{type(exc).__name__}: {exc}")
        _PROVIDER_HEALTH_CACHE[provider_key] = result
        logger.warning("provider %s installed but failed to import: %s", provider_key, exc)
        return result

    if info.env_var and not os.environ.get(info.env_var):
        result = ("no_key", f"{info.env_var} not set")
        _PROVIDER_HEALTH_CACHE[provider_key] = result
        return result

    result = ("ok", "ready")
    _PROVIDER_HEALTH_CACHE[provider_key] = result
    return result


def provider_health_report() -> dict[str, dict[str, str]]:
    """Return a structured snapshot of every registered provider's status.

    Suitable for boot diagnostics or an ``olano doctor`` command.
    """
    report: dict[str, dict[str, str]] = {}
    for key, info in PROVIDERS.items():
        status, detail = check_provider_health(key)
        report[key] = {
            "name": info.name,
            "package": info.package,
            "env_var": info.env_var or "",
            "status": status,
            "detail": detail,
        }
    return report


def _can_auto_unlock_vault() -> bool:
    from olano.vault.store import vault_key_from_env

    if vault_key_from_env():
        return True
    # Only a SECURE OS keyring backend counts (see vault.keyring_backend) — a
    # headless box with no secure backend must rely on OLANO_VAULT_KEY.
    from olano.vault.keyring_backend import secure_keyring_available

    return secure_keyring_available()


def _hydrate_provider_env_from_vault(provider_key: str) -> None:
    info = PROVIDERS.get(provider_key)
    env_var = info.env_var if info else None
    if not env_var or os.environ.get(env_var):
        return

    # Try the already-unlocked runtime vault first (covers passphrase-unlocked sessions).
    try:
        from olano.vault.resolver import get_vault

        runtime_vault = get_vault()
        if runtime_vault is not None:
            value = runtime_vault.get(env_var)
            if value:
                os.environ[env_var] = value
                return
    except Exception:
        pass

    # Fall back to opening a fresh connection (works when OLANO_VAULT_KEY or keyring is set).
    if not _can_auto_unlock_vault():
        return
    try:
        from olano.vault.store import VaultStore, default_vault_path

        vault_path = default_vault_path()
        if not vault_path.exists():
            return
        vault = VaultStore.open(db_path=vault_path)
        os.environ[env_var] = vault.get(env_var)
    except Exception:
        return


# ── Model builder ─────────────────────────────────────────────────────────────


def catalog_context_window(model_str: str | None) -> int | None:
    """Max input tokens for a ``provider:model`` string, from olano's own sources.

    The synced cloud catalog first (``contextWindow`` per model row), then the
    managed-tier table for ``olano:*``. ``None`` when nothing on the box knows
    the model. Aliases resolve first so ``default:pro`` answers for its target.
    """
    if not model_str:
        return None
    try:
        from olano.models.aliases import resolve_model_alias

        model_str = resolve_model_alias(model_str)
    except Exception:
        pass
    try:
        from olano.catalog import models_registry

        window = models_registry.model_context_window(model_str)
        if window:
            return int(window)
    except Exception:
        pass
    if model_str.lower().startswith("olano:"):
        try:
            from olano.models.olano_gateway import tier_context_window

            return tier_context_window(model_str.lower())
        except Exception:
            return None
    return None


def _profile_max_input_tokens(model: Any) -> int | None:
    try:
        profile = model.profile
    except Exception:
        return None
    if not isinstance(profile, dict):
        return None
    value = profile.get("max_input_tokens")
    return value if isinstance(value, int) and value > 0 else None


def attach_context_profile(model: BaseChatModel, model_str: str) -> BaseChatModel:
    """Give ``model`` a ``profile["max_input_tokens"]`` when its client has none.

    Every LangChain provider client resolves ``model.profile`` from a static
    table bundled in its own pip package, keyed by model name — so a model
    newer than the installed package (``gpt-6-luna`` on the Codex backend), a
    gateway model or a self-hosted one reports no window at all. deepagents
    reads exactly that field to pick its compaction thresholds, and with it
    missing falls back to "keep the last 6 messages", which is how an agent
    forgot the start of a five-message session. The catalog knows the window,
    so hand it over. A profile the client already resolved is never
    overwritten. Best-effort: a client that refuses the attribute keeps
    working without it.
    """
    if _profile_max_input_tokens(model) is not None:
        return model
    window = catalog_context_window(model_str)
    if not window:
        return model
    try:
        existing = model.profile
    except Exception:
        existing = None
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged["max_input_tokens"] = int(window)
    try:
        model.profile = merged  # type: ignore[assignment]
    except Exception:
        try:
            object.__setattr__(model, "profile", merged)
        except Exception:
            logger.debug("could not attach context profile to %s", model_str)
            return model
    return model


def build_model(
    model_str: str,
    *,
    allow_utility: bool = False,
    max_output_tokens: int | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Build a LangChain chat model from a "provider:model" string.

    Thin wrapper over :func:`_build_model_client` (every provider branch lives
    there) that then attaches the catalog's context window as the model
    profile when the client has none — see :func:`attach_context_profile`.
    """
    model = _build_model_client(
        model_str, allow_utility=allow_utility, max_output_tokens=max_output_tokens, **kwargs
    )
    if model_str.startswith("fake"):
        return model
    return attach_context_profile(model, model_str)


def _build_model_client(
    model_str: str,
    *,
    allow_utility: bool = False,
    max_output_tokens: int | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """
    Build a LangChain chat model from a "provider:model" string.

    Handles all providers in PROVIDERS dict. For Ollama, auto-discovers
    local models. Falls back to init_chat_model for unknown providers.

    ``max_output_tokens`` is the provider-agnostic per-reply ceiling from
    ``message_policy``; it is translated to whichever kwarg the target provider
    accepts (``max_tokens`` / ``max_output_tokens`` / ``num_predict``) before
    any provider branch runs, so the managed ``olano:*`` tiers — which return
    early — get it too. See :mod:`olano.models.output_limit`.

    ``allow_utility`` is the TOOL-ONLY entry. Olano utility groups
    (``olano:vision``, ``olano:olano-image``) are services the product calls on
    the agent's behalf, never a brain a customer selects — so building one as an
    agent model is rejected unless the caller is the product tool itself (e.g.
    ``vision._build_vision_model`` passes ``allow_utility=True``). This is the
    one chokepoint every brain-selection surface funnels through.
    """
    # `default:<tier>` aliases resolve to their configured concrete target
    # before anything else looks at the string — parse_model_string knows no
    # `default` provider, and everything downstream (the fake short-circuit
    # included, so an alias can target `fake:*` in keyless CI; the utility
    # hard-block; entitlements; reasoning-kwarg gating) must judge the real
    # model.
    from olano.models.aliases import resolve_model_alias

    model_str = resolve_model_alias(model_str)

    # Deterministic offline model for profiling and keyless CI — never a real
    # provider, so it resolves before the catalog/registry paths entirely.
    if model_str == "fake" or model_str.startswith("fake:"):
        from olano.models.fake import build_fake_model

        return build_fake_model(model_str, **kwargs)

    from langchain.chat_models import init_chat_model

    provider, model_name = parse_model_string(model_str)

    # Per-reply output ceiling, mapped to this provider's spelling. Applied
    # here — above every provider branch, including the early `olano:*` return
    # — because a cap that works on one provider and silently vanishes on
    # another is worse than no cap at all.
    if max_output_tokens:
        from olano.models.output_limit import apply_output_limit

        kwargs = apply_output_limit(provider or "", kwargs, max_output_tokens)

    # "olano:*" — the branded, Olano-managed tiers. These do not resolve to a
    # provider client here: they resolve to the cloud model gateway, which holds
    # Olano's provider keys and meters the call in credits. This box never holds
    # a key for them, by design (models/olano_gateway.py explains why).
    #
    # The entitlement check below gates *selection* and is genuinely advisory —
    # it runs on customer-controlled hardware, so the gateway re-checks
    # authorization itself. Keeping it here is still worth it: it turns "your
    # plan doesn't include this tier" into an actionable local error instead of
    # a 403 mid-turn.
    if provider == "olano":
        from olano.deployment import enforcement as dep_enforcement
        from olano.models.olano_gateway import (
            UNGATED_TIERS,
            build_olano_model,
            build_olano_vision_model,
            is_olano_utility_model,
            normalize_tier,
        )

        # Hard-block a utility group used as a brain (hand-edited agent.json5,
        # /model, dashboard picker, cognition fast/deep model, …). The product
        # tools reach these through allow_utility=True; nothing else may.
        if is_olano_utility_model(model_str) and not allow_utility:
            raise ValueError(
                f"'{model_str}' is an Olano utility service the product calls on "
                "the agent's behalf (image understanding / image generation), not "
                "a model you can assign as an agent's brain. Choose a tier instead: "
                "olano:turbo, olano:pro or olano:max."
            )

        if not dep_enforcement.feature_enabled("olano_models"):
            raise ValueError(
                "Olano models are disabled for this deployment (deployment "
                "settings: features.olano_models)."
            )
        # `olano:vision` is a utility GROUP, not a tier: it is what
        # `image_describe` asks for when it needs a guaranteed-multimodal
        # backend. It resolves here so the whole product keeps one model-string
        # entry point, but it is deliberately absent from OLANO_TIERS, so no
        # picker offers it and no per-tier entitlement withholds it (a customer
        # entitled to olano models is entitled to look at an image).
        if model_name.strip().lower() == "vision":
            return build_olano_vision_model(**kwargs)
        tier = normalize_tier(model_name)
        if tier is None:
            raise ValueError(
                f"Unknown Olano model tier '{model_name}'. Use olano:turbo, olano:pro or olano:max."
            )
        # UNGATED_TIERS are gated by `olano_models` alone (the default tier, and
        # the retired-but-still-resolving `auto`); the rest can be withheld
        # individually per plan.
        if tier not in UNGATED_TIERS and not dep_enforcement.feature_enabled(f"olano_model_{tier}"):
            raise ValueError(
                f"The Olano model tier '{tier}' is disabled for this "
                f"deployment (deployment settings: features.olano_model_{tier})."
            )
        return build_olano_model(tier, **kwargs)

    if provider:
        _hydrate_provider_env_from_vault(provider)

    # One shared guard for every construction path: silently drop reasoning
    # kwargs the target model can't accept (see models/reasoning.py).
    from olano.models.reasoning import gate_reasoning_kwargs

    kwargs = gate_reasoning_kwargs(provider or "", model_name, kwargs)

    if provider == "ollama":
        # Ensure langchain-ollama is used (not community fallback). The class
        # is a subclass, not `ChatOllama` itself: langchain_ollama's message
        # converter raises on any content block outside its own four-kind
        # vocabulary, and a thread that survived a model switch (or carries an
        # image attachment olano composed Anthropic-style) hands it exactly
        # those. See `models/ollama_compat.py`.
        from olano.models.ollama_compat import chat_ollama_class

        return cast(BaseChatModel, chat_ollama_class()(model=model_name, **kwargs))

    # deepagents special-cases OpenAI to use the Responses API
    if provider == "openai":
        try:
            return cast(
                BaseChatModel,
                _responses_chat_openai_class()(model=model_name, use_responses_api=True, **kwargs),
            )
        except Exception:
            return cast(BaseChatModel, init_chat_model(model_str, **kwargs))

    if provider == "openai-codex":
        return _build_openai_codex_model(model_name, **kwargs)

    if provider == "vercel":
        return _build_vercel_gateway_model(model_name, **kwargs)

    if provider == "deepseek":
        try:
            return _build_deepseek_model(model_name, **kwargs)
        except ImportError:
            return cast(BaseChatModel, init_chat_model(model_str, **kwargs))

    if provider == "qwq":
        # langchain-qwq is not in init_chat_model's provider registry, so route it
        # explicitly. QwQ reasoning models (qwq-plus, qwq-32b, …) use ChatQwQ; the
        # broader Qwen3 series (qwen3-*, qwen-max, …) uses ChatQwen, which also
        # handles DashScope thinking mode. Both read DASHSCOPE_API_KEY from the env.
        from langchain_qwq import ChatQwen, ChatQwQ

        qwen_cls = ChatQwQ if model_name.lower().startswith("qwq") else ChatQwen
        return cast(BaseChatModel, qwen_cls(model=model_name, **kwargs))

    if provider == "moonshot":
        # Moonshot's ChatMoonshot (Kimi) is likewise absent from init_chat_model.
        from langchain_moonshot import ChatMoonshot

        return cast(BaseChatModel, ChatMoonshot(model=model_name, **kwargs))

    if provider == "huggingface":
        return _build_huggingface_model(model_name, **kwargs)

    # openrouter, litellm, and the rest resolve through init_chat_model's provider
    # registry (their packages ship in the base install).
    return cast(BaseChatModel, init_chat_model(model_str, **kwargs))


def is_codex_terminal_parse_error(exc: BaseException) -> bool:
    """True for the benign end-of-stream parse errors langchain_openai's
    Responses-API streaming raises on a terminal/empty chunk.

    The parser occasionally hits a ``None`` where it expected a dict/list/str and
    raises a ``TypeError`` or ``AttributeError`` that mentions ``NoneType`` — e.g.
    ``'NoneType' object is not iterable`` or ``'NoneType' object has no attribute
    'search'``. The model's content has already streamed by then, so these are
    treated as end-of-stream rather than a hard failure. Exposed at module level
    so it can be unit-tested.
    """
    if not isinstance(exc, TypeError | AttributeError):
        return False
    msg = str(exc).lower()
    return "nonetype" in msg and (
        "is not iterable" in msg or "has no attribute" in msg or "is not subscriptable" in msg
    )


# The ChatGPT Codex backend deterministically returns an opaque in-stream
# ``server_error`` for certain (model, exact-prompt-bytes) combinations: the
# same request replayed always fails, while byte-level variations of it — or the
# same bytes on a sibling model — succeed. Observed on gpt-5.6-luna/gpt-5.5
# (2026-07); no request parameter (reasoning effort, cache key, tool count)
# escapes it. Recovery that works in practice: retry the identical request on a
# sibling model. Chain is overridable via LIONCLAW_CODEX_FALLBACK_MODELS.
CODEX_SERVER_ERROR_FALLBACKS = ("gpt-5.6-sol", "gpt-5.6-terra")


def is_codex_server_error(exc: BaseException) -> bool:
    """True for the Codex backend's opaque ``server_error`` stream error.

    Read the marker from **both** ``type`` and ``code``. OpenAI's standard error
    envelope carries the classifier in ``type`` and leaves ``code`` null for
    server faults, and ``APIError`` copies each straight off the body — so a
    ``code``-only check silently missed the most common shape of this very
    error, and the sibling-model retry below never fired for it. Symptom: the
    user sees "API error (APIError) … include the request ID …" with no
    "retrying on" line, and the *same* message keeps failing while a differently
    worded one goes through.

    Still narrow on purpose: only the ``server_error`` marker matches, so
    ``invalid_request_error`` / ``authentication_error`` / ``rate_limit_error``
    are never retried on a sibling model.

    langchain-openai 1.7 adds two more shapes, both a plain ``ValueError`` raised
    while converting the stream: a standard Responses ``error`` event (top-level
    ``code``/``message``, which the SDK does not raise on) becomes
    ``"server_error: <message>"``, and a ``response.failed`` event becomes the
    ``str()`` of its ``ResponseError`` (``"ResponseError(code='server_error', ..."``,
    or without the class name, depending on how the event was parsed).
    Through 1.6 both were ignored and the turn ended empty, so matching them
    here routes a newly visible failure into the same sibling retry.
    """
    if isinstance(exc, ValueError):
        text = str(exc)
        return text.startswith(
            ("server_error: ", "code='server_error'", "ResponseError(code='server_error'")
        )
    try:
        from openai import APIError
    except ImportError:  # pragma: no cover - openai ships with langchain-openai
        return False
    if not isinstance(exc, APIError):
        return False
    return "server_error" in {getattr(exc, "code", None), getattr(exc, "type", None)}


def codex_server_error_fallbacks(model_name: str) -> list[str]:
    """Sibling models to retry on after a ``server_error``, excluding the current one."""
    raw = os.getenv("LIONCLAW_CODEX_FALLBACK_MODELS")
    chain = (
        [m.strip() for m in raw.split(",") if m.strip()]
        if raw is not None
        else list(CODEX_SERVER_ERROR_FALLBACKS)
    )
    return [m for m in chain if m != model_name]


# Sentinel attribute marking langchain-openai's Responses chunk converter as
# already wrapped by _patch_responses_terminal_usage_salvage (idempotence).
_RESPONSES_SALVAGE_FLAG = "_lionclaw_terminal_usage_salvage"


def _patch_responses_terminal_usage_salvage() -> None:
    """Never lose ``usage_metadata`` to a terminal-event parse crash.

    langchain-openai attaches ``usage_metadata`` (and the real model name) to the
    stream only while parsing the final ``response.completed`` /
    ``response.incomplete`` event. The ChatGPT Codex backend's terminal payload
    trips that parser with the NoneType ``TypeError``/``AttributeError`` handled
    by :func:`is_codex_terminal_parse_error` — so before this patch every such
    call ended with **no usage recorded anywhere**: nothing in the checkpointed
    ``AIMessage`` for Insights/threadcost to mine, an empty ``usage_meta`` in the
    runner, and therefore no usage-ledger event or budget accounting either.

    Wrap the module-level converter so a parse failure on a *terminal* event
    salvages token usage straight from the raw ``chunk.response.usage`` and emits
    a minimal final chunk instead of raising. Failures on non-terminal events
    still raise exactly as before. Idempotent; if a future langchain-openai
    renames the hook the patch silently skips (behavior degrades to today's).
    """
    try:
        import langchain_openai.chat_models.base as lc_base
    except ImportError:  # pragma: no cover - langchain-openai ships in base install
        return
    orig = getattr(lc_base, "_convert_responses_chunk_to_generation_chunk", None)
    if orig is None or getattr(orig, _RESPONSES_SALVAGE_FLAG, False):
        return

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    def _salvage_terminal_chunk(
        chunk: Any, current_index: int, current_output_index: int, current_sub_index: int
    ) -> tuple[int, int, int, Any]:
        response = getattr(chunk, "response", None)
        usage = getattr(response, "usage", None)
        usage_metadata = None
        if usage is not None:
            try:
                raw = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
                usage_metadata = lc_base._create_usage_metadata_responses(raw)
            except Exception:
                usage_metadata = None
        response_metadata: dict[str, Any] = {}
        model_name = getattr(response, "model", None)
        if model_name:
            response_metadata["model_name"] = str(model_name)
        message = AIMessageChunk(
            content="", usage_metadata=usage_metadata, response_metadata=response_metadata
        )
        return (
            current_index,
            current_output_index,
            current_sub_index,
            ChatGenerationChunk(message=message),
        )

    def _converter_with_salvage(
        chunk: Any,
        current_index: int,
        current_output_index: int,
        current_sub_index: int,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[int, int, int, Any]:
        try:
            return cast(
                "tuple[int, int, int, Any]",
                orig(
                    chunk, current_index, current_output_index, current_sub_index, *args, **kwargs
                ),
            )
        except (TypeError, AttributeError) as exc:
            if getattr(chunk, "type", "") not in ("response.completed", "response.incomplete"):
                raise
            logger.warning(
                "Recovered token usage from a malformed terminal Responses event (%s): %s",
                getattr(chunk, "type", ""),
                exc,
            )
            return _salvage_terminal_chunk(
                chunk, current_index, current_output_index, current_sub_index
            )

    _carry_converter_flags(orig, _converter_with_salvage)
    setattr(_converter_with_salvage, _RESPONSES_SALVAGE_FLAG, True)
    lc_base._convert_responses_chunk_to_generation_chunk = _converter_with_salvage


# Sentinel attribute marking the converter as already wrapped by
# _patch_responses_parallel_tool_args (idempotence).
_RESPONSES_TOOL_ARGS_FLAG = "_olano_parallel_tool_args"

# function_call item id -> {"index": content index the call was announced at,
# "streamed": whether any argument text reached it}. Item ids are unique per
# response, so one module-level map serves every concurrent stream. Entries are
# dropped on the item's ``done`` event; the cap only bounds streams that died
# before theirs.
_FUNCTION_CALL_SLOTS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_FUNCTION_CALL_SLOTS_MAX = 4096
_FUNCTION_CALL_SLOTS_LOCK = threading.Lock()


def _patch_responses_parallel_tool_args() -> None:
    """Route each streamed argument delta to the tool call it belongs to.

    langchain-openai gives every streamed block a content ``index`` by bumping a
    counter whenever the event's ``output_index`` changes, and never looks back.
    That holds while the backend streams one call at a time (announce, args,
    announce, args). When it announces several parallel calls first and streams
    their arguments after, each delta lands on a fresh index: the announced
    calls keep their name and id with ``{}`` as arguments, and the real
    arguments become nameless, id-less calls nobody executes. The agent then
    sees every parallel call fail ``Field required`` for arguments it did send
    (observed with ``openai-codex:gpt-6-astra``; still present in
    langchain-openai 1.7.0).

    The wrapper remembers the index each ``function_call`` item was announced
    at, keyed by its item id, and converts its deltas at that index without
    moving the stream's own position. Sequential streams convert exactly as
    before. If a call's ``done`` event carries arguments that never streamed,
    they are emitted then, so a backend that skips the deltas loses nothing
    either. Idempotent; skips silently if langchain-openai renames the hook.
    """
    try:
        import langchain_openai.chat_models.base as lc_base
    except ImportError:  # pragma: no cover - langchain-openai ships in base install
        return
    orig = getattr(lc_base, "_convert_responses_chunk_to_generation_chunk", None)
    if orig is None or getattr(orig, _RESPONSES_TOOL_ARGS_FLAG, False):
        return

    from types import SimpleNamespace

    def _remember(item_id: str, index: int, streamed: bool) -> None:
        with _FUNCTION_CALL_SLOTS_LOCK:
            _FUNCTION_CALL_SLOTS[item_id] = {"index": index, "streamed": streamed}
            while len(_FUNCTION_CALL_SLOTS) > _FUNCTION_CALL_SLOTS_MAX:
                _FUNCTION_CALL_SLOTS.popitem(last=False)

    def _converter_routing_tool_args(
        chunk: Any,
        current_index: int,
        current_output_index: int,
        current_sub_index: int,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[int, int, int, Any]:
        event_type = getattr(chunk, "type", "")
        item = getattr(chunk, "item", None)

        if event_type == "response.function_call_arguments.delta":
            slot = _FUNCTION_CALL_SLOTS.get(getattr(chunk, "item_id", None) or "")
            if slot is not None:
                if getattr(chunk, "delta", ""):
                    slot["streamed"] = True
                # Converting at the slot's index with a matching output index
                # makes langchain's _advance a no-op, so the delta lands on its
                # call; the stream's own position is returned untouched.
                _, _, _, gen = orig(
                    chunk, slot["index"], chunk.output_index, current_sub_index, *args, **kwargs
                )
                return current_index, current_output_index, current_sub_index, gen

        elif (
            event_type == "response.output_item.done"
            and getattr(item, "type", None) == "function_call"
        ):
            item_id = getattr(item, "id", None) or ""
            with _FUNCTION_CALL_SLOTS_LOCK:
                slot = _FUNCTION_CALL_SLOTS.pop(item_id, None)
            arguments = getattr(item, "arguments", None)
            if slot is not None and not slot["streamed"] and arguments:
                delta = SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    output_index=chunk.output_index,
                    item_id=item_id,
                    delta=arguments,
                )
                _, _, _, gen = orig(
                    delta, slot["index"], chunk.output_index, current_sub_index, *args, **kwargs
                )
                return current_index, current_output_index, current_sub_index, gen

        result = cast(
            "tuple[int, int, int, Any]",
            orig(chunk, current_index, current_output_index, current_sub_index, *args, **kwargs),
        )
        if event_type == "response.output_item.added" and getattr(item, "type", None) == (
            "function_call"
        ):
            item_id = getattr(item, "id", None)
            if item_id:
                _remember(item_id, result[0], bool(getattr(item, "arguments", "")))
        return result

    _carry_converter_flags(orig, _converter_routing_tool_args)
    setattr(_converter_routing_tool_args, _RESPONSES_TOOL_ARGS_FLAG, True)
    lc_base._convert_responses_chunk_to_generation_chunk = _converter_routing_tool_args


def _carry_converter_flags(inner: Any, outer: Any) -> None:
    """Copy the converter patches' markers from ``inner`` onto its wrapper.

    Each patch checks the module-level converter for its own marker; without
    this, applying them in the order A, B, A wraps A twice.
    """
    for flag in (_RESPONSES_SALVAGE_FLAG, _RESPONSES_TOOL_ARGS_FLAG):
        if getattr(inner, flag, False):
            setattr(outer, flag, True)


def _default_tools_non_strict(payload: dict) -> None:
    """Send Responses API function tools with ``strict: false`` unless chosen.

    The Responses API treats a function tool with no ``strict`` key as strict,
    and strict mode makes every property required. An MCP tool whose optional
    fields are mutually exclusive then cannot be called correctly: the model
    must fill every field, so it invents placeholders (Lusha ``contacts_search``
    got ``"email": "x@y.com"`` beside ``contacts`` and refused the mixed call).
    Strict mode is also what rejects regex lookaround in a schema ``pattern``.

    Only the Responses shape (``name`` at the top level) is touched: a Chat
    Completions tool nests its definition under ``function`` and already
    defaults to non-strict, and a stray top-level key there would be rejected.
    """
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "function" and "name" in tool:
            tool.setdefault("strict", False)


_RESPONSES_CHAT_OPENAI_CLASS: type[ChatOpenAI] | None = None


def _responses_chat_openai_class() -> type[ChatOpenAI]:
    """``ChatOpenAI`` for the Responses API, with non-strict tools by default.

    Built lazily and cached: ``langchain_openai`` must not be imported at module
    load (see ``builder._ensure_deepagents_provider_imports_deferred``).
    """
    global _RESPONSES_CHAT_OPENAI_CLASS
    from langchain_openai import ChatOpenAI as _Base

    # Every Responses API model streams parallel tool calls through the same
    # converter, so the fix rides the class every such model is built from.
    _patch_responses_parallel_tool_args()

    # Keyed on the base's identity, not merely "built once": a test that
    # patches ``langchain_openai.ChatOpenAI`` must get a subclass of its fake.
    if _RESPONSES_CHAT_OPENAI_CLASS is None or _RESPONSES_CHAT_OPENAI_CLASS.__base__ is not _Base:

        class _ChatOpenAIResponses(_Base):
            def _get_request_payload(
                self,
                input_: Any,
                *,
                stop: list[str] | None = None,
                **request_kwargs: Any,
            ) -> dict:
                payload = super()._get_request_payload(input_, stop=stop, **request_kwargs)
                _default_tools_non_strict(payload)
                return payload

        _RESPONSES_CHAT_OPENAI_CLASS = _ChatOpenAIResponses
    return _RESPONSES_CHAT_OPENAI_CLASS


def _build_openai_codex_model(model_name: str, **kwargs: Any) -> BaseChatModel:
    """Build a ChatOpenAI client for the ChatGPT Codex backend."""
    from pydantic import SecretStr

    _patch_responses_terminal_usage_salvage()

    from olano.models.codex_auth import (
        codex_default_headers,
        resolve_codex_runtime_credentials,
    )

    def _stringify_responses_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(p for p in parts if p)
        return str(content) if content is not None else ""

    _is_codex_terminal_parse_error = is_codex_terminal_parse_error

    class _ChatOpenAICodex(_responses_chat_openai_class()):  # type: ignore[misc,valid-type]
        def _get_request_payload(
            self,
            input_: Any,
            *,
            stop: list[str] | None = None,
            **request_kwargs: Any,
        ) -> dict:
            payload: dict = super()._get_request_payload(input_, stop=stop, **request_kwargs)
            payload["store"] = False
            payload["stream"] = True

            input_items = payload.get("input")
            if isinstance(input_items, list):
                # Under store=False, a reasoning item can only be re-sent if it
                # carries inline encrypted_content (see include= on the client).
                # One that only has an `rs_...` id references a server item that
                # was never persisted, so the Responses API 404s. Drop those.
                payload["input"] = [
                    item
                    for item in input_items
                    if not (
                        isinstance(item, dict)
                        and item.get("type") == "reasoning"
                        and not item.get("encrypted_content")
                    )
                ]

            if payload.get("instructions"):
                return payload

            input_items = payload.get("input")
            if not isinstance(input_items, list):
                payload["instructions"] = "Follow the provided conversation instructions."
                return payload

            instructions: list[str] = []
            remaining: list[Any] = []
            for item in input_items:
                if isinstance(item, dict) and item.get("role") in {"system", "developer"}:
                    text = _stringify_responses_content(item.get("content")).strip()
                    if text:
                        instructions.append(text)
                else:
                    remaining.append(item)

            payload["input"] = remaining
            payload["instructions"] = (
                "\n\n".join(instructions).strip()
                or "Follow the provided conversation instructions."
            )
            return payload

        def _stream(self, *args: Any, **request_kwargs: Any):
            attempts: list[str | None] = [None, *codex_server_error_fallbacks(self.model_name)]
            for i, override in enumerate(attempts):
                kw = request_kwargs if override is None else {**request_kwargs, "model": override}
                yielded = False
                try:
                    for chunk in super()._stream(*args, **kw):
                        yielded = True
                        yield chunk
                    return
                except (TypeError, AttributeError) as exc:
                    if _is_codex_terminal_parse_error(exc):
                        logger.warning("Ignoring OpenAI Codex terminal stream parse error: %s", exc)
                        return
                    raise
                except Exception as exc:
                    # Only a pre-output server_error is safely retryable; retry it
                    # on the next sibling model (see CODEX_SERVER_ERROR_FALLBACKS).
                    if yielded or i == len(attempts) - 1 or not is_codex_server_error(exc):
                        raise
                    logger.warning(
                        "OpenAI Codex model '%s' returned server_error before any "
                        "output (deterministic upstream serving bug); retrying on '%s'",
                        override or self.model_name,
                        attempts[i + 1],
                    )

        async def _astream(self, *args: Any, **request_kwargs: Any):
            attempts: list[str | None] = [None, *codex_server_error_fallbacks(self.model_name)]
            for i, override in enumerate(attempts):
                kw = request_kwargs if override is None else {**request_kwargs, "model": override}
                yielded = False
                try:
                    async for chunk in super()._astream(*args, **kw):
                        yielded = True
                        yield chunk
                    return
                except (TypeError, AttributeError) as exc:
                    if _is_codex_terminal_parse_error(exc):
                        logger.warning(
                            "Ignoring OpenAI Codex terminal async stream parse error: %s", exc
                        )
                        return
                    raise
                except Exception as exc:
                    if yielded or i == len(attempts) - 1 or not is_codex_server_error(exc):
                        raise
                    logger.warning(
                        "OpenAI Codex model '%s' returned server_error before any "
                        "output (deterministic upstream serving bug); retrying on '%s'",
                        override or self.model_name,
                        attempts[i + 1],
                    )

    creds = resolve_codex_runtime_credentials()
    access_token = str(creds["api_key"])
    # ChatGPT-account Codex OAuth currently accepts only the streaming Responses
    # shape and rejects server-side storage.
    kwargs["streaming"] = True
    kwargs["store"] = False
    # Request inline encrypted reasoning so chain-of-thought round-trips across
    # tool calls without server-side storage (store=False). Without this, reasoning
    # items come back as bare `rs_...` ids that 404 on the next request.
    kwargs.setdefault("include", ["reasoning.encrypted_content"])
    return cast(
        BaseChatModel,
        _ChatOpenAICodex(
            model=model_name,
            api_key=SecretStr(access_token),
            base_url=str(creds["base_url"]),
            default_headers=codex_default_headers(access_token),
            use_responses_api=True,
            **kwargs,
        ),
    )


def _build_vercel_gateway_model(model_name: str, **kwargs: Any) -> BaseChatModel:
    """Build a ChatOpenAI client pointed at the Vercel AI Gateway.

    The gateway is OpenAI-compatible, so we reuse ``langchain-openai`` (already a
    dependency) with a custom ``base_url`` and the ``AI_GATEWAY_API_KEY`` bearer.
    Models are addressed as ``creator/model`` (e.g. ``anthropic/claude-sonnet-4-6``),
    which is exactly the ``model_name`` left after stripping the ``vercel:`` prefix.
    The endpoint can be overridden with ``AI_GATEWAY_BASE_URL`` (e.g. for a
    self-hosted gateway).
    """
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    api_key = os.environ.get("AI_GATEWAY_API_KEY")
    if not api_key:
        raise ValueError(
            "Vercel AI Gateway requires AI_GATEWAY_API_KEY. Set it in the credentials "
            "vault or environment (create a key at https://vercel.com/dashboard → AI Gateway)."
        )
    base_url = os.environ.get("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh/v1")
    return cast(
        BaseChatModel,
        ChatOpenAI(
            model=model_name,
            api_key=SecretStr(api_key),
            base_url=base_url,
            **kwargs,
        ),
    )


def _build_deepseek_model(model_name: str, **kwargs: Any) -> BaseChatModel:
    """Build ChatDeepSeek that round-trips reasoning_content for thinking mode.

    DeepSeek V4 Pro / V4.1 Flash in thinking mode require the assistant's
    `reasoning_content` from any turn that performed a tool call to be passed
    back in every subsequent request, or the API returns 400:
        "The reasoning_content in the thinking mode must be passed back to the API."

    Upstream langchain-deepseek captures reasoning_content into
    AIMessage.additional_kwargs on receipt but never re-includes it when
    serializing messages for the next request. We override _get_request_payload
    to mirror it back onto the assistant message dicts. The DeepSeek docs say
    passing reasoning_content on turns where it isn't strictly required is
    safe — the API ignores it — so we always include it when present.
    """
    from langchain_core.messages import AIMessage
    from langchain_deepseek import ChatDeepSeek

    class _ChatDeepSeekThinking(ChatDeepSeek):
        def _get_request_payload(
            self,
            input_: Any,
            *,
            stop: list[str] | None = None,
            **kw: Any,
        ) -> dict:
            payload = super()._get_request_payload(input_, stop=stop, **kw)
            try:
                src_messages = self._convert_input(input_).to_messages()
            except Exception:
                return payload
            for src, dst in zip(src_messages, payload.get("messages", []), strict=False):
                if dst.get("role") != "assistant" or not isinstance(src, AIMessage):
                    continue
                reasoning = src.additional_kwargs.get("reasoning_content")
                if reasoning:
                    dst["reasoning_content"] = reasoning
            return payload

    return cast(BaseChatModel, _ChatDeepSeekThinking(model=model_name, **kwargs))


def _build_huggingface_model(model_name: str, **kwargs: Any) -> BaseChatModel:
    """Build a **serverless** ChatHuggingFace (HF Inference API / TGI endpoint).

    ``init_chat_model("huggingface:…")`` defaults to a **local** transformers
    pipeline — it imports ``transformers`` (which pulls ``torch``) and raises
    ``Could not import transformers`` on the torch-free default install. We instead
    wrap a ``HuggingFaceEndpoint`` so the model runs against Hugging Face's hosted
    Inference API (or a dedicated TGI server), which needs only ``huggingface_hub``
    and stays torch-free. Reads ``HUGGINGFACEHUB_API_TOKEN`` from the env; point at
    a dedicated TGI deployment with ``HUGGINGFACEHUB_ENDPOINT_URL``. Passing
    ``model_id`` skips ChatHuggingFace's network/tokenizer model-id resolution.
    """
    from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

    endpoint_kwargs: dict[str, Any] = {"task": "text-generation"}
    endpoint_url = os.environ.get("HUGGINGFACEHUB_ENDPOINT_URL")
    if endpoint_url:
        endpoint_kwargs["endpoint_url"] = endpoint_url
    else:
        endpoint_kwargs["repo_id"] = model_name
    llm = HuggingFaceEndpoint(**endpoint_kwargs)
    return cast(BaseChatModel, ChatHuggingFace(llm=llm, model_id=model_name, **kwargs))


# ── Install hint ──────────────────────────────────────────────────────────────


def install_hint(provider_key: str) -> str:
    info = PROVIDERS.get(provider_key)
    if not info:
        return f"Unknown provider: {provider_key}"
    return f"uv add {info.package}"


# ── Provider label lookup ─────────────────────────────────────────────────────


def provider_label(slug: str) -> str:
    """Return the human-readable label for a provider slug.

    Replaces the legacy ``hermes_cli.providers.get_label`` helper.  Falls
    back to the slug itself when the provider isn't registered so callers
    can stay agnostic about exotic / custom providers.
    """
    if not slug:
        return ""
    info = PROVIDERS.get(slug)
    return info.name if info else slug
