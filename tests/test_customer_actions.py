"""Tests for the identity-injected per-user action and file tools.

The point of these tools is that the model cannot choose *whose* data it acts
on: the platform and sender id come from the authenticated request context, so
there is no parameter for a prompt injection to manipulate. Most of what is
asserted here is that property, from several angles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.security.request_context import push_request_context
from olano.tools.customer_actions import make_customer_action_tools

AGENT = "supportbot"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    return tmp_path


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _customer_dir(home: Path, customer: str) -> Path:
    return home / "agents" / AGENT / "workspace" / "customers" / customer


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


#: Echoes the injected identity + its inputs, and writes into cwd — enough to
#: prove which user the action actually ran as.
ECHO = """\
import json, os, sys
payload = json.load(sys.stdin)
open("touched.txt", "w").write("ran")
print(json.dumps({
    "inputs": payload,
    "cwd": os.getcwd(),
    "customer_id": os.environ.get("LIONCLAW_CUSTOMER_ID"),
    "customer_key": os.environ.get("LIONCLAW_CUSTOMER_KEY"),
    "platform": os.environ.get("LIONCLAW_CUSTOMER_PLATFORM"),
}))
"""


# ───────────────────────────── execute_action ────────────────────────────────


def test_action_runs_as_the_context_user_not_a_parameter(home: Path, tmp_path: Path):
    tools = make_customer_action_tools(
        AGENT, {"echo": str(_script(tmp_path, "e.py", ECHO))}, [tmp_path]
    )
    run = _tool(tools, "execute_action")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = json.loads(run.invoke({"action": "echo", "inputs": {"x": 1}}))

    # KEY names the store; ID names the person to their platform. Keeping
    # them apart stops a script handing the folder name to a delivery API.
    assert out["customer_key"] == "telegram-alice"
    assert out["customer_id"] == "alice"
    assert out["platform"] == "telegram"
    assert out["inputs"] == {"x": 1}
    # cwd is that user's own folder, so a script writing a relative path is
    # automatically scoped to them.
    assert Path(out["cwd"]).resolve() == _customer_dir(home, "telegram-alice").resolve()
    assert (_customer_dir(home, "telegram-alice") / "touched.txt").exists()


def test_execute_action_takes_no_user_or_platform_parameter(home: Path, tmp_path: Path):
    """The whole security model: there is nothing in the schema to forge."""
    tools = make_customer_action_tools(
        AGENT, {"echo": str(_script(tmp_path, "e.py", ECHO))}, [tmp_path]
    )
    schema = _tool(tools, "execute_action").args_schema.model_json_schema()
    assert set(schema["properties"]) == {"action", "inputs"}


def test_a_forged_id_in_inputs_does_not_change_whose_folder_is_used(home: Path, tmp_path: Path):
    """A prompt-injected 'run this for user bob' reaches the script only as
    inert data — the folder and injected identity stay the caller's."""
    tools = make_customer_action_tools(
        AGENT, {"echo": str(_script(tmp_path, "e.py", ECHO))}, [tmp_path]
    )
    run = _tool(tools, "execute_action")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = json.loads(
            run.invoke(
                {
                    "action": "echo",
                    "inputs": {"customer_id": "telegram-bob", "platform": "whatsapp"},
                }
            )
        )

    assert out["customer_key"] == "telegram-alice"
    assert out["customer_id"] == "alice"
    assert out["platform"] == "telegram"
    assert Path(out["cwd"]).name == "telegram-alice"
    assert not _customer_dir(home, "telegram-bob").exists()


def test_only_allow_listed_actions_can_run(home: Path, tmp_path: Path):
    _script(tmp_path, "secret.py", "print('should never run')")
    tools = make_customer_action_tools(
        AGENT, {"echo": str(_script(tmp_path, "e.py", ECHO))}, [tmp_path]
    )
    run = _tool(tools, "execute_action")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        for attempt in ("secret", "secret.py", str(tmp_path / "secret.py"), "../secret"):
            out = run.invoke({"action": attempt, "inputs": {}})
            assert "Unknown action" in out


def test_action_catalog_is_in_the_tool_description(home: Path, tmp_path: Path):
    tools = make_customer_action_tools(
        AGENT,
        {
            "add_bill": str(_script(tmp_path, "a.py", ECHO)),
            "list_bills": str(_script(tmp_path, "b.py", ECHO)),
        },
        [tmp_path],
    )
    description = _tool(tools, "execute_action").description
    assert "- add_bill" in description
    assert "- list_bills" in description


def test_unresolvable_action_is_dropped_not_offered(home: Path, tmp_path: Path, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="olano.tools.customer_actions"):
        tools = make_customer_action_tools(AGENT, {"ghost": "does/not/exist.py"}, [tmp_path])
    # No runnable action → no runner at all, rather than one that always fails.
    assert not [t for t in tools if t.name == "execute_action"]
    assert "does not resolve to a file" in caplog.text


def test_failing_action_reports_its_error(home: Path, tmp_path: Path):
    boom = _script(tmp_path, "boom.py", "import sys; sys.stderr.write('bad input\\n'); sys.exit(3)")
    tools = make_customer_action_tools(AGENT, {"boom": str(boom)}, [tmp_path])
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "execute_action").invoke({"action": "boom", "inputs": {}})
    assert "exit 3" in out
    assert "bad input" in out


def test_action_timeout_is_bounded(home: Path, tmp_path: Path):
    hang = _script(tmp_path, "hang.py", "import time; time.sleep(30)")
    tools = make_customer_action_tools(AGENT, {"hang": str(hang)}, [tmp_path], timeout=1)
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "execute_action").invoke({"action": "hang", "inputs": {}})
    assert "timed out" in out


def test_no_identified_user_refuses(home: Path, tmp_path: Path):
    tools = make_customer_action_tools(
        AGENT, {"echo": str(_script(tmp_path, "e.py", ECHO))}, [tmp_path]
    )
    with push_request_context(user_id=""):
        assert "No identified user" in _tool(tools, "execute_action").invoke(
            {"action": "echo", "inputs": {}}
        )


# ─────────────────────── read / write / list customer files ──────────────────


def test_files_are_scoped_to_the_context_user(home: Path, tmp_path: Path):
    tools = make_customer_action_tools(AGENT, {}, [tmp_path])
    write, read = _tool(tools, "write_customer_file"), _tool(tools, "read_customer_file")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        write.invoke({"path": "notes.md", "content": "alice's private note"})
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        write.invoke({"path": "notes.md", "content": "bob's private note"})
        assert "bob's" in read.invoke({"path": "notes.md"})
        assert "alice's" not in read.invoke({"path": "notes.md"})

    assert (_customer_dir(home, "telegram-alice") / "notes.md").exists()
    assert (_customer_dir(home, "telegram-bob") / "notes.md").exists()


@pytest.mark.parametrize(
    "escape",
    [
        "../telegram-bob/notes.md",
        "../../MEMORY.md",
        "a/../../telegram-bob/notes.md",
        "/etc/passwd",
    ],
)
def test_file_paths_cannot_escape_the_users_folder(home: Path, tmp_path: Path, escape: str):
    tools = make_customer_action_tools(AGENT, {}, [tmp_path])
    victim = _customer_dir(home, "telegram-bob")
    victim.mkdir(parents=True)
    (victim / "notes.md").write_text("bob's secret", encoding="utf-8")

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        assert "must stay inside" in _tool(tools, "read_customer_file").invoke({"path": escape})
        assert "must stay inside" in _tool(tools, "write_customer_file").invoke(
            {"path": escape, "content": "pwned"}
        )

    assert (victim / "notes.md").read_text(encoding="utf-8") == "bob's secret"


def test_list_customer_files_shows_only_this_user(home: Path, tmp_path: Path):
    tools = make_customer_action_tools(AGENT, {}, [tmp_path])
    write = _tool(tools, "write_customer_file")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        write.invoke({"path": "a.md", "content": "x"})
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        write.invoke({"path": "b.md", "content": "y"})
        listing = _tool(tools, "list_customer_files").invoke({})
    assert "b.md" in listing
    assert "a.md" not in listing


def test_oversized_write_is_refused(home: Path, tmp_path: Path):
    from olano.tools.customer_actions import MAX_WRITE_CHARS

    tools = make_customer_action_tools(AGENT, {}, [tmp_path])
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "write_customer_file").invoke(
            {"path": "big.txt", "content": "x" * (MAX_WRITE_CHARS + 1)}
        )
    assert "too large" in out


# ───────────────────────────── builder wiring ────────────────────────────────


def test_builder_binds_the_action_tools_for_a_multi_user_agent(home: Path, tmp_path: Path):
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_memory_tools

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _script(workspace, "act.py", ECHO)

    config = AgentConfig(name=AGENT, allowed_actions={"echo": "act.py"})
    config.memory.per_customer = True
    config.memory.markdown = False

    cats = _build_memory_tools(config, workspace, categorized=True)
    # Action + file tools live in the "customer" category — exempt from the
    # top_level_memory_tools pin, governed by their own opt-ins only.
    names = {t.name for t in cats["customer"]}
    assert {"execute_action", "read_customer_file", "write_customer_file"} <= names
    # …alongside the memory tools, which share the same identity rule.
    assert {"remember", "recall", "forget_me"} <= {t.name for t in cats["memory"]}


def test_actions_are_not_bound_without_multi_user_mode(home: Path, tmp_path: Path):
    """Without a resolved user there is no folder to scope an action to."""
    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_memory_tools

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _script(workspace, "act.py", ECHO)

    config = AgentConfig(name="merli", allowed_actions={"echo": "act.py"})
    config.memory.per_customer = False

    result = _build_memory_tools(config, workspace, categorized=True)
    names = {t.name for t in result["memory"]}
    assert "execute_action" not in names


# ───────────────────────── subscription status (read-only) ───────────────────


def test_subscription_status_reports_only_the_asker(home: Path):
    """Billing state is scoped to the person talking — there is no user param."""
    import time

    from olano.monetization.store import (
        EntitlementRecord,
        EntitlementStatus,
        get_entitlement_store,
    )
    from olano.monetization.tools import make_subscription_tools

    store = get_entitlement_store()
    store.upsert(
        EntitlementRecord(
            agent=AGENT,
            channel="telegram",
            user_id="alice",
            status=EntitlementStatus.ACTIVE,
            current_period_end=time.time() + 10 * 86400,
        )
    )
    store.upsert(
        EntitlementRecord(
            agent=AGENT, channel="telegram", user_id="bob", status=EntitlementStatus.EXPIRED
        )
    )

    status = _tool(make_subscription_tools(AGENT), "subscription_status")
    assert set(status.args_schema.model_json_schema().get("properties", {})) == set()

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = status.invoke({})
    assert "active" in out.lower()
    assert "10 day" in out or "9 day" in out

    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        out = status.invoke({})
    assert "expired" in out.lower()
    assert "/subscribe" in out


def test_subscription_status_reports_trial_days_left(home: Path):
    """The gate ends a trial at trial_start + trial_days and never sets
    current_period_end on it, so the tool must count from the same start."""
    import time

    from olano.monetization.store import (
        EntitlementRecord,
        EntitlementStatus,
        get_entitlement_store,
    )
    from olano.monetization.tools import make_subscription_tools

    get_entitlement_store().upsert(
        EntitlementRecord(
            agent=AGENT,
            channel="telegram",
            user_id="carol",
            status=EntitlementStatus.TRIAL,
            trial_start=time.time() - 2 * 86400 - 3600,
        )
    )
    status = _tool(make_subscription_tools(AGENT, trial_days=7), "subscription_status")
    with push_request_context(source="channel", channel="telegram", user_id="carol"):
        out = status.invoke({})
    assert "trial" in out.lower()
    # Same rounding as the gate and /trial: 4.96 days remaining reads as 5.
    assert "About 5 day(s) left." in out
    assert "/subscribe" in out


def test_subscription_status_for_an_unknown_user_points_at_subscribe(home: Path):
    from olano.monetization.tools import make_subscription_tools

    status = _tool(make_subscription_tools(AGENT), "subscription_status")
    with push_request_context(source="channel", channel="telegram", user_id="stranger"):
        out = status.invoke({})
    assert "No subscription yet" in out
    assert "/subscribe" in out


# ─────────────────── the full posture: enforced, not disciplined ─────────────


def test_actions_plus_filesystem_backend_gives_an_enforced_boundary(home: Path, tmp_path: Path):
    """The point of `allowed_actions`: with no shell tool the agent can run on a
    plain `filesystem` backend, and THAT is a backend deepagents will install
    permissions for. So the customers/** deny rule is actually active, while
    the identity-injected tools still reach the user's folder directly.

    Under the execution-capable backends this agent used to need
    (local_shell / lionclaw_docker) the same rule is silently dropped — see
    test_execution_backend_warns_* in test_customer_memory.py.
    """
    from deepagents.middleware.filesystem import _check_fs_permission

    from olano.config.agent_config import AgentConfig
    from olano.core.builder import _build_memory_tools, _build_permissions

    workspace = home / "agents" / AGENT / "workspace"
    workspace.mkdir(parents=True)
    _script(workspace, "act.py", ECHO)

    config = AgentConfig(
        name=AGENT,
        backend={"type": "filesystem", "root_dir": str(workspace), "access_mode": "read_write"},
        allowed_actions={"echo": "act.py"},
    )
    config.memory.per_customer = True

    # 1. The deny rule is installed — the generic file tools cannot reach any
    #    user's folder, including one the model names directly.
    rules = _build_permissions(config, workspace)
    assert rules is not None
    victim = str(workspace / "customers" / "telegram-bob" / "MEMORY.md")
    for operation in ("read", "write"):
        assert _check_fs_permission(rules, operation, victim) == "deny"

    # 2. The identity-injected tools still work, because they reach the store
    #    directly rather than through the backend.
    cats = _build_memory_tools(config, workspace, categorized=True)
    names = {t.name for t in [*cats["memory"], *cats["customer"]]}
    assert {"execute_action", "read_customer_file", "write_customer_file"} <= names

    # 3. And there is no shell tool in the bound surface to route around (1).
    assert not names & {"safe_shell", "execute"}
