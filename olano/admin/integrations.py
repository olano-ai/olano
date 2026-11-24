"""The integration finder — the deterministic answer to "connect me to X".

Three nouns, kept apart on purpose. An **integration** is a way to reach an
outside service; a **connection** is a credential in the one home (vault or
broker record), obtained by a method the provider declares; a **magic link**
is the chat-side completion of that method. This module owns the first noun:
given a query and an agent it lists every *supported* way to reach the
service, ranked, each with the exact tool calls that make it happen.

Four classes, ranked strictly in this order when a user asks to connect X:

1. **channel** — a messenger / platform connector (Telegram, WhatsApp, …).
2. **MCP server** — from the directory (a verified endpoint, its own OAuth).
3. **built-in toolkit** — a ``tools:`` pack, authenticated official Olano
   OAuth > own OAuth app > device code > API key.
4. **external** — nothing in any catalog. Never *listed*: it may be installed
   in the agent's sandbox, Olano never authenticates it, and it is at the
   user's risk. :func:`inspect_external` reads its documentation first.

Runtime-free: both the agent's own ``self_find_integration`` and
AgentFather's ``olano_find_integration`` call :func:`find_integration`, which
only reads catalogs and config. Every source is an existing accessor — the
platform catalog, the MCP directory, the toolkit registry, the connector
broker — nothing is duplicated here, and **no toolkit module is imported**:
member tool names come from inverting the generated ``_name_index`` (the
full ``list_bundles()`` scan costs ~2.5 s and is deliberately off this path).

Toolkit **auth** ranks through :func:`olano.core.connectors.connect_methods`
(``official_oauth`` > ``byo_oauth`` > ``device`` > ``key``); a key-only
toolkit reports ``key`` with the credential names the magic-link secret form
should collect.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── constants (public: the tools quote them) ─────────────────────────────────

POLICY = (
    "Only supported integrations are listed. Anything else is external: it may "
    "be installed in the agent's sandbox, Olano never authenticates it, and it "
    "is at the user's risk."
)

EXTERNAL_NOTE = (
    "Nothing built in matches. Anything else is external: it may be installed "
    "in the agent's sandbox, Olano never authenticates it, and it is at the "
    "user's risk. Use self_inspect_external(url) to read its documentation first."
)

INSPECT_DISCLAIMER = "Read from the project's documentation; nothing was installed or executed."

CLASS_RANK: dict[str, int] = {"channel": 0, "mcp": 1, "toolkit": 2}
MATCH_SCORE: dict[str, int] = {
    "exact": 100,
    "alias": 90,
    "prefix": 60,
    "substring": 40,
    "fuzzy": 20,
}
PER_CLASS_CAP = 3
FUZZY_RATIO = 0.75
# Local directory matches kept before option-building. `_rank` caps the mcp
# class at PER_CLASS_CAP anyway; this bounds the per-entry config walk, with
# headroom for the already_connected / on_agent bonuses to promote a weaker
# match past a stronger one.
_MCP_LOCAL_LIMIT = 12

# Which `match_kind` grades the LOCAL directory pass accepts. Deliberately
# stricter than the channel and toolkit legs, which take anything non-None: the
# MCP directory is ~526 entries against their ~96, so the same rule admits
# several times the noise, and it is measurable — `prefix` answered "google
# drive" with bigquery/firestore/spanner and "web search" with webflow/webex,
# `fuzzy` answered "notion" with motion. Naming a service is exact or aliased;
# looser matching is what the free-text pass below is for, and it still runs.
_MCP_LOCAL_TIERS: frozenset[str] = frozenset({"exact", "alias"})

# Toolkits an agent may never grant itself and never sees as "integrations":
# self-granting ``olano_admin`` is fleet-admin escalation, the rest are
# plumbing (already bound by capability) or need host-level setup.
TOOLKIT_DENYLIST: frozenset[str] = frozenset(
    {
        "olano_admin",
        "self_config",
        "magic_link",
        "secrets",
        "talk_to",
        "cortex",
        "gcloud",
        # Bound by capability (an agent with a WhatsApp business connector,
        # Twilio or Meta Cloud API). Its everyday tools come with the
        # connector; its admin tools are operator-granted
        # (OPERATOR_GRANTED_TOOLKITS). Neither is something an agent adds to
        # itself by toolkit name.
        "whatsapp_business",
    }
)

# The strict subset of the deny-list whose tools act on OTHER agents — the
# shared deployment log, any agent's transcripts and workspace, cross-agent
# execution, the fleet's config. This is a different question from the
# deny-list's ("may an agent grant itself this toolkit?"): ``cortex`` and
# ``talk_to`` are refused there because the builder already binds them by
# capability, and ``cortex_*`` / ``talk_to_agent`` sit in every agent's default
# tool list — badging those "fleet-wide" or gating an edit that re-lists them
# would be wrong on every agent. Holding one of THESE is what the Tools tab
# badges, ``_privileged_tools_denial`` gates, and ``doctor`` audits.
PRIVILEGED_TOOLKITS: frozenset[str] = frozenset({"olano_admin"})

# Toolkits only an administrator may hand to an agent, for a reason other than
# fleet scope: their tools act on an outside account that every agent on the box
# may share (creating, submitting and deleting the WhatsApp Business account's
# templates, on Twilio or on Meta's Cloud API). Like PRIVILEGED_TOOLKITS they are deny-listed (no agent adds them
# by toolkit name) and newly writing a member's name into a config from chat
# needs an administrator present (``_privileged_tools_denial``). Unlike them they
# are not badged "fleet-wide" and not in the doctor's fleet audit, because they
# do not reach other agents.
OPERATOR_GRANTED_TOOLKITS: frozenset[str] = frozenset({"whatsapp_business"})

# toolkit → connector-broker provider id (``olano.core.connectors``). Google
# and YouTube are NOT here: they ride the bespoke ``google`` flow, which only
# ever takes the operator's own OAuth client (``byo_oauth``).
TOOLKIT_PROVIDER: dict[str, str] = {
    "github": "github",
    "microsoft365": "microsoft365",
    "hubspot": "hubspot",
    "salesforce": "salesforce",
    "quickbooks": "quickbooks",
    "shopify": "shopify",
    "linkedin": "linkedin",
    "x_twitter": "x_twitter",
    "reddit": "reddit",
    "fitbit": "fitbit",
    "health": "fitbit",
    "buffer": "buffer",
    "docusign": "docusign",
    "facebook": "meta",
    "instagram": "meta",
}
GOOGLE_TOOLKITS: frozenset[str] = frozenset({"google", "youtube"})

# The messengers the ``channel-connect`` magic-link page serves, keyed by the
# ``channel_setup`` connector's ``platform`` id. Derived from the registry at
# call time (``all_connectors``); this only names the tool per pairing mode.
_QR_SELF_TOOL: dict[str, str] = {
    "whatsapp": "self_connect_whatsapp",
    "wechat": "self_connect_weixin",
}

_AUTH_ORDER: tuple[str, ...] = ("official_oauth", "byo_oauth", "device", "key")

# Words that carry no service name ("connect me to the github please").
_FILLER: frozenset[str] = frozenset(
    {
        "connect",
        "to",
        "the",
        "with",
        "my",
        "a",
        "an",
        "up",
        "me",
        "please",
        "and",
        "our",
        "into",
        "integrate",
        "integration",
    }
)

# query token → additional targets (matched as ``alias`` hits). The git group
# is an ADDITIONAL hit alongside github: "push my repo" surfaces both.
_ALIASES: dict[str, tuple[str, ...]] = {
    "gh": ("github",),
    "gmail": ("google",),
    "wechat": ("weixin",),
    "twitter": ("x_twitter",),
    "x": ("x_twitter",),
    "ms365": ("microsoft365",),
    "o365": ("microsoft365",),
    "outlook": ("microsoft365",),
    "fb": ("facebook",),
    "ig": ("instagram",),
    "clone": ("git", "github"),
    "push": ("git", "github"),
    "repo": ("git", "github"),
    "repository": ("git", "github"),
}

# Two products share the WeChat brand, and a bare "wechat" must reach exactly
# one of them: the personal messenger (``platform:weixin``, Tencent's iLink
# Bot API, QR-paired, the ``wechat`` channel-setup connector) versus WeCom,
# the enterprise product (``platform:wecom`` + the ``wecom`` toolkit; sold
# earlier as "WeChat Work" and "Enterprise WeChat", bot id + secret). Name
# matching alone confused them in both directions: WeCom's label carried the
# word "wechat", so "connect wechat" offered WeCom as a second option, while
# "enterprise wechat" ranked the personal messenger FIRST because "wechat" was
# an exact hit on it and only a substring hit on WeCom. :func:`wechat_flavour`
# reads the qualifier off the query and the channel and toolkit legs then hide
# the other product outright, out-scoring it is not enough, since the lead
# sentence names every option and the model may pick the wrong one.
_WECHAT_WORDS: frozenset[str] = frozenset({"wechat", "weixin", "wx"})
# Names that mean the enterprise product on their own.
_WECOM_WORDS: frozenset[str] = frozenset({"wecom", "qiye", "qiyeweixin"})
# Qualifiers that make any WeChat mention mean WeCom wherever they appear.
_WECOM_QUALIFIERS: frozenset[str] = frozenset(
    {"enterprise", "enterprises", "corporate", "corp", "company", "companies"}
)
# "WeChat Work" was the product's English brand until 2020, but "work" is an
# everyday word ("wechat for my work team"), so it counts only right beside
# the WeChat word.
_WECOM_ADJACENT: frozenset[str] = frozenset({"work"})
# Which product a channel base type / toolkit is, for the two legs.
_WECHAT_PRODUCTS: dict[str, str] = {"weixin": "personal", "wecom": "enterprise"}


def wechat_flavour(query: Query) -> str | None:
    """Which WeChat product *query* names: ``"personal"``, ``"enterprise"`` or ``None``.

    ``None`` means the query does not mention WeChat at all, and the legs
    apply no rule. ``"enterprise"`` needs either a WeCom name or a WeChat word
    plus an enterprise qualifier; a WeChat word alone is ``"personal"``.
    """
    tokens = list(query.tokens)
    if any(t in _WECOM_WORDS for t in tokens):
        return "enterprise"
    named = [i for i, t in enumerate(tokens) if t in _WECHAT_WORDS]
    if not named:
        return None
    if any(t in _WECOM_QUALIFIERS for t in tokens):
        return "enterprise"
    for i in named:
        before = tokens[i - 1] if i > 0 else ""
        after = tokens[i + 1] if i + 1 < len(tokens) else ""
        if before in _WECOM_ADJACENT or after in _WECOM_ADJACENT:
            return "enterprise"
    return "personal"


def _wechat_rule(flavour: str | None, product: str | None, match: str | None) -> str | None:
    """Apply the WeChat rule to one entry: ``"skip"``, or the match to use.

    *product* is the entry's side of :data:`_WECHAT_PRODUCTS` (``None`` for
    anything that is not a WeChat product, the rule never touches those).
    The wrong product is skipped; the right one gets at least an ``alias``
    grade, because with the brand word stripped from WeCom's label a query
    like "enterprise wechat" no longer matches its name at all.
    """
    if flavour is None or product is None:
        return match
    if product != flavour:
        return "skip"
    if match is None or MATCH_SCORE[match] < MATCH_SCORE["alias"]:
        return "alias"
    return match


# Entry-side keywords a toolkit answers to besides its own name. A hit here
# is an ``alias`` match (the user said what the service does, not its id).
_TOOLKIT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "google": ("gmail", "calendar", "drive", "sheets", "docs", "workspace"),
    "microsoft365": ("outlook", "teams", "onedrive", "sharepoint", "excel", "office"),
    "x_twitter": ("x", "twitter", "tweet"),
    "sql": ("postgres", "postgresql", "mysql", "sqlite", "database", "db"),
    "hubspot": ("crm",),
    "salesforce": ("crm",),
    "apollo": (
        "apollo.io",
        "prospecting",
        "lead enrichment",
        "contact enrichment",
        "sales intelligence",
        "sales sequences",
    ),
    "lusha": (
        "lusha.com",
        "b2b data",
        "contact data",
        "lead enrichment",
        "contact enrichment",
        "company enrichment",
        "prospecting",
        "buying signals",
        "intent data",
    ),
    "wecom": ("enterprise wechat", "wechat work", "wechat enterprise", "work wechat"),
    "web_search": ("search",),
    "web_fetch": ("fetch", "scrape"),
    "linkedin_outreach": (
        "linkedin",
        "connection request",
        "linkedin invite",
        "linkedin follow",
        "prospecting",
        "outreach",
    ),
    "wallet": ("ethereum", "eth", "crypto wallet"),
    "health": ("fitbit",),
    "facebook": ("meta",),
    "instagram": ("meta",),
    "weather": ("forecast",),
}

_TOOLKIT_LABELS: dict[str, str] = {
    "whatsapp_business": "WhatsApp Business",
    "github": "GitHub",
    "x_twitter": "X (Twitter)",
    "microsoft365": "Microsoft 365",
    "hubspot": "HubSpot",
    "apollo": "Apollo.io",
    "lusha": "Lusha",
    "quickbooks": "QuickBooks",
    "paypal": "PayPal",
    "docusign": "DocuSign",
    "youtube": "YouTube",
    "linkedin": "LinkedIn",
    "linkedin_outreach": "LinkedIn outreach",
    "coingecko": "CoinGecko",
    "okx": "OKX",
    "ibkr": "Interactive Brokers",
    "dydx": "dYdX",
    "fred": "FRED",
    "wecom": "WeCom",
    "dingtalk": "DingTalk",
    "web_search": "Web search",
    "web_fetch": "Web fetch",
    "sql": "SQL databases",
    "alpha_vantage": "Alpha Vantage",
    "e2b": "E2B sandboxes",
    "ddg": "DuckDuckGo",
    "wolfram": "Wolfram|Alpha",
    "pubmed": "PubMed",
    "arxiv": "arXiv",
    "http": "HTTP requests",
}


# ── query normalisation & matching ───────────────────────────────────────────


@dataclass(frozen=True)
class Query:
    """A normalised "connect X" request.

    ``tokens`` are the user's own words with filler dropped; ``phrases`` are
    the joined forms of a multi-word query (``microsoft 365`` → ``microsoft365``
    / ``microsoft_365``); ``alias_targets`` are what the alias table adds.
    """

    raw: str
    tokens: tuple[str, ...]
    phrases: tuple[str, ...]
    alias_targets: tuple[str, ...]

    @property
    def probes(self) -> tuple[str, ...]:
        return self.tokens + self.phrases

    @property
    def search_terms(self) -> tuple[str, ...]:
        """What to ask a free-text directory search: the query itself first,
        then each alias target (capped — every term is a lookup)."""
        terms: list[str] = [" ".join(self.tokens)]
        for target in self.alias_targets:
            if target not in terms:
                terms.append(target)
        return tuple(terms[:3])


def normalise_query(query: str) -> Query:
    """Lowercase, split, drop filler, expand aliases. Raises on an empty query."""
    text = str(query or "").strip().lower()
    words = [w for w in re.split(r"[^a-z0-9_.:/\-]+", text) if w]
    tokens = [w.strip("./:-") for w in words if w not in _FILLER]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise ValueError("say what to connect, e.g. 'notion', 'github' or 'whatsapp'")
    # Drop the words that name the MECHANISM rather than the service, so
    # "parallel mcp" and "parallel" ask the same question. Stripped from the
    # TOKENS, not just from `search_terms`: `probes` is tokens + phrases and
    # feeds `match_kind` on all three legs, so a surviving "mcp" would
    # substring-match every entry named "… MCP Server" and fill the per-class
    # cap with noise. Never empties the query — a bare "mcp server" keeps the
    # user's own words instead of raising above.
    from olano.core.mcp_registry import DIRECTORY_NOISE  # lazy: no module-scope imports here

    content = [t for t in tokens if t not in DIRECTORY_NOISE]
    if content:
        tokens = content
    targets: list[str] = []
    for tok in tokens:
        for alias in _ALIASES.get(tok, ()):
            if alias not in targets and alias not in tokens:
                targets.append(alias)
    phrases: tuple[str, ...] = ()
    if len(tokens) > 1:
        phrases = ("".join(tokens), "_".join(tokens), " ".join(tokens))
    return Query(raw=query, tokens=tuple(tokens), phrases=phrases, alias_targets=tuple(targets))


def _keyset(*names: str | None) -> set[str]:
    """Every spelling an entry answers to: as is, collapsed, and with
    ``_``/``-`` read as spaces (``x_twitter`` ⇒ ``x twitter``, ``xtwitter``)."""
    out: set[str] = set()
    for name in names:
        base = str(name or "").strip().lower()
        if not base:
            continue
        spaced = base.replace("_", " ").replace("-", " ")
        out.update({base, spaced, spaced.replace(" ", ""), spaced.replace(" ", "_")})
    return out


# A catalog name carries its pricing/auth qualifier in parentheses ("Parallel
# Search (Free · no API key)"); the brand is what a user says.
_PAREN_SUFFIX = re.compile(r"\s*\(.*?\)\s*$")


def _entry_slug(entry: dict[str, Any]) -> str:
    """The key a directory entry is deduped and configured under."""
    return str(entry.get("slug") or entry.get("url") or entry.get("name") or "")


def _entry_keys(entry: dict[str, Any]) -> set[str]:
    """Every spelling a directory entry answers to — **its own name only**.

    Slug plus the display name with its parenthetical qualifier dropped. Two
    tempting additions are deliberately absent, each measured against the live
    526-entry directory:

    * ``description`` / ``category`` — ``match_kind`` has no weak tier, so a
      passing prose mention would grade ``exact``/``alias`` (100/90) and
      outrank a real name match. Prose hits still arrive through the free-text
      pass and keep the ``fuzzy`` floor: found by prose, ranks last.
    * the endpoint's host label (``search.parallel.ai`` → ``parallel``) — one
      vendor host is shared by many products, so "microsoft 365" graded
      ``microsoft-learn``, ``microsoft-fabric`` and ``microsoft-sentinel`` as
      ``exact`` and buried the Microsoft 365 toolkit under them.
    """
    slug = str(entry.get("slug") or "")
    name = _PAREN_SUFFIX.sub("", str(entry.get("name") or "")).strip()
    return _keyset(slug, name)


def match_kind(query: Query, keys: Iterable[str], aliases: Iterable[str] = ()) -> str | None:
    """The strongest way ``query`` matches an entry, or ``None``.

    ``exact`` — a query word IS one of the entry's names; ``alias`` — the
    alias table (either side) bridges them; ``prefix`` / ``substring`` — the
    word is the start of / inside a name (3+ chars); ``fuzzy`` — difflib
    ratio ≥ 0.75 on 4+ char words. Evaluated strongest-first.
    """
    keyset = {k for k in (str(x).strip().lower() for x in keys) if k}
    aliasset = {a for a in (str(x).strip().lower() for x in aliases) if a}
    probes = list(query.probes)
    if any(p in keyset for p in probes):
        return "exact"
    if any(t in keyset for t in query.alias_targets) or any(p in aliasset for p in probes):
        return "alias"
    long_probes = [p for p in probes if len(p) >= 3]
    if any(k.startswith(p) for p in long_probes for k in keyset):
        return "prefix"
    if any(p in k for p in long_probes for k in keyset) or any(
        k in p for p in long_probes for k in keyset if len(k) >= 4
    ):
        return "substring"
    fuzzy = [p for p in probes if len(p) >= 4]
    for p in fuzzy:
        for k in keyset:
            if len(k) >= 4 and difflib.SequenceMatcher(None, p, k).ratio() >= FUZZY_RATIO:
                return "fuzzy"
    return None


# ── ranking ──────────────────────────────────────────────────────────────────


def _score(option: dict[str, Any]) -> int:
    score = MATCH_SCORE[option["match"]]
    if option.get("already_connected"):
        score += 10
    if option.get("on_agent"):
        score += 5
    if option["class"] == "mcp" and option.get("entry_kind") == "example":
        score -= 25
    return score


def _rank(options: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Strict class order, then score, then label; ≤ 3 per class; then ``limit``.

    Pure — the table-driven test drives it with synthetic options.
    """
    for option in options:
        option["score"] = _score(option)
    ordered = sorted(
        options,
        key=lambda o: (CLASS_RANK[o["class"]], -o["score"], str(o.get("label", "")).lower()),
    )
    counts: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    for option in ordered:
        cls = option["class"]
        if counts.get(cls, 0) >= PER_CLASS_CAP:
            continue
        counts[cls] = counts.get(cls, 0) + 1
        kept.append(option)
    kept = kept[: max(1, int(limit))]
    for i, option in enumerate(kept, 1):
        option["rank"] = i
    return kept


# ── shared helpers ───────────────────────────────────────────────────────────


def _load_config(agent: str) -> Any | None:
    """The agent's config, or ``None`` — a broken config must not stop the
    catalog from answering (it degrades to "nothing connected yet")."""
    try:
        from olano.config.loader import load_agent_config

        return load_agent_config(agent)
    except Exception:
        logger.debug("integrations: could not load config for %s", agent, exc_info=True)
        return None


def _credential_scope(agent: str, cfg: Any) -> Any:
    """A context in which ``get_credential`` answers for ``agent``.

    The self tool already runs inside the turn's ``agent_runtime_context``
    (re-binding would only reset the vault memo); the AgentFather twin runs
    as a *different* agent, so the probe must be re-bound to the target's
    identity + ``config.env`` overlay — the pattern ``multimodal_defaults``
    uses for the same reason.
    """
    import contextlib

    from olano.tools.talk_to import current_agent

    if current_agent.get() == agent:
        return contextlib.nullcontext()
    from olano.tools._credentials import agent_runtime_context

    return agent_runtime_context(agent, getattr(cfg, "env", None) or None)


def _resolves(name: str) -> bool:
    try:
        from olano.tools._credentials import get_credential

        return bool(get_credential(name))
    except Exception:
        return False


def _vault_ref(name: str) -> str:
    return "${vault:" + name + "}"


def _call(
    tool: str, args: dict[str, Any] | None = None, *, restart: bool = False
) -> dict[str, Any]:
    return {"tool": tool, "args": dict(args or {}), "requires_restart": bool(restart)}


def _configured_channel_bases(cfg: Any) -> set[str]:
    """Base channel types (``platform:telegram`` and ``telegram`` both read
    ``telegram``) present in the agent's ``channels`` — the same walk
    ``self_connect_platform`` does."""
    from olano.platforms.catalog import base_channel_type

    out: set[str] = set()
    for ch in getattr(cfg, "channels", None) or []:
        if isinstance(ch, str):
            ctype = ch.strip()
        elif isinstance(ch, dict):
            ctype = str(ch.get("type") or "")
        else:
            ctype = str(getattr(ch, "type", "") or "")
        if ctype:
            out.add(base_channel_type(ctype))
    return out


# ── toolkit registry views (no toolkit imports) ──────────────────────────────


def toolkit_members() -> dict[str, list[str]]:
    """toolkit → its member tool names, from the generated static index.

    Inverts ``_name_index.NAME_INDEX`` (tool → owning module) against
    ``registry._TOOLKITS`` (toolkit → module). Zero toolkit imports.
    """
    from olano.tools._name_index import NAME_INDEX
    from olano.tools.registry import _TOOLKITS

    by_module: dict[str, list[str]] = {}
    for tool_name, module in NAME_INDEX.items():
        by_module.setdefault(module, []).append(tool_name)
    return {tk: sorted(by_module.get(module, [])) for tk, module in _TOOLKITS.items()}


def privileged_tool_names() -> frozenset[str]:
    """Every tool name belonging to a :data:`PRIVILEGED_TOOLKITS` toolkit.

    :func:`expand_toolkit` refuses the deny-listed toolkits *by name*, which
    covers ``self_add_toolkit`` and ``olano_add_toolkit`` — but ``tools:`` takes
    individual tool names, so writing ``["olano_tail_log"]`` straight into an
    agent config walked around the same rule. This is the set that guard, the
    catalog badge and the doctor audit all read; same static-index inversion
    as :func:`toolkit_members`, so still zero toolkit imports.
    """
    members = toolkit_members()
    return frozenset(name for toolkit in PRIVILEGED_TOOLKITS for name in members.get(toolkit, ()))


def operator_granted_tool_names() -> frozenset[str]:
    """Every tool of an :data:`OPERATOR_GRANTED_TOOLKITS` toolkit that only an operator grants.

    A toolkit may hold both: ``whatsapp_business``'s everyday tools come with a
    connector through the internal-tools catalog, its admin tools do not. The
    catalog is what tells them apart, so a new tool in such a toolkit is
    operator-granted until it is deliberately put on the catalog, never the
    other way round.
    """
    from olano.config.internal_tools import INTERNAL_TOOL_CATALOG

    platform_bound = {t.name for t in INTERNAL_TOOL_CATALOG}
    members = toolkit_members()
    return frozenset(
        name
        for toolkit in OPERATOR_GRANTED_TOOLKITS
        for name in members.get(toolkit, ())
        if name not in platform_bound
    )


def expand_toolkit(name: str) -> list[str]:
    """Member tool names of a toolkit or composite bundle. Raises ``ValueError``
    for an unknown name or one on the deny-list."""
    from olano.tools.registry import _COMPOSITE_BUNDLES, _TOOLKITS

    slug = str(name or "").strip().lower()
    if not slug:
        raise ValueError("give a toolkit name, e.g. 'github' or 'notion'")
    if slug in TOOLKIT_DENYLIST:
        raise ValueError(
            f"toolkit '{slug}' cannot be granted this way — it is bound by capability "
            "(or is an admin-only toolkit an agent must never grant itself)"
        )
    if slug in _COMPOSITE_BUNDLES:
        return list(_COMPOSITE_BUNDLES[slug])
    if slug not in _TOOLKITS:
        raise ValueError(
            f"unknown toolkit '{slug}' — pick one from self_find_integration / "
            "olano_list_integrations (individual tool names go in tools: directly)"
        )
    members = toolkit_members().get(slug, [])
    if not members:
        raise ValueError(
            f"toolkit '{slug}' has no indexed tools — the static tool index is stale "
            "(scripts/gen_tool_name_index.py)"
        )
    return members


def toolkit_label(toolkit: str) -> str:
    return _TOOLKIT_LABELS.get(toolkit) or toolkit.replace("_", " ").title()


def toolkit_env_specs(toolkit: str) -> list[dict[str, Any]]:
    """The ``env_vars`` specs a toolkit declares (``requirements.BUNDLE_REQUIREMENTS``)."""
    from olano.tools.requirements import BUNDLE_REQUIREMENTS

    req = BUNDLE_REQUIREMENTS.get(toolkit) or {}
    return [dict(spec) for spec in (req.get("env_vars") or [])]


def _google_connected(agent: str) -> bool:
    """A Google token on disk for this agent or the box (the bespoke flow's
    store, not the vault) — without importing ``google_tools``."""
    try:
        from olano.config.home import get_olano_paths

        paths = get_olano_paths()
        return paths.agent_google_token_path(agent).exists() or paths.google_token_path.exists()
    except Exception:
        return False


def _google_client_available() -> bool:
    return (
        _resolves("GOOGLE_OAUTH_CLIENT")
        or _resolves("GOOGLE_OAUTH_CREDENTIALS")
        or (_resolves("GOOGLE_CLIENT_ID") and _resolves("GOOGLE_CLIENT_SECRET"))
    )


def _provider_auth(provider_id: str, required: list[str]) -> dict[str, Any]:
    """Rank a connector provider's methods: official > byo > device > key."""
    from olano.core.connectors import connect_methods, get_provider

    provider = get_provider(provider_id)
    if provider is None:
        return _key_auth(required)
    methods_avail = connect_methods(provider)
    why = {
        "official_oauth": (
            "Sign in with Olano's own app — one click, nothing to create",
            "No official Olano app is published for this service on this box",
        ),
        "byo_oauth": (
            "Sign in with the operator's own OAuth app (its client is stored)",
            "No OAuth client of your own is stored for this service",
        ),
        "device": (
            "Device-code sign-in: the user types a short code on the provider's page",
            "This service declares no device-code flow",
        ),
    }
    methods: list[dict[str, Any]] = []
    for method in _AUTH_ORDER:
        if method == "key":
            names = list(methods_avail.get("key") or []) or list(required)
            methods.append({"method": "key", "available": bool(names), "names": names})
            continue
        available = bool(methods_avail.get(method))
        methods.append(
            {"method": method, "available": available, "why": why[method][0 if available else 1]}
        )
    best = next((m["method"] for m in methods if m["available"]), "key")
    return {"provider": provider_id, "best": best, "methods": methods}


def _provider_refusal(provider_id: str, agent: str) -> str | None:
    """The stored refusal on *agent*'s connection for *provider_id*, or ``None``.

    Reads the broker's ``needs_reauth`` mark (set when a toolkit's 401 survived
    a token refresh); never raises, a broker hiccup must not hide an option.
    """
    try:
        from olano.core.connectors import connector_auth_status, get_provider

        provider = get_provider(provider_id)
        if provider is None:
            return None
        status = connector_auth_status(provider, agent or None)
        if not status.get("needs_reauth"):
            return None
        return str(status.get("auth_error") or "the service refused the saved sign-in")
    except Exception:
        return None


def _key_auth(names: list[str]) -> dict[str, Any]:
    if not names:
        return {"best": "none", "methods": []}
    return {"best": "key", "methods": [{"method": "key", "available": True, "names": list(names)}]}


# ── sources ──────────────────────────────────────────────────────────────────


def _channel_options(query: Query, cfg: Any, agent: str, surface: str) -> list[dict[str, Any]]:
    from olano.admin.platforms import list_platforms
    from olano.platforms.catalog import base_channel_type
    from olano.platforms.channel_setup import all_connectors

    connectors = {c.channel_type: c for c in all_connectors()}
    configured = _configured_channel_bases(cfg) if cfg is not None else set()
    flavour = wechat_flavour(query)
    options: list[dict[str, Any]] = []
    for row in list_platforms().get("platforms", []):
        if row.get("hidden") or row.get("deprecated"):
            continue
        ctype = str(row.get("type") or "")
        base = base_channel_type(ctype)
        label = re.sub(r"\s*\(.*?\)\s*$", "", str(row.get("label") or base)).strip() or base
        connector = connectors.get(ctype)
        keys = _keyset(base, label)
        if connector is not None:
            keys |= _keyset(connector.platform, connector.label)
        # A variant row (Lark on the Feishu adapter) is a name of its own.
        variants = list(row.get("variants") or [])
        for variant in variants:
            keys |= _keyset(
                str(variant.get("id") or ""),
                _PAREN_SUFFIX.sub("", str(variant.get("label") or "")).strip(),
            )
        match = _wechat_rule(flavour, _WECHAT_PRODUCTS.get(base), match_kind(query, keys))
        if match is None or match == "skip":
            continue
        pairing = "qr" if row.get("pairing") == "qr" else "token"
        required = list(row.get("required") or [])
        connected = base in configured
        connect: list[dict[str, Any]] = []
        alternatives: list[dict[str, Any]] = []
        if connector is not None:
            args: dict[str, Any] = {"platforms": connector.platform}
            if surface == "admin":
                args["agent"] = agent
            connect.append(_call("magic_link_connect_channels", args))
            qr_tool = _QR_SELF_TOOL.get(connector.platform)
            if qr_tool and surface == "self":
                alternatives.append(_call(qr_tool, {}, restart=True))
        else:
            settings: dict[str, Any] = {
                field: f"<{key}>" for field, key in (row.get("credentials") or {}).items()
            }
            settings["allowed_users"] = ["<user id>"]
            if surface == "admin":
                connect.append(
                    _call(
                        "olano_update_agent",
                        {"name": agent, "updates": {"channels": [{"type": ctype, **settings}]}},
                        restart=True,
                    )
                )
                connect.append(_call("olano_restart_agent", {"name": agent}))
            else:
                connect.append(
                    _call(
                        "self_connect_platform",
                        {"platform_type": ctype, "settings": settings},
                        restart=True,
                    )
                )
                connect.append(_call("self_restart", {}))
        how: list[dict[str, Any]] = []
        if connector is not None and connector.instructions:
            how.append(
                {"name": connector.platform, "doc_url": "", "guidance": connector.instructions}
            )
        for key in required:
            how.append({"name": key, "doc_url": "", "guidance": row.get("pairing_hint") or ""})
        options.append(
            {
                "class": "channel",
                "id": ctype,
                "label": label,
                "match": match,
                "already_connected": connected,
                "on_agent": connected,
                "enables": {
                    "kind": "channel",
                    "pairing": pairing,
                    "credentials": required,
                    "note": ("A new inbound door for humans: insist on an allowed_users list."),
                    # Same type, a preset of `extra` keys: Lark is
                    # `platform:feishu` + `extra.domain: "lark"`.
                    **({"variants": variants} if variants else {}),
                },
                "auth": {
                    "best": pairing,
                    "methods": [{"method": pairing, "available": True, "names": required}],
                },
                "connect": [] if connected else connect,
                "alternatives": [] if connected else alternatives,
                "requires_restart": any(c["requires_restart"] for c in connect) and not connected,
                "how_to_get_it": how,
            }
        )
    return options


def _mcp_configured_entry(cfg: Any, entry: dict[str, Any]) -> Any | None:
    url = entry.get("url")
    slug = entry.get("slug")
    command = entry.get("command")
    for server in getattr(cfg, "mcp_servers", None) or []:
        if url and server.url == url:
            return server
        if command and getattr(server, "transport", "") == "stdio":
            if server.command == command and list(server.args or []) == list(
                entry.get("args") or []
            ):
                return server
            if slug and server.url == slug:
                return server
    return None


def _live_mcp_tool_count(agent: str, url: str) -> int | None:
    """The last-known tool count for a connected server — only when the
    builder is already loaded (importing it here would cost seconds)."""
    builder = sys.modules.get("olano.core.builder")
    if builder is None:
        return None
    try:
        for row in builder.get_mcp_summary(agent) or []:
            if row.get("url") == url:
                tools = row.get("tools")
                return len(tools) if isinstance(tools, list) else None
    except Exception:
        return None
    return None


def _mcp_options(query: Query, cfg: Any, agent: str, surface: str) -> list[dict[str, Any]]:
    from olano.admin.mcp import directory_entries, search_mcp_directory

    hits: dict[str, dict[str, Any]] = {}
    grades: dict[str, str] = {}

    # Pass 1 — LOCAL. The channel and toolkit legs enumerate their catalogs and
    # filter with `match_kind`; this leg used to hand matching to a remote
    # whole-phrase `ilike`, so every extra word the user said hid the entry
    # ("parallel mcp" missed the row whose slug is exactly "parallel"). No
    # network: `directory_entries` is cache → disk → bundled.
    try:
        local: list[tuple[int, str, dict[str, Any], str]] = []
        for entry in directory_entries():
            slug = _entry_slug(entry)
            match = match_kind(query, _entry_keys(entry)) if slug else None
            if match in _MCP_LOCAL_TIERS:
                local.append((MATCH_SCORE[match], slug, entry, match))
        # Deterministic: CI runs xdist, and a shuffled tie is a flaky assertion.
        local.sort(key=lambda row: (-row[0], row[1]))
        for _score, slug, entry, match in local[:_MCP_LOCAL_LIMIT]:
            hits[slug], grades[slug] = entry, match
    except Exception:
        logger.debug("integrations: local MCP directory scan failed", exc_info=True)

    # Pass 2 — REMOTE free-text, one lookup per search term. Keeps hits driven
    # by an entry's description and anything added cloud-side since the last
    # sync. A remote row is fresher, so it REPLACES the local copy's fields —
    # but the local grade stands, because only it was computed per token.
    for term in query.search_terms:
        try:
            for entry in search_mcp_directory(term, limit=5):
                slug = _entry_slug(entry)
                if slug:
                    hits[slug] = entry
        except Exception:
            logger.debug("integrations: MCP directory search failed for %r", term, exc_info=True)

    options: list[dict[str, Any]] = []
    add_tool = "olano_add_mcp_server" if surface == "admin" else "self_add_mcp_server"
    for slug, entry in hits.items():
        name = str(entry.get("name") or slug)
        match = grades.get(slug) or match_kind(query, _keyset(slug, name)) or "fuzzy"
        transport = str(entry.get("transport") or ("stdio" if entry.get("command") else "http"))
        auth = str(entry.get("auth") or "none")
        env_keys = [str(k) for k in (entry.get("envKeys") or [])]
        url = str(entry.get("url") or "")
        command = str(entry.get("command") or "")
        args = [str(a) for a in (entry.get("args") or [])]
        configured = _mcp_configured_entry(cfg, entry) if cfg is not None else None
        oauth_done = False
        if configured is not None and auth == "oauth":
            try:
                from olano.core.mcp_setup import has_oauth_tokens

                oauth_done = has_oauth_tokens(agent, configured.url)
            except Exception:
                oauth_done = False
        connected = configured is not None and (auth != "oauth" or oauth_done)

        def _agent_args(extra: dict[str, Any]) -> dict[str, Any]:
            return {"agent": agent, **extra} if surface == "admin" else extra

        connect: list[dict[str, Any]] = []
        method = "none"
        if transport == "stdio":
            method = "key" if env_keys else "none"
            for key in env_keys:
                connect.append(
                    _call(
                        "magic_link_request_secret",
                        _agent_args({"name": key, "reason": f"{name} MCP server needs it"}),
                    )
                )
            connect.append(
                _call(
                    add_tool,
                    _agent_args(
                        {
                            "url": slug,
                            "transport": "stdio",
                            "command": command,
                            "command_args": args,
                            "env": {k: _vault_ref(k) for k in env_keys},
                        }
                    ),
                    restart=True,
                )
            )
        elif auth == "oauth":
            method = "oauth"
            connect.append(
                _call(add_tool, _agent_args({"url": url, "transport": transport, "auth": "oauth"}))
            )
            connect.append(_call("magic_link_connect_service", _agent_args({"service_name": url})))
        elif env_keys:
            method = "key"
            key = env_keys[0]
            connect.append(
                _call(
                    "magic_link_request_secret",
                    _agent_args({"name": key, "reason": f"{name} MCP server needs it"}),
                )
            )
            connect.append(
                _call(
                    add_tool,
                    _agent_args(
                        {
                            "url": url,
                            "transport": transport,
                            "auth": "none",
                            "headers": {"Authorization": f"Bearer {_vault_ref(key)}"},
                        }
                    ),
                    restart=True,
                )
            )
        else:
            connect.append(
                _call(add_tool, _agent_args({"url": url, "transport": transport}), restart=True)
            )
        if configured is not None and not connected:
            # Added, never authorized: only the sign-in is missing.
            connect = [_call("magic_link_connect_service", _agent_args({"service_name": url}))]
        if any(c["requires_restart"] for c in connect):
            connect.append(
                _call("olano_restart_agent", {"name": agent})
                if surface == "admin"
                else _call("self_restart", {})
            )
        guidance = " — ".join(
            s for s in (str(entry.get("appConsoleLabel") or ""), str(entry.get("notes") or "")) if s
        )
        how = [
            {
                "name": key,
                "doc_url": entry.get("appConsoleUrl") or entry.get("homepageUrl") or "",
                "guidance": guidance,
            }
            for key in (env_keys or [slug])
        ]
        tool_count = (
            _live_mcp_tool_count(agent, configured.url) if connected and configured else None
        )
        auth_block: dict[str, Any] = {
            "best": method,
            "methods": [{"method": method, "available": True, "names": env_keys}]
            if method != "none"
            else [],
        }
        oauth_client = str(entry.get("oauthClient") or "") if auth == "oauth" else ""
        if oauth_client:
            # "dcr" signs in with one tap; "preregistered" needs an OAuth app
            # of the user's own. Said up front so the agent can set the
            # expectation before the link, not after it asks.
            auth_block["oauth_client"] = oauth_client
            if oauth_client == "preregistered":
                auth_block["note"] = (
                    f"{name} only accepts an OAuth app of the user's own: the magic-link "
                    "page asks for its Client ID and Client secret and shows the redirect "
                    "URL to register on it (with a link to the provider's console), and "
                    "offers Composio as the route that needs no app. Tell the user this "
                    "before sending the link."
                )
        options.append(
            {
                "class": "mcp",
                "id": slug,
                "label": name,
                "match": match,
                "entry_kind": str(entry.get("kind") or "preset"),
                "already_connected": connected,
                "on_agent": configured is not None,
                "enables": {
                    "kind": "mcp",
                    "tool_count": tool_count,
                    "transport": transport,
                    "auth": auth,
                    "note": "known after the first connect" if tool_count is None else "live",
                },
                "auth": auth_block,
                "connect": [] if connected else connect,
                "requires_restart": (not connected) and any(c["requires_restart"] for c in connect),
                "how_to_get_it": how,
            }
        )
    return options


def _toolkit_options(query: Query, cfg: Any, agent: str, surface: str) -> list[dict[str, Any]]:
    from olano.subagents._defaults import _CATALOG_HIDDEN, default_category
    from olano.tools.requirements import BUNDLE_REQUIREMENTS

    members = toolkit_members()
    on_config = set(getattr(cfg, "tools", None) or []) if cfg is not None else set()
    flavour = wechat_flavour(query)
    options: list[dict[str, Any]] = []
    for toolkit in sorted(members):
        if toolkit in _CATALOG_HIDDEN or toolkit in TOOLKIT_DENYLIST:
            continue
        label = toolkit_label(toolkit)
        match = _wechat_rule(
            flavour,
            _WECHAT_PRODUCTS.get(toolkit),
            match_kind(query, _keyset(toolkit, label), _TOOLKIT_KEYWORDS.get(toolkit, ())),
        )
        if match is None or match == "skip":
            continue
        tools = members[toolkit]
        specs = toolkit_env_specs(toolkit)
        declared = [str(s.get("name")) for s in specs if s.get("name")]
        required = [str(s.get("name")) for s in specs if s.get("name") and not s.get("optional")]
        resolved = {name: _resolves(name) for name in declared}
        if toolkit in GOOGLE_TOOLKITS:
            connected = _google_connected(agent)
        elif required:
            connected = all(resolved[n] for n in required)
        elif declared:
            connected = any(resolved.values())
        else:
            connected = True  # keyless — nothing to connect
        on_agent = any(t in on_config for t in tools)

        provider_id = TOOLKIT_PROVIDER.get(toolkit)
        # A connector record the service refused (a toolkit reported the 401
        # and a refresh could not cure it) still resolves a token, so the
        # credential probe above says "connected". It is not: the finder
        # lists the sign-in step again and says why, so "connect me to
        # Salesforce" on a broken connection offers the reconnect instead of
        # answering "already connected".
        refused = _provider_refusal(provider_id, agent) if provider_id else None
        if refused is not None:
            connected = False
        if toolkit in GOOGLE_TOOLKITS:
            client_ok = _google_client_available()
            auth: dict[str, Any] = {
                "provider": "google",
                "best": "byo_oauth",
                "methods": [
                    {
                        "method": "byo_oauth",
                        "available": client_ok,
                        "why": (
                            "Google sign-in with the operator's own OAuth client"
                            if client_ok
                            else "Store a Google OAuth client first (GOOGLE_OAUTH_CLIENT "
                            "in the vault, via magic_link_request_secret)"
                        ),
                    }
                ],
            }
        elif provider_id:
            auth = _provider_auth(provider_id, required)
        else:
            auth = _key_auth(required or declared)

        def _agent_args(extra: dict[str, Any]) -> dict[str, Any]:
            return {"agent": agent, **extra} if surface == "admin" else extra

        connect: list[dict[str, Any]] = []
        if not on_agent:
            if surface == "admin":
                connect.append(
                    _call("olano_add_toolkit", {"agent": agent, "name": toolkit}, restart=True)
                )
            else:
                connect.append(_call("self_add_toolkit", {"name": toolkit}, restart=True))
        if not connected:
            best = auth["best"]
            if toolkit in GOOGLE_TOOLKITS:
                if not _google_client_available():
                    connect.append(
                        _call(
                            "magic_link_request_secret",
                            _agent_args(
                                {
                                    "name": "GOOGLE_OAUTH_CLIENT",
                                    "reason": "OAuth client JSON for the Google sign-in",
                                }
                            ),
                        )
                    )
                connect.append(
                    _call("magic_link_connect_service", _agent_args({"service_name": "google"}))
                )
            elif provider_id and best != "key":
                connect.append(
                    _call(
                        "magic_link_connect_service",
                        _agent_args({"service_name": provider_id, "method": best}),
                    )
                )
            else:
                names = required or [m["names"][0] for m in auth["methods"] if m.get("names")][:1]
                for name in names:
                    connect.append(
                        _call(
                            "magic_link_request_secret",
                            _agent_args({"name": name, "reason": f"the {label} tools need it"}),
                        )
                    )
        if any(c["requires_restart"] for c in connect):
            connect.append(
                _call("olano_restart_agent", {"name": agent})
                if surface == "admin"
                else _call("self_restart", {})
            )
        req = BUNDLE_REQUIREMENTS.get(toolkit) or {}
        how: list[dict[str, Any]] = [
            {
                "name": str(s.get("name")),
                "doc_url": str(s.get("doc_url") or req.get("doc_url") or ""),
                "guidance": str(s.get("guidance") or ""),
                **({"optional": True} if s.get("optional") else {}),
            }
            for s in specs
        ]
        if req.get("guidance") and not any(h["guidance"] for h in how):
            how.append(
                {
                    "name": toolkit,
                    "doc_url": str(req.get("doc_url") or ""),
                    "guidance": str(req["guidance"]),
                }
            )
        options.append(
            {
                "class": "toolkit",
                **({"needs_reauth": True, "auth_error": refused} if refused is not None else {}),
                "id": toolkit,
                "label": label,
                "match": match,
                "already_connected": connected,
                "on_agent": on_agent,
                "enables": {
                    "kind": "toolkit",
                    "tool_count": len(tools),
                    "tools": tools[:12],
                    "category": default_category(toolkit),
                },
                "auth": auth,
                "credentials": [{"name": n, "resolved": resolved[n]} for n in declared],
                "connect": connect,
                "requires_restart": not on_agent,
                "how_to_get_it": how,
            }
        )
    return options


# ── public API ───────────────────────────────────────────────────────────────


def find_integration(
    query: str, agent: str, *, limit: int = 8, surface: str = "self"
) -> dict[str, Any]:
    """Every supported way to connect ``query`` for ``agent``, ranked.

    ``surface`` decides whose tool names the ``connect`` steps name:
    ``"self"`` (the agent's own ``self_*`` tools, the default) or ``"admin"``
    (AgentFather's ``olano_*`` twins, every call carrying ``agent``). Raises
    ``ValueError`` on an empty query; a source that fails is skipped, never
    fatal.
    """
    q = normalise_query(query)
    agent = str(agent or "").strip()
    if not agent:
        raise ValueError("an agent name is required")
    surface = "admin" if surface == "admin" else "self"
    cfg = _load_config(agent)
    options: list[dict[str, Any]] = []
    sources = (
        ("channel", _channel_options),
        ("mcp", _mcp_options),
        ("toolkit", _toolkit_options),
    )
    with _credential_scope(agent, cfg):
        for cls, source in sources:
            try:
                options.extend(source(q, cfg, agent, surface))
            except Exception:
                logger.warning("integrations: %s source failed for %r", cls, query, exc_info=True)
    ranked = _rank(options, limit)
    return {
        "query": query,
        "agent": agent,
        "options": ranked,
        "external_note": None if ranked else EXTERNAL_NOTE,
        "policy": POLICY,
    }


def lead_sentence(result: dict[str, Any]) -> str:
    """One paragraph the model can relay verbatim."""
    options = result.get("options") or []
    if not options:
        return str(result.get("external_note") or EXTERNAL_NOTE)
    parts = []
    for o in options:
        state = (
            "already connected"
            if o.get("already_connected")
            else f"connect via {o['auth']['best']}"
        )
        if o.get("on_agent") and not o.get("already_connected"):
            state += ", already on this agent"
        parts.append(f"{o['rank']}. {o['label']} ({o['class']}, {state})")
    return f"Supported ways to connect {result.get('query')!r}, in order: " + "; ".join(parts) + "."


# ── toolkit grants ───────────────────────────────────────────────────────────


def _credential_report(agent: str, cfg: Any, toolkit: str) -> list[dict[str, Any]]:
    with _credential_scope(agent, cfg):
        return [
            {"name": str(s["name"]), "resolved": _resolves(str(s["name"]))}
            for s in toolkit_env_specs(toolkit)
            if s.get("name")
        ]


def add_toolkit(agent: str, name: str) -> dict[str, Any]:
    """Append a toolkit's member tool names to ``agent``'s ``tools:`` list.

    ``tools:`` takes individual tool names (the registry resolves names
    through the index; a bundle name is unknown there), so the toolkit is
    expanded through the static index and only the names not already present
    are appended. Deny-listed and unknown toolkits raise before any write.
    Persisted through the locked, validated ``mutate_agent_config`` writer.
    """
    from olano.config.loader import mutate_agent_config

    slug = str(name or "").strip().lower()
    members = expand_toolkit(slug)
    added: list[str] = []
    present: list[str] = []

    def _apply(cfg: Any) -> None:
        existing = list(cfg.tools or [])
        for tool_name in members:
            (present if tool_name in existing else added).append(tool_name)
        cfg.tools = existing + [t for t in members if t not in existing]

    cfg = mutate_agent_config(agent, _apply)
    return {
        "agent": agent,
        "toolkit": slug,
        "added": added,
        "already_present": present,
        "requires_restart": True,
        "next": "call self_restart" if added else "nothing to do — every tool was already listed",
        "credentials": _credential_report(agent, cfg, slug),
    }


def remove_toolkit(agent: str, name: str) -> dict[str, Any]:
    """Remove a toolkit's member tool names from ``agent``'s ``tools:`` list
    (only those names — nothing else in the list is touched)."""
    from olano.config.loader import mutate_agent_config

    slug = str(name or "").strip().lower()
    members = set(expand_toolkit(slug))
    removed: list[str] = []
    missing: list[str] = []

    def _apply(cfg: Any) -> None:
        existing = list(cfg.tools or [])
        for tool_name in sorted(members):
            (removed if tool_name in existing else missing).append(tool_name)
        cfg.tools = [t for t in existing if t not in members]

    mutate_agent_config(agent, _apply)
    note = ""
    if not removed:
        note = (
            "none of these tools were listed in tools: — a tool granted by default "
            "is switched off with disabled_tools instead"
        )
    return {
        "agent": agent,
        "toolkit": slug,
        "removed": removed,
        "not_present": missing,
        "requires_restart": bool(removed),
        "next": "call self_restart" if removed else note,
    }


# ── external inspection ──────────────────────────────────────────────────────

_GITHUB_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", re.I)
_BARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_BARE_NAME_RE = re.compile(r"^@?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)?$")
_ENV_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,}(?:_API_KEY|_TOKEN|_SECRET|_KEY))\b")
_OAUTH_RE = re.compile(r"\boauth\b|\bclient_id\b", re.I)
_MCP_URL_RE = re.compile(r"https?://[^\s\"'`<>)]+/(?:mcp|sse)\b[^\s\"'`<>)]*", re.I)

# Table-driven classifier: first rule that fires wins.
_KIND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mcp_stdio", re.compile(r"mcpServers|\bnpx\s+-y\b|\buvx\b")),
    ("mcp_remote", re.compile(r"--transport\s+(?:http|sse)\b|https?://\S+/(?:mcp|sse)\b", re.I)),
    ("skill", re.compile(r"\bSKILL\.md\b")),
    (
        "cli",
        re.compile(
            r"\b(?:npm\s+install\s+-g|brew\s+install|pip\s+install)\b[\s\S]{0,6000}?\bUsage:",
            re.I,
        ),
    ),
)

RUNS_WHERE: dict[str, str] = {
    "mcp_stdio": "ephemeral sandbox container (supported: add it as a stdio MCP server)",
    "mcp_remote": "remote (supported: add it as an http/sse MCP server)",
}
_RUNS_UNSUPPORTED = (
    "your agent's sandbox (unsupported: Olano never authenticates it; at the user's risk)"
)


def _candidate_urls(target: str) -> list[str]:
    t = target.strip()
    m = _GITHUB_RE.match(t)
    if m:
        owner, repo = m.group(1), m.group(2).removesuffix(".git")
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md", t]
    if re.match(r"^https?://", t, re.I):
        return [t]
    if _BARE_REPO_RE.match(t) and not t.startswith("@"):
        owner, repo = t.split("/", 1)
        return [
            f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md",
            f"https://github.com/{owner}/{repo}",
        ]
    if _BARE_NAME_RE.match(t):
        return [f"https://www.npmjs.com/package/{t}", f"https://pypi.org/project/{t}/"]
    return []


def _fetch_doc(url: str) -> tuple[str, str, str]:
    """``(text, title, error)`` via the sanitising, caching web fetcher."""
    try:
        from olano.tools.web_fetch import _web_fetch_impl

        raw = _web_fetch_impl(url, "markdown", 40_000)
    except Exception as exc:
        return "", "", f"{url}: {type(exc).__name__}: {exc}"
    if not isinstance(raw, str) or raw.startswith("Error"):
        return "", "", f"{url}: {raw}"
    try:
        payload = json.loads(raw)
    except ValueError:
        return "", "", f"{url}: unreadable fetch result"
    text = str(payload.get("text") or "")
    if not text.strip():
        return "", "", f"{url}: empty document"
    return text, str(payload.get("title") or ""), ""


def _snippet(text: str, match: re.Match[str], width: int = 160) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _guess_service_name(target: str, urls: list[str], title: str) -> str:
    """The service a project is probably *for*: ``notion-mcp-server`` → notion."""
    candidates: list[str] = []
    m = _GITHUB_RE.match(target) or (
        re.match(r"^([^/]+)/([^/]+)$", target) if _BARE_REPO_RE.match(target) else None
    )
    if m:
        candidates.append(m.group(2))
    for url in urls:
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail and tail.lower() not in {"readme.md", "head"}:
            candidates.append(tail)
    candidates.append(target)
    if title:
        candidates.append(title)
    for cand in candidates:
        words = re.split(r"[^a-z0-9]+", cand.lower())
        words = [
            w
            for w in words
            if w
            and w
            not in {
                "mcp",
                "server",
                "servers",
                "official",
                "the",
                "a",
                "an",
                "for",
                "com",
                "www",
                "http",
                "https",
                "readme",
                "md",
                "git",
                "github",
                "npm",
                "pypi",
                "package",
                "project",
            }
        ]
        if words:
            return " ".join(words[:2])
    return ""


def inspect_external(target: str, agent: str) -> dict[str, Any]:
    """Read an external project's documentation and say what it is.

    Fetches the README / package page (GitHub repos, npm and PyPI names, or
    any URL), classifies it by a table of textual markers, extracts the
    credential names and OAuth mentions it documents, and points at any
    built-in integration that already covers the same service. Never raises
    and never installs or runs anything: a fetch failure reads as
    ``kind: "unknown"`` with the error in ``evidence``.
    """
    target = str(target or "").strip()
    agent = str(agent or "").strip()
    result: dict[str, Any] = {
        "target": target,
        "kind": "unknown",
        "runs_where": _RUNS_UNSUPPORTED,
        "needs": {"env": [], "oauth": False},
        "built_in_alternative": [],
        "recommendation": "",
        "evidence": [],
        "disclaimer": INSPECT_DISCLAIMER,
    }
    try:
        if not target:
            result["evidence"].append("no URL or package name given")
            result["recommendation"] = (
                "Give a URL (or a GitHub owner/repo, npm or PyPI name) to inspect."
            )
            return result
        urls = _candidate_urls(target)
        if not urls:
            result["evidence"].append(f"{target!r} is not a URL, owner/repo, or package name")
            result["recommendation"] = (
                "Give a URL (or a GitHub owner/repo, npm or PyPI name) to inspect."
            )
            return result
        docs: list[tuple[str, str, str]] = []
        for url in urls:
            text, title, error = _fetch_doc(url)
            if error:
                result["evidence"].append(error)
                continue
            docs.append((url, text, title))
            if len(docs) >= 2 or url.endswith("README.md"):
                break
        result["fetched"] = [u for u, _, _ in docs]
        text = "\n\n".join(t for _, t, _ in docs)
        title = next((t for _, _, t in docs if t), "")
        kind = "unknown"
        if text:
            for rule_kind, pattern in _KIND_RULES:
                m = pattern.search(text)
                if m:
                    kind = rule_kind
                    result["evidence"].append(f"{rule_kind}: …{_snippet(text, m)}…")
                    break
            env_keys: list[str] = []
            for m in _ENV_KEY_RE.finditer(text):
                if m.group(1) not in env_keys:
                    env_keys.append(m.group(1))
            oauth = _OAUTH_RE.search(text)
            result["needs"] = {"env": env_keys[:10], "oauth": bool(oauth)}
            if oauth:
                result["evidence"].append(f"oauth: …{_snippet(text, oauth)}…")
            cmd = re.search(r'"command"\s*:\s*"([^"]+)"', text)
            args = re.search(r'"args"\s*:\s*\[([^\]]*)\]', text)
            if kind == "mcp_stdio" and cmd:
                result["suggested_server"] = {
                    "transport": "stdio",
                    "command": cmd.group(1),
                    "args": re.findall(r'"([^"]*)"', args.group(1)) if args else [],
                    "env": {k: _vault_ref(k) for k in env_keys[:10]},
                }
            elif kind == "mcp_remote":
                url_m = _MCP_URL_RE.search(text)
                if url_m:
                    remote = url_m.group(0)
                    result["suggested_server"] = {
                        "transport": "sse" if remote.rstrip("/").endswith("/sse") else "http",
                        "url": remote,
                        "auth": "oauth" if oauth else "none",
                    }
        result["kind"] = kind
        result["runs_where"] = RUNS_WHERE.get(kind, _RUNS_UNSUPPORTED)
        guess = _guess_service_name(target, urls, title)
        if guess and agent:
            try:
                result["built_in_alternative"] = find_integration(guess, agent)["options"][:3]
            except Exception:
                result["built_in_alternative"] = []
        result["recommendation"] = _recommend(result, guess)
        return result
    except Exception as exc:  # never raise into a tool
        logger.debug("inspect_external failed for %r", target, exc_info=True)
        result["evidence"].append(f"{type(exc).__name__}: {exc}")
        result["recommendation"] = (
            "Could not read the documentation; ask the user for a URL or try later."
        )
        return result


def _recommend(result: dict[str, Any], guess: str) -> str:
    alt = result.get("built_in_alternative") or []
    kind = result["kind"]
    parts: list[str] = []
    if alt:
        first = alt[0]
        parts.append(
            f"A supported integration already covers '{guess}': prefer {first['label']} "
            f"({first['class']}) — its connect steps are in built_in_alternative."
        )
    env = result["needs"]["env"]
    if kind == "mcp_stdio":
        parts.append(
            "Otherwise this is a stdio MCP server, supported as-is: collect each credential "
            "with magic_link_request_secret, then add it with self_add_mcp_server(transport="
            "'stdio', command, command_args, env={KEY: '${vault:KEY}'}) and restart."
        )
    elif kind == "mcp_remote":
        parts.append(
            "Otherwise this is a remote MCP server, supported as-is: add it with "
            "self_add_mcp_server(url, transport) — auth='oauth' plus magic_link_connect_service"
            "(url) when it signs in, or a '${vault:KEY}' Authorization header for an API key."
        )
    elif kind == "skill":
        parts.append(
            "This is a skill (SKILL.md), not a connector: install it as a skill; it "
            "authenticates nothing on its own."
        )
    elif kind == "cli":
        parts.append(
            "This is a command-line tool. It can only run in the agent's sandbox, unsupported: "
            "Olano never authenticates it, and it is at the user's risk. Say so before "
            "installing anything, and only with the user's explicit go-ahead."
        )
    else:
        parts.append(
            "Could not tell what this is from its documentation. Treat it as external: "
            "unsupported, never authenticated by Olano, at the user's risk."
        )
    if env:
        parts.append("Documented credentials: " + ", ".join(env) + ".")
    if result["needs"]["oauth"]:
        parts.append("It mentions OAuth; a sign-in Olano does not broker stays the user's own.")
    return " ".join(parts)


__all__ = [
    "EXTERNAL_NOTE",
    "INSPECT_DISCLAIMER",
    "OPERATOR_GRANTED_TOOLKITS",
    "POLICY",
    "PRIVILEGED_TOOLKITS",
    "TOOLKIT_DENYLIST",
    "TOOLKIT_PROVIDER",
    "Query",
    "add_toolkit",
    "expand_toolkit",
    "find_integration",
    "inspect_external",
    "lead_sentence",
    "match_kind",
    "normalise_query",
    "operator_granted_tool_names",
    "privileged_tool_names",
    "remove_toolkit",
    "toolkit_members",
]
