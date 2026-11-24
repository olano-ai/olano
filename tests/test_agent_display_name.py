"""The agent's display name: the fallback, and where it is allowed to reach.

`AgentConfig.name` is the immutable handle — folder, thread scope, registry
key, API path, `ai-<name>` principal — and has no rename endpoint. Everything
here exists so an agent can be *called* something else without any of that
moving.
"""

from __future__ import annotations

from unittest import mock

from olano.config import json5_fast
from olano.config.agent_config import AgentConfig
from olano.config.workspace import get_workspace
from olano.core.prompt_assembler import PromptAssembler
from olano.issues.principals import agent_principal

# ------------------------------------------------------------------- label


def test_label_falls_back_to_name():
    assert AgentConfig(name="merli").label == "merli"


def test_display_name_is_never_empty():
    """Blank coerces to the handle **verbatim** — never a prettified variant.

    This is also the migration for pre-field agent.json5 files: every load
    fills it, the next save persists it. Verbatim is load-bearing: whoami's
    `Handle:` line, the prompt's "You are called" line, the roster's bold face
    and the detail header's second line all gate on `label != name`, so a
    beautified auto-fill ("Merli") would flip all four for every existing
    agent on upgrade.
    """
    assert AgentConfig(name="merli").display_name == "merli"
    assert AgentConfig(name="merli", display_name="  ").display_name == "merli"


def test_label_prefers_display_name():
    assert AgentConfig(name="merli", display_name="Merli the Analyst").label == "Merli the Analyst"


def test_whitespace_only_display_name_is_not_a_name():
    assert AgentConfig(name="merli", display_name="   ").label == "merli"


def test_display_name_is_stored_trimmed():
    """So the dashboard round-trips the value it renders, padding and all."""
    assert AgentConfig(name="merli", display_name="  Merli  ").display_name == "Merli"


def test_label_never_round_trips_into_agent_json5():
    """`label` is derived. Persisting it would create a second, staler copy of
    the answer that `save_agent_config` would then write back to disk."""
    dumped = AgentConfig(name="merli", display_name="Merli").model_dump()
    assert "label" not in dumped
    assert dumped["display_name"] == "Merli"


def test_config_round_trips_through_save_and_load(tmp_path):
    from olano.config.loader import save_agent_config

    cfg = AgentConfig(name="merli", display_name="Merli the Analyst")
    path = save_agent_config(cfg, base=tmp_path)

    raw = path.read_text()
    assert '"display_name": "Merli the Analyst"' in raw
    # A derived value on disk is a second copy of the answer that can go stale.
    assert '"label"' not in raw

    reloaded = AgentConfig.model_validate(json5_fast.loads(raw))
    assert reloaded.label == "Merli the Analyst"
    assert reloaded.name == "merli"


# ------------------------------------------------------ the handle is intact


def test_principal_is_built_from_the_handle_not_the_display_name():
    """An editable field must never key the issue audit log."""
    cfg = AgentConfig(name="merli", display_name="Merli the Analyst")
    assert agent_principal(cfg.name) == "ai-merli"


# -------------------------------------------------------------------- whoami


def _whoami(cfg: AgentConfig) -> str:
    from olano.tools.agent_tools import make_whoami_tool

    return make_whoami_tool(cfg).func()  # type: ignore[attr-defined]


def test_whoami_reports_the_display_name_and_the_handle():
    out = _whoami(AgentConfig(name="merli", display_name="Merli the Analyst"))
    assert "Name: Merli the Analyst" in out
    # The handle is what talk_to_agent and every API path take, so an agent
    # that answers only with its pretty name has given an unusable answer.
    assert "Handle: merli" in out


def test_whoami_omits_the_handle_line_when_it_would_just_repeat_the_name():
    out = _whoami(AgentConfig(name="merli"))
    assert "Name: merli" in out
    assert "Handle:" not in out


def test_whoami_lists_magic_link_tools_only_under_the_capability():
    """whoami reports the builder-granted magic-link family under the same
    predicate the builder binds it with, so the inventory never disagrees."""
    assert "magic_link_request_secret" in _whoami(AgentConfig(name="merli"))
    off = AgentConfig(name="merli", autonomy={"magic_links": False})
    assert "magic_link_request_secret" not in _whoami(off)


# ------------------------------------------------------------------- prompt


def _identity_block(cfg: AgentConfig, root) -> str:
    from olano.core.prompt_assembler import PromptAssembler

    return PromptAssembler(cfg, get_workspace(str(root)))._build_identity_block()


def test_prompt_is_byte_identical_when_no_display_name_is_set(tmp_workspace):
    """Every agent that predates this field must see exactly the prompt it saw
    before — the generated line is opt-in, not a new baseline."""
    plain = _identity_block(AgentConfig(name="merli", workspace=str(tmp_workspace)), tmp_workspace)
    same = _identity_block(
        AgentConfig(name="merli", display_name="merli", workspace=str(tmp_workspace)),
        tmp_workspace,
    )
    assert "You are called" not in plain
    # display_name == name is not a difference, so it earns no line either.
    assert same == plain


def test_prompt_names_the_agent_and_its_handle_when_they_differ(tmp_workspace):
    block = _identity_block(
        AgentConfig(name="merli", display_name="Merli the Analyst", workspace=str(tmp_workspace)),
        tmp_workspace,
    )
    assert "You are called **Merli the Analyst**." in block
    assert "`merli`" in block


# -------------------------------------------------------------- peer roster


def test_roster_shows_the_display_name_and_the_handle_together(tmp_workspace, monkeypatch):
    """The face is what a user will say; the backticked handle is what
    `talk_to_agent` takes. Dropping either one strands the model."""
    cfg = AgentConfig(
        name="merli",
        workspace=str(tmp_workspace),
        tools=["talk_to_agent"],
        agent_visibility={"mode": "all"},
    )
    monkeypatch.setattr(
        "olano.tools.talk_to.list_peers_info",
        lambda: [
            {
                "name": "kevin",
                "label": "Kevin the Ops Lead",
                "description": "ops",
                "emoji": "🦁",
                "tools": [],
                "hidden": False,
                "has_shell": False,
                "is_admin": False,
            }
        ],
    )
    monkeypatch.setattr("olano.tools.talk_to.can_message", lambda a, b: True)

    block = PromptAssembler(cfg, get_workspace(str(tmp_workspace)))._build_agent_roster_block()
    assert "**Kevin the Ops Lead**" in block
    assert "`kevin`" in block


def test_roster_shows_the_handle_alone_when_there_is_no_display_name(tmp_workspace, monkeypatch):
    cfg = AgentConfig(
        name="merli",
        workspace=str(tmp_workspace),
        tools=["talk_to_agent"],
        agent_visibility={"mode": "all"},
    )
    monkeypatch.setattr(
        "olano.tools.talk_to.list_peers_info",
        lambda: [
            {
                "name": "kevin",
                "label": "kevin",
                "description": "ops",
                "emoji": "🦁",
                "tools": [],
                "hidden": False,
                "has_shell": False,
                "is_admin": False,
            }
        ],
    )
    monkeypatch.setattr("olano.tools.talk_to.can_message", lambda a, b: True)

    block = PromptAssembler(cfg, get_workspace(str(tmp_workspace)))._build_agent_roster_block()
    assert "`kevin`" in block
    assert "**kevin**" not in block


# ------------------------------------------------- channel-facing self-name


def _adapter(agent_name: str):
    from olano.platforms.base import BasePlatformAdapter
    from olano.platforms.config import Platform, PlatformConfig

    class _Probe(BasePlatformAdapter):
        async def connect(self): ...
        async def disconnect(self): ...
        async def send(self, target, text, **kwargs): ...
        async def get_chat_info(self, chat_id): ...

    return _Probe(PlatformConfig(), Platform.EMAIL, agent_name=agent_name)


def test_adapter_agent_label_resolves_the_display_name():
    """What an outbound email puts in its From: line."""
    with mock.patch(
        "olano.config.loader.load_agent_config",
        lambda name, **kw: AgentConfig(name="merli", display_name="Merli the Analyst"),
    ):
        assert _adapter("merli").agent_label == "Merli the Analyst"


def test_adapter_agent_label_falls_back_to_the_handle_when_config_is_unreadable():
    """A missing pretty name must never be the reason a send fails."""
    with mock.patch(
        "olano.config.loader.load_agent_config",
        side_effect=FileNotFoundError("no such agent"),
    ):
        assert _adapter("merli").agent_label == "merli"


def test_adapter_agent_label_is_empty_without_an_agent():
    assert _adapter("").agent_label == ""


# --------------------------------------------------------- archetype installs


def test_archetype_install_maps_label_to_display_name(lionclaw_home):
    """Every archetype template already carries the human name as `label`
    ("Customer Service Agent" beside name "CustomerServiceAgent") — until now
    read only by the catalog picker, so installed agents went by their ugly
    handle everywhere. The install maps it through."""
    from olano.archetypes.installer import install_archetype

    path = install_archetype("customer-service", agent_name="support-bot")
    raw = json5_fast.loads(path.read_text())
    assert raw["display_name"] == "Customer Service Agent"
    assert raw["name"] == "support-bot"
    # And the scaffolded identity introduces the agent by that name.
    ident = (path.parent / "workspace" / "IDENTITY.md").read_text()
    assert "# Identity — Customer Service Agent" in ident


def test_archetype_install_explicit_display_name_beats_the_label(lionclaw_home):
    from olano.archetypes.installer import install_archetype

    path = install_archetype(
        "customer-service", agent_name="helpdesk", display_name="Ana's Help Desk"
    )
    assert json5_fast.loads(path.read_text())["display_name"] == "Ana's Help Desk"


def test_wizard_edit_pass_preserves_a_dashboard_rename(lionclaw_home):
    """`olano setup` edit mode rewrites agent.json5 from the template; a
    display name the owner set in the dashboard must survive that pass."""
    from olano.cli.main import _install_or_update_archetype_agent

    kw = dict(
        archetype_name="customer-service",
        agent_id="support-bot",
        emoji="🦁",
        security_preset="safe",
    )
    path = _install_or_update_archetype_agent(
        overwrite_existing=False, display_name="Ana's Desk", **kw
    )
    assert json5_fast.loads(path.read_text())["display_name"] == "Ana's Desk"

    # The edit pass types nothing — the rename must not revert to the label.
    path = _install_or_update_archetype_agent(overwrite_existing=True, display_name="", **kw)
    assert json5_fast.loads(path.read_text())["display_name"] == "Ana's Desk"


def test_wizard_slug_shaped_name_defers_to_the_archetype_label(lionclaw_home):
    """A typed name that IS its own slug carries no human shape — the CLI must
    produce the same display name the dashboard install of the same archetype
    does, not defeat the label mapping with the slug."""
    from olano.cli.main import _install_or_update_archetype_agent, _slugify_agent_name

    entered = "customer-service"  # what a --yes run defaults to
    assert _slugify_agent_name(entered) == entered  # the guard's premise
    path = _install_or_update_archetype_agent(
        archetype_name="customer-service",
        agent_id=entered,
        emoji="🦁",
        security_preset="safe",
        overwrite_existing=False,
        display_name="",  # what the call sites pass for a slug-shaped name
    )
    assert json5_fast.loads(path.read_text())["display_name"] == "Customer Service Agent"


# ------------------------------------------------------------------- title
#
# The third half of the identity: `org.title`, the agent's *position*
# ("Recruitment Specialist"). An archetype's human label is the job it was
# written for, so an install keeps it as the title when the person names the
# agent something else — the display name says who, the title says what.


def _title_cfg(name: str, display: str = "", title: str = "", **kw) -> AgentConfig:
    return AgentConfig(name=name, display_name=display, org={"title": title} if title else {}, **kw)


def test_archetype_install_defaults_the_title_to_the_label(lionclaw_home):
    """A person installs the Customer Service archetype as "Hillary": she is
    called Hillary, her title is the archetype's label, and the file holds
    that answer exactly once (in `org.title`)."""
    from olano.archetypes.installer import install_archetype

    path = install_archetype("customer-service", agent_name="hillary", display_name="Hillary")
    raw = json5_fast.loads(path.read_text())
    assert raw["display_name"] == "Hillary"
    assert raw["org"]["title"] == "Customer Service Agent"
    assert "title" not in raw  # never a second, top-level copy
    # The scaffolded identity carries both halves.
    ident = (path.parent / "workspace" / "IDENTITY.md").read_text()
    assert "**Name:** Hillary" in ident
    assert "**Title:** Customer Service Agent" in ident


def test_archetype_install_explicit_title_beats_the_label(lionclaw_home):
    from olano.archetypes.installer import install_archetype

    path = install_archetype(
        "customer-service", agent_name="helpdesk", display_name="Ana", title="Support Lead"
    )
    assert json5_fast.loads(path.read_text())["org"]["title"] == "Support Lead"


def test_template_title_key_wins_over_the_label():
    """The pack may carry an explicit `title` beside `label` — a team labelled
    "LionHR" can hold the position "HR Operations Lead"."""
    from olano.archetypes.installer import apply_template_title

    template = {"name": "x", "label": "LionHR", "title": "HR Operations Lead"}
    assert apply_template_title(template) == "HR Operations Lead"
    assert template["org"]["title"] == "HR Operations Lead"
    assert "title" not in template


def test_an_authored_org_title_survives_unless_overridden():
    from olano.archetypes.installer import apply_template_title

    template = {"name": "x", "label": "Sales Bot", "org": {"title": "Account Executive"}}
    assert apply_template_title(template) == "Account Executive"
    assert apply_template_title(dict(template), "Closer") == "Closer"


def test_a_template_with_no_label_invents_no_title():
    from olano.archetypes.installer import apply_template_title

    template = {"name": "blank"}
    assert apply_template_title(template) == ""
    assert "org" not in template


def test_prompt_identity_line_carries_the_title(tmp_workspace):
    """The archetype's own prompt says "You are RecruitmentSpecialist"; the
    identity line has to say which of the two is the name. Otherwise an agent
    someone named Hillary introduces herself by her job title — the exact
    thing this field exists to stop."""
    block = _identity_block(
        _title_cfg("hillary", "Hillary", "Recruitment Specialist", workspace=str(tmp_workspace)),
        tmp_workspace,
    )
    assert "You are called **Hillary**. Your title is Recruitment Specialist." in block
    assert "`hillary`" in block
    assert "Introduce yourself by your name" in block


def test_prompt_states_the_title_alone_when_the_agent_goes_by_its_handle(tmp_workspace):
    block = _identity_block(
        _title_cfg("hillary", "", "Recruitment Specialist", workspace=str(tmp_workspace)),
        tmp_workspace,
    )
    assert "Your title is Recruitment Specialist." in block
    assert "You are called" not in block


def test_prompt_is_unchanged_for_an_agent_with_no_title(tmp_workspace):
    block = _identity_block(
        AgentConfig(name="merli", display_name="Merli the Analyst", workspace=str(tmp_workspace)),
        tmp_workspace,
    )
    assert "Your title" not in block


def test_central_org_title_wins_over_the_config_block(lionclaw_home, tmp_workspace):
    """organization.yaml is edited live from the Talk to Agent tab; the prompt
    and whoami must follow it without a restart, exactly as the org chart does."""
    from olano.config import org_store
    from olano.config.agent_config import OrgConfig
    from olano.config.loader import save_agent_config

    cfg = _title_cfg("hillary", "Hillary", "Recruitment Specialist", workspace=str(tmp_workspace))
    save_agent_config(cfg)
    assert org_store.resolve_title("hillary", cfg.org) == "Recruitment Specialist"
    org_store.update_org("hillary", OrgConfig(title="Head of Talent"))
    assert org_store.resolve_title("hillary", cfg.org) == "Head of Talent"
    assert "Your title is Head of Talent." in _identity_block(cfg, tmp_workspace)
    assert "Title: Head of Talent" in _whoami(cfg)


def test_whoami_reports_the_title():
    out = _whoami(_title_cfg("hillary", "Hillary", "Recruitment Specialist"))
    assert "Name: Hillary" in out
    assert "Title: Recruitment Specialist" in out
    assert "Handle: hillary" in out


def test_whoami_omits_the_title_line_when_there_is_none():
    assert "Title:" not in _whoami(AgentConfig(name="merli"))


def test_roster_shows_the_peers_title(tmp_workspace, monkeypatch):
    """ "Ask the recruiter" must resolve as readily as "ask Hillary"."""
    cfg = AgentConfig(
        name="merli",
        workspace=str(tmp_workspace),
        tools=["talk_to_agent"],
        agent_visibility={"mode": "all"},
    )
    monkeypatch.setattr(
        "olano.tools.talk_to.list_peers_info",
        lambda: [
            {
                "name": "hillary",
                "label": "Hillary",
                "title": "Recruitment Specialist",
                "description": "hiring",
                "emoji": "🎯",
                "tools": [],
                "hidden": False,
                "has_shell": False,
                "is_admin": False,
            }
        ],
    )
    monkeypatch.setattr("olano.tools.talk_to.can_message", lambda a, b: True)
    block = PromptAssembler(cfg, get_workspace(str(tmp_workspace)))._build_agent_roster_block()
    assert "**Hillary** (`hillary`), Recruitment Specialist — hiring" in block


def test_peer_info_carries_the_title(tmp_workspace, monkeypatch):
    from olano.tools import talk_to as tt

    peer = _title_cfg("hillary", "Hillary", "Recruitment Specialist", workspace=str(tmp_workspace))
    runner = mock.MagicMock()
    runner.config = peer
    monkeypatch.setattr(tt, "_RUNTIME", mock.MagicMock(_runners={"hillary": runner}))
    (info,) = tt.list_peers_info()
    assert info["title"] == "Recruitment Specialist"
    assert info["label"] == "Hillary"
