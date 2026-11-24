"""Structured model catalog for the setup wizard model picker."""

from __future__ import annotations

from dataclasses import dataclass, field

from olano.models import PROVIDERS


@dataclass(frozen=True)
class ModelCatalogEntry:
    name: str  # "provider:model" e.g. "anthropic:claude-sonnet-4-6"
    provider: str
    display_name: str
    description: str
    recommended: bool
    local: bool
    disk_gb: float | None  # non-None for local models
    context_window: int  # 0 = unknown
    requires_keys: tuple[str, ...] = field(default_factory=tuple)


# Static metadata overlay for well-known models
# Keys are the short model name (after provider: prefix)
_MODEL_METADATA: dict[str, dict] = {
    # Anthropic
    "claude-opus-4-6": {
        "display_name": "Claude Opus 4.6",
        "description": "Most capable Anthropic model, deep reasoning",
        "recommended": False,
        "context_window": 200_000,
    },
    "claude-sonnet-4-6": {
        "display_name": "Claude Sonnet 4.6",
        "description": "Anthropic flagship — fast, capable, 200K ctx",
        "recommended": True,
        "context_window": 200_000,
    },
    "claude-haiku-4-5": {
        "display_name": "Claude Haiku 4.5",
        "description": "Fastest and lightest Anthropic model",
        "recommended": False,
        "context_window": 200_000,
    },
    "claude-sonnet-4": {
        "display_name": "Claude Sonnet 4",
        "description": "Previous Sonnet generation",
        "recommended": False,
        "context_window": 200_000,
    },
    # OpenAI
    "gpt-5.4": {
        "display_name": "GPT-5.4",
        "description": "OpenAI full model",
        "recommended": True,
        "context_window": 128_000,
    },
    "gpt-5.4-mini": {
        "display_name": "GPT-5.4 mini",
        "description": "Smaller, faster, cheaper GPT-5",
        "recommended": False,
        "context_window": 128_000,
    },
    "gpt-5.4-nano": {
        "display_name": "GPT-5.4 nano",
        "description": "Nano variant of GPT-5",
        "recommended": False,
        "context_window": 128_000,
    },
    # Google
    "gemini-2.5-pro": {
        "display_name": "Gemini 2.5 Pro",
        "description": "Google's most capable Gemini model",
        "recommended": True,
        "context_window": 1_048_576,
    },
    "gemini-2.5-flash": {
        "display_name": "Gemini 2.5 Flash",
        "description": "Fast + capable Gemini with 1M context",
        "recommended": False,
        "context_window": 1_000_000,
    },
    # Ollama
    "llama3.2": {
        "display_name": "Llama 3.2",
        "description": "Meta's open model, runs locally via Ollama",
        "recommended": True,
        "context_window": 128_000,
        "disk_gb": 2.0,
    },
    "qwen2.5:7b": {
        "display_name": "Qwen 2.5 7B",
        "description": "Alibaba open model, strong code + multilingual",
        "recommended": False,
        "context_window": 32_000,
        "disk_gb": 4.5,
    },
    "mistral": {
        "display_name": "Mistral 7B",
        "description": "Mistral AI open model, fast inference",
        "recommended": False,
        "context_window": 32_000,
        "disk_gb": 4.1,
    },
}

_DEFAULTS: dict = {
    "display_name": "",
    "description": "",
    "recommended": False,
    "context_window": 0,
    "disk_gb": None,
}


def load_catalog() -> list[ModelCatalogEntry]:
    """Build the full model catalog from PROVIDERS + metadata overlay."""
    entries: list[ModelCatalogEntry] = []
    for provider_key, info in PROVIDERS.items():
        local = provider_key in ("ollama",)
        requires_keys: tuple[str, ...] = (info.env_var,) if info.env_var and not local else ()
        for model in info.default_models:
            meta = {**_DEFAULTS, **_MODEL_METADATA.get(model, {})}
            full_name = f"{provider_key}:{model}"
            display_name = meta["display_name"] or f"{info.name} {model}"
            entries.append(
                ModelCatalogEntry(
                    name=full_name,
                    provider=provider_key,
                    display_name=display_name,
                    description=meta["description"],
                    recommended=meta["recommended"],
                    local=local,
                    disk_gb=meta.get("disk_gb"),
                    context_window=meta["context_window"],
                    requires_keys=requires_keys,
                )
            )
    return entries


def recommended_entries() -> list[ModelCatalogEntry]:
    return [e for e in load_catalog() if e.recommended]


def filter_catalog(catalog: list[ModelCatalogEntry], query: str) -> list[ModelCatalogEntry]:
    """Substring filter on display_name + description."""
    q = query.lower()
    return [e for e in catalog if q in e.display_name.lower() or q in e.description.lower()]
