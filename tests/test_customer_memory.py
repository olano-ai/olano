"""Tests for per-customer isolated memory (customer-facing agents)."""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.memory.customer_memory import (
    _sanitize_id,
    active_customer_id,
    customers_base,
    legacy_customers_base,
    make_customer_memory_tools,
    make_shared_knowledge_tools,
    sanitize_identity,
)
from olano.security.request_context import push_request_context


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the olano home at a temp dir so customer data is isolated per test."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    return tmp_path


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _customer_dir(home: Path, agent: str, customer: str) -> Path:
    return home / "agents" / agent / "workspace" / "customers" / customer


# ─────────────────────────────── sanitization ────────────────────────────────


def test_sanitize_blocks_path_traversal():
    assert _sanitize_id("../../etc/passwd") == "etc_passwd"
    assert _sanitize_id("..") is None
    assert _sanitize_id("/") is None
    assert _sanitize_id("") is None
    assert _sanitize_id("  ") is None


def test_sanitize_keeps_normal_ids():
    assert _sanitize_id("449281") == "449281"
    assert _sanitize_id("user-42_x") == "user-42_x"


# ───────────────────── identity encoding must be injective ───────────────────
#
# `_sanitize_id` maps every unsafe character to "_", so it merges people:
# bob.smith and bob_smith are two subscribers, not one. Merging them shares
# their private memory, their files and their subscription record, so the
# identity path uses `sanitize_identity`, which never collides.


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("bob.smith", "bob_smith"),  # different characters, different people
        ("a/b", "a_b"),
        ("a b", "a_b"),
        ("café", "caf_"),
        ("x" * 130 + "1", "x" * 130 + "2"),  # collided on truncation
        ("user@host", "user_host"),
    ],
)
def test_distinct_identities_never_share_a_directory(left: str, right: str):
    assert left != right
    assert sanitize_identity(left) != sanitize_identity(right)


def test_identity_is_stable_for_ids_already_safe():
    """No migration: every directory already on disk keeps its name."""
    for value in ("449281", "user-42_x", "bob_smith", "50688881234", "abc-DEF_123"):
        assert sanitize_identity(value) == value


def test_identity_is_deterministic_and_filesystem_safe():
    import re as _re

    for value in ("bob.smith", "a/b", "../../etc/passwd", "x" * 300, "  spaced  "):
        first = sanitize_identity(value)
        assert first == sanitize_identity(value)  # deterministic across calls
        assert first and _re.fullmatch(r"[A-Za-z0-9_-]+", first), first
        assert ".." not in first and "/" not in first
        assert len(first) <= 128


def test_identity_refuses_an_empty_value():
    """Callers must refuse rather than invent a shared bucket."""
    for value in ("", "   ", None):
        assert sanitize_identity(value) is None


def test_two_platforms_cannot_alias_across_the_separator():
    """`telegram-a` + `b` must not name the same folder as `telegram` + `a-b`."""
    with push_request_context(channel="telegram-a", user_id="b"):
        first = active_customer_id()
    with push_request_context(channel="telegram", user_id="a-b"):
        second = active_customer_id()
    assert first != second


def test_active_customer_id_reads_context():
    """The directory name is `<platform>-<user_id>`, both from the context."""
    with push_request_context(channel="telegram", user_id="12345"):
        assert active_customer_id() == "telegram-12345"
    # The default context carries channel="local".
    with push_request_context(user_id="12345"):
        assert active_customer_id() == "local-12345"


def test_real_channel_names_are_untouched():
    """Every `Platform` enum value is a bare lowercase word, so no deployed
    directory is renamed by the injective encoding."""
    for channel in ("telegram", "whatsapp", "discord", "slack", "local", "email"):
        with push_request_context(channel=channel, user_id="50688881234"):
            assert active_customer_id() == f"{channel}-50688881234"


def test_an_exotic_channel_name_is_disambiguated_too():
    """A custom channel with unsafe characters must not alias onto the
    underscore spelling of another one."""
    with push_request_context(channel="platform:whatsapp", user_id="50688881234"):
        colon = active_customer_id()
    with push_request_context(channel="platform_whatsapp", user_id="50688881234"):
        underscore = active_customer_id()
    assert colon != underscore
    assert colon.endswith("-50688881234")


# ─────────────────────────────── isolation ───────────────────────────────────


def test_two_customers_do_not_share_memory(home: Path):
    tools = make_customer_memory_tools("supportbot")
    remember = _tool(tools, "remember")
    recall = _tool(tools, "recall")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        remember.invoke({"fact": "Order 111 is delayed"})
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        remember.invoke({"fact": "Wants a refund on order 222"})

    # Alice only sees her own data.
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = recall.invoke({"query": "order"})
        assert "111" in out
        assert "222" not in out

    # Bob only sees his own.
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        out = recall.invoke({"query": "order"})
        assert "222" in out
        assert "111" not in out


def test_same_sender_id_on_two_platforms_is_two_customers(home: Path):
    """Sender ids are only unique within a platform: the same digits arriving
    over Telegram and WhatsApp must never collapse into one store."""
    tools = make_customer_memory_tools("supportbot")
    remember = _tool(tools, "remember")
    recall = _tool(tools, "recall")

    with push_request_context(source="channel", channel="telegram", user_id="449281"):
        remember.invoke({"fact": "Telegram person: order 111"})
    with push_request_context(source="channel", channel="whatsapp", user_id="449281"):
        remember.invoke({"fact": "WhatsApp person: order 222"})

    with push_request_context(source="channel", channel="telegram", user_id="449281"):
        out = recall.invoke({"query": "order"})
        assert "111" in out
        assert "222" not in out

    assert (_customer_dir(home, "supportbot", "telegram-449281") / "MEMORY.md").exists()
    assert (_customer_dir(home, "supportbot", "whatsapp-449281") / "MEMORY.md").exists()


def test_storage_lives_in_the_agent_workspace(home: Path):
    """`<home>/agents/<agent>/workspace/customers/<platform>-<user_id>/` — inside
    the agent's own tree, so deleting/backing up the agent takes its customer
    data with it. Nothing may be written to the pre-0006 home-level layout
    anymore."""
    tools = make_customer_memory_tools("supportbot")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "remember").invoke({"fact": "hello"})

    customer_dir = _customer_dir(home, "supportbot", "telegram-alice")
    assert (customer_dir / "MEMORY.md").exists()
    assert customers_base("supportbot") == customer_dir.parent
    # No leakage into a different agent's namespace.
    assert not _customer_dir(home, "other", "telegram-alice").exists()
    # The legacy home-level tree is never created.
    assert not legacy_customers_base("supportbot").exists()
    assert not (home / "customers").exists()


# ─────────────────── pre-platform (bare sender id) stores ────────────────────


def test_legacy_bare_id_store_is_adopted_on_first_contact(home: Path):
    """A directory written before the platform qualifier (named by the bare
    sender id) is renamed to `<platform>-<user_id>` the first time that sender
    reaches the agent again, and its history carries over."""
    legacy = _customer_dir(home, "supportbot", "alice")
    legacy.mkdir(parents=True)
    (legacy / "MEMORY.md").write_text(
        "# Memory\n\n## General\n\n- Order 111 is delayed\n", encoding="utf-8"
    )

    tools = make_customer_memory_tools("supportbot")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "recall").invoke({"query": "order"})
    assert "111" in out
    assert not legacy.exists()
    assert (_customer_dir(home, "supportbot", "telegram-alice") / "MEMORY.md").exists()


def test_legacy_store_is_not_adopted_when_a_platform_store_exists(home: Path):
    """Once a platform-qualified store exists it wins; a stray bare-id dir must
    never overwrite or merge into it."""
    platform_store = _customer_dir(home, "supportbot", "telegram-alice")
    platform_store.mkdir(parents=True)
    (platform_store / "MEMORY.md").write_text(
        "# Memory\n\n## General\n\n- fresh fact\n", encoding="utf-8"
    )
    legacy = _customer_dir(home, "supportbot", "alice")
    legacy.mkdir(parents=True)
    (legacy / "MEMORY.md").write_text("# Memory\n\n## General\n\n- stale fact\n", encoding="utf-8")

    tools = make_customer_memory_tools("supportbot")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "recall").invoke({"query": "fact"})
    assert "fresh" in out
    assert "stale" not in out
    assert legacy.exists()  # left alone for a manual merge


def test_no_customer_in_context_refuses(home: Path):
    tools = make_customer_memory_tools("supportbot")
    # Default context has user_id="owner" which IS a valid bucket, so use an
    # empty user_id to simulate a context with no identified customer.
    with push_request_context(user_id=""):
        out = _tool(tools, "remember").invoke({"fact": "x"})
        assert "No identified customer" in out
        assert not customers_base("supportbot").exists()


def test_forget_me_deletes_only_that_customer(home: Path):
    tools = make_customer_memory_tools("supportbot")
    remember = _tool(tools, "remember")
    forget = _tool(tools, "forget_me")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        remember.invoke({"fact": "keep me out of bob's view"})
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        remember.invoke({"fact": "bob stays"})

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        forget.invoke({})

    assert not _customer_dir(home, "supportbot", "telegram-alice").exists()
    assert (_customer_dir(home, "supportbot", "telegram-bob") / "MEMORY.md").exists()


def test_forget_me_also_purges_an_unadopted_legacy_store(home: Path):
    """An erasure request must catch data written before the platform
    qualifier, still sitting under the bare sender id — but never another
    platform's store for the same digits."""
    other_platform = _customer_dir(home, "supportbot", "whatsapp-alice")
    other_platform.mkdir(parents=True)
    (other_platform / "MEMORY.md").write_text("# Memory\n\n- separate person\n", encoding="utf-8")

    tools = make_customer_memory_tools("supportbot")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "remember").invoke({"fact": "new data"})

    # A leftover pre-platform directory (e.g. restored from a backup after the
    # platform store already existed, so it was never adopted).
    legacy = _customer_dir(home, "supportbot", "alice")
    legacy.mkdir(parents=True)
    (legacy / "MEMORY.md").write_text("# Memory\n\n- old data\n", encoding="utf-8")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "forget_me").invoke({})

    assert not _customer_dir(home, "supportbot", "telegram-alice").exists()
    assert not legacy.exists()
    # The same digits on another platform are a different customer — untouched.
    assert (other_platform / "MEMORY.md").exists()


def test_legacy_store_named_by_the_old_sanitizer_is_adopted_and_erased(home: Path):
    """Pre-platform directories were named by ``_sanitize_id``, which spells an
    unsafe id differently than ``sanitize_identity`` does (no digest suffix).
    Adoption and ``forget_me`` must look for THAT spelling, or exactly the users
    whose ids the old sanitizer altered — every WhatsApp JID — keep stranded
    data that an erasure request never removes."""
    raw = "12025550188@s.whatsapp.net"
    legacy_name = _sanitize_id(raw)
    assert legacy_name != sanitize_identity(raw)  # the divergent case

    legacy = _customer_dir(home, "supportbot", legacy_name)
    legacy.mkdir(parents=True)
    (legacy / "MEMORY.md").write_text(
        "# Memory\n\n## General\n\n- Order 111 is delayed\n", encoding="utf-8"
    )

    tools = make_customer_memory_tools("supportbot")
    with push_request_context(source="channel", channel="whatsapp", user_id=raw):
        out = _tool(tools, "recall").invoke({"query": "order"})
        assert "111" in out  # history carried over via adoption
        assert not legacy.exists()

        # And an erasure request removes everything, including a re-appeared
        # legacy directory (e.g. restored from a backup).
        legacy.mkdir(parents=True)
        (legacy / "MEMORY.md").write_text("# Memory\n\n- old data\n", encoding="utf-8")
        _tool(tools, "forget_me").invoke({})

    adopted = _customer_dir(home, "supportbot", f"whatsapp-{sanitize_identity(raw)}")
    assert not adopted.exists()
    assert not legacy.exists()


# ─────────────────────────────── shared knowledge ────────────────────────────


def test_shared_knowledge_search_and_read(tmp_path: Path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "refunds.md").write_text(
        "# Refund Policy\n\nRefunds are processed within 14 days.\n",
        encoding="utf-8",
    )
    tools = make_shared_knowledge_tools(tmp_path)

    out = _tool(tools, "knowledge_search").invoke({"query": "refund"})
    assert "14 days" in out
    assert "refunds" in out  # source doc stem

    listed = _tool(tools, "knowledge_list").invoke({})
    assert "refunds" in listed

    body = _tool(tools, "knowledge_read").invoke({"doc": "refunds"})
    assert "Refund Policy" in body


def test_shared_knowledge_missing_dir_is_graceful(tmp_path: Path):
    tools = make_shared_knowledge_tools(tmp_path)
    out = _tool(tools, "knowledge_search").invoke({"query": "anything"})
    assert "empty" in out.lower()


# ─────────────────────────────── builder wiring ──────────────────────────────


def test_builder_registers_customer_tools_when_per_customer(home: Path, tmp_path: Path):
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_memory_tools

    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = AgentConfig(name="supportbot")
    config.memory.per_customer = True
    config.memory.markdown = False

    result = _build_memory_tools(config, workspace, categorized=True)
    names = {t.name for t in result["memory"]}
    assert {"remember", "recall", "forget_me"} <= names
    # The shared MEMORY.md tools must NOT be present in per-customer mode.
    assert "memory_save" not in names


# ──────────────────── file tools are denied the customer store ───────────────
#
# The store now lives INSIDE the agent workspace, so without an explicit deny
# rule one customer's conversation could read every other customer's notes with
# `read_file` / `grep`. deepagents takes the FIRST matching rule.


def _permission_verdict(rules, operation: str, path: str) -> str:
    from deepagents.middleware.filesystem import _check_fs_permission

    return _check_fs_permission(rules, operation, path)


def _agent_workspace(home: Path, agent: str) -> Path:
    ws = home / "agents" / agent / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.mark.parametrize("access_mode", ["read", "read_write"])
def test_customer_store_is_denied_to_the_agents_file_tools(home: Path, access_mode: str):
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_permissions

    workspace = _agent_workspace(home, "supportbot")
    config = AgentConfig(
        name="supportbot",
        backend={"type": "filesystem", "root_dir": str(workspace), "access_mode": access_mode},
    )
    config.memory.per_customer = True

    rules = _build_permissions(config, workspace)
    assert rules is not None
    victim = str(workspace / "customers" / "bob" / "MEMORY.md")
    for operation in ("read", "write"):
        assert _permission_verdict(rules, operation, victim) == "deny"
    # The dir itself too, so `ls` can't enumerate who the other customers are.
    assert _permission_verdict(rules, "read", str(workspace / "customers")) == "deny"
    # Ordinary workspace reads still work (in read mode nothing else is denied).
    assert _permission_verdict(rules, "read", str(workspace / "MEMORY.md")) == "allow"


def test_customer_deny_rule_is_absent_without_per_customer(home: Path):
    """A normal agent that happens to have a `customers/` folder keeps using it."""
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_permissions

    workspace = _agent_workspace(home, "merli")
    config = AgentConfig(
        name="merli",
        backend={"type": "filesystem", "root_dir": str(workspace), "access_mode": "read_write"},
    )

    rules = _build_permissions(config, workspace)
    assert rules is not None
    path = str(workspace / "customers" / "notes.md")
    assert _permission_verdict(rules, "read", path) == "allow"
    assert _permission_verdict(rules, "write", path) == "allow"


@pytest.mark.parametrize(
    "asked",
    [
        "customers/bob/MEMORY.md",
        "./customers/bob/MEMORY.md",
        "projects/../customers/bob/MEMORY.md",
        "customers/bob",
        # A bare directory path is a listing: `Workspace.read_file` renders a
        # directory as its entries, i.e. the other customers' ids.
        "customers",
    ],
)
def test_workspace_context_refuses_another_customers_memory(home: Path, asked: str):
    """`workspace_context` reads model-supplied workspace paths straight off disk,
    outside the backend's permission rules — the one workspace-relative way left
    to reach another customer's store."""
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    workspace = _agent_workspace(home, "supportbot")
    (workspace / "USER.md").write_text("# User\n\nOwner notes.\n", encoding="utf-8")
    victim = workspace / "customers" / "bob"
    victim.mkdir(parents=True)
    (victim / "MEMORY.md").write_text("# Memory\n\n- Card ending 4242\n", encoding="utf-8")

    config = AgentConfig(name="supportbot", workspace=str(workspace))
    config.memory.per_customer = True
    assembler = PromptAssembler(config, Workspace(root=workspace))

    out = assembler.build_l2(file_paths=[asked, "USER.md"])
    assert "4242" not in out
    assert "Owner notes" in out  # the legitimate path in the same call still loads


def test_workspace_context_still_reads_customers_for_a_normal_agent(home: Path):
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    workspace = _agent_workspace(home, "merli")
    (workspace / "customers").mkdir()
    (workspace / "customers" / "leads.md").write_text("# Leads\n\n- Acme\n", encoding="utf-8")

    config = AgentConfig(name="merli", workspace=str(workspace))
    assembler = PromptAssembler(config, Workspace(root=workspace))

    assert "Acme" in assembler.build_l2(file_paths=["customers/leads.md"])


def test_cartography_never_maps_the_customer_store(tmp_path: Path):
    """Customer ids (and their memory filenames) must not reach the L0 map."""
    from olano.cartography.map_generator import generate_workspace_map

    (tmp_path / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    cust = tmp_path / "customers" / "alice-449281"
    cust.mkdir(parents=True)
    (cust / "MEMORY.md").write_text("# Memory\n\n- Order 111 is delayed\n", encoding="utf-8")

    out = generate_workspace_map(tmp_path)
    assert "alice-449281" not in out
    assert "Order 111" not in out


# ─────────────────────────── archetype lockdown ──────────────────────────────


def test_customer_service_archetype_locks_down_sensitive_tools(home: Path, tmp_path: Path):
    """The shipped CustomerServiceAgent archetype must stay sealed: no vault,
    shell, trash, web/HTTP/browser, peer-messaging, or config-disclosure tools —
    only the per-customer memory + shared knowledge surface."""
    from olano.archetypes.installer import load_archetype_template
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_extra_tools

    template = load_archetype_template("customer-service")
    config = AgentConfig(**template)

    disabled = set(config.disabled_tools)
    assert {
        # peer messaging / delegation
        "talk_to_agent",
        "talk_to",
        "delegate_task",
        # web / HTTP / browser
        "web_search",
        "web_fetch",
        "http_request",
        "url_download",
        "browser_navigate",
        # credentials vault
        "secret_get",
        "secret_list",
        "secret_set",
        # shell / code execution
        "safe_shell",
        "node_run",
        "execute",
        # trash tools bypass the read-only backend permission layer
        "trash_put",
        "trash_list",
        "trash_restore",
        "trash_empty",
        # internal-state disclosure + outbound email
        "whoami",
        "workspace_context",
        "gmail_send",
        # universal defaults with no place in a customer conversation
        "wait_n_seconds",
        "document_to_text",
        "pdf_to_text",
    } <= disabled

    # None of the disabled names survive the universal-defaults merge.
    assert not disabled & set(config.tools)
    # No builtin subagents → no OpsManager, so the `task` surface is never bound.
    assert config.subagents == []
    # Privacy config stays on.
    assert config.memory.per_customer is True
    assert config.human_only is True

    workspace = tmp_path / "ws"
    workspace.mkdir()
    tools = _build_extra_tools(config, workspace)
    names = {t.name for t in tools}
    # The per-customer memory + shared knowledge tools are the working surface…
    assert {
        "remember",
        "recall",
        "recall_all",
        "forget_me",
        "knowledge_search",
        "knowledge_list",
        "knowledge_read",
    } <= names
    # …and nothing on the deny-list leaks through — in particular the vault
    # tools the builder injects unconditionally.
    assert not names & disabled


def test_customer_service_archetype_default_tool_surface(home: Path, tmp_path: Path):
    """The archetype ships exactly nine default tools plus the shared read-only
    knowledge lookup — the smallest set that can run a support conversation.

    Four of the nine (`remember`/`recall`/`recall_all`/`forget_me`) are built
    per-agent from `memory.per_customer` and pinned in `top_level_memory_tools`,
    so they are absent from `config.tools` by design; the other five are listed
    in the template. Anything beyond this set is a regression — every addition
    widens what a stranger on a shared channel can reach.
    """
    from olano.archetypes.installer import load_archetype_template
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_extra_tools

    config = AgentConfig(**load_archetype_template("customer-service"))

    assert set(config.tools) == {
        "get_current_time",
        "get_current_date",
        "notification_send_text",
        "notification_send_photo",
        "notification_send_document",
    }

    workspace = tmp_path / "ws"
    workspace.mkdir()
    bound = {t.name for t in _build_extra_tools(config, workspace)}
    assert bound == {
        # privacy-isolated per-customer memory
        "remember",
        "recall",
        "recall_all",
        "forget_me",
        # shared read-only knowledge base the system prompt relies on
        "knowledge_search",
        "knowledge_list",
        "knowledge_read",
        # the customer's own file store
        "read_customer_file",
        "write_customer_file",
        "list_customer_files",
        # the customer's own delivery switches ("say stop any time")
        "mute_my_notifications",
        "resume_my_notifications",
        # time + outbound delivery on the bot's own channel. `get_current_date`
        # is an alias of `get_current_time` (registry._ALIASES), so the model is
        # bound to one clock tool even though both names appear in `tools`.
        "get_current_time",
        "notification_send_text",
        "notification_send_photo",
        "notification_send_document",
    }


# ───────────── multi-user mode on an execution-capable backend ───────────────
#
# deepagents refuses permission rules on any backend that can execute commands
# (it cannot enforce them on `execute`), so the customers/** deny rule that
# multi-user mode advertises is simply not installed there. That is a real hole
# in the isolation story for exactly the agents most likely to be sold to
# strangers, so the builder must say so at boot instead of failing silently.


@pytest.mark.parametrize(
    "backend",
    [
        {"type": "local_shell", "root_dir": "workspace"},
        # The Docker sandbox takes no root_dir — the mount plan defines its view.
        {"type": "lionclaw_docker"},
    ],
    ids=["local_shell", "lionclaw_docker"],
)
def test_execution_backend_warns_that_the_customer_deny_rule_is_absent(
    home: Path, caplog, backend: dict
):
    import logging

    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_permissions

    workspace = _agent_workspace(home, "supportbot")
    config = AgentConfig(name="supportbot", backend=backend)
    config.memory.per_customer = True

    with caplog.at_level(logging.WARNING, logger="olano.core.builder"):
        rules = _build_permissions(config, workspace)

    # No rules are installed — that is the deepagents constraint, not a bug…
    assert rules is None
    # …but the operator is told, and told what to do about it.
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "multi-user mode is ON" in warning
    assert backend["type"] in warning
    assert "filesystem" in warning  # points at the backend that does carry it


def test_no_warning_for_an_execution_backend_without_multi_user_mode(home: Path, caplog):
    """A single-user agent on a shell backend is the normal case — stay quiet."""
    import logging

    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_permissions

    workspace = _agent_workspace(home, "merli")
    config = AgentConfig(name="merli", backend={"type": "local_shell", "root_dir": str(workspace)})

    with caplog.at_level(logging.WARNING, logger="olano.core.builder"):
        assert _build_permissions(config, workspace) is None
    assert not [r for r in caplog.records if "multi-user mode" in str(r.message)]
