from __future__ import annotations

from pathlib import Path

import pytest

from olano.archetypes.installer import is_first_run
from olano.config.workspace import Workspace
from olano.core.system_middleware import (
    ONBOARDING_START_TOKEN,
    SESSION_START_TOKEN,
    load_builtin_middleware,
)


@pytest.mark.asyncio
async def test_builtin_onboarding_middleware_injects_kickoff(
    tmp_workspace: Path, base_config
) -> None:
    identity = tmp_workspace / "IDENTITY.md"
    identity.write_text("<!-- lionclaw:first-run -->\n# Identity\n", encoding="utf-8")
    (tmp_workspace / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text("# User\n", encoding="utf-8")

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    result = await hooks.on_input[0](ONBOARDING_START_TOKEN, base_config, context)

    assert "FIRST-RUN ONBOARDING MODE" in result
    assert "Start the conversation now" in result


@pytest.mark.asyncio
async def test_builtin_onboarding_middleware_marks_onboarded_when_saved(
    tmp_workspace: Path, base_config
) -> None:
    identity = tmp_workspace / "IDENTITY.md"
    identity.write_text("<!-- lionclaw:first-run -->\n# Identity\n", encoding="utf-8")
    (tmp_workspace / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text("# User\n", encoding="utf-8")

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    await hooks.on_tool_result[0](
        "user_profile_save", "Saved to USER.md: Preferred name = Carlitos", base_config, context
    )
    await hooks.on_tool_result[0](
        "user_profile_save", "Saved to USER.md: Goals = Build AI infra", base_config, context
    )
    await hooks.on_tool_result[0](
        "soul_preference_save",
        "Saved to SOUL.md: Communication style = Keep it real",
        base_config,
        context,
    )
    await hooks.on_output[0]("All set! Let's get to work.", base_config, context)

    assert "<!-- lionclaw:first-run -->" not in identity.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_builtin_middleware_injects_startup_greeting_for_new_chat(
    tmp_workspace: Path, base_config
) -> None:
    (tmp_workspace / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (tmp_workspace / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text(
        "# User\n\n## Setup Profile\n\n- Preferred name: Carlos\n",
        encoding="utf-8",
    )

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    result = await hooks.on_input[0](SESSION_START_TOKEN, base_config, context)

    assert "START-OF-CHAT GREETING MODE" in result
    assert "at most one short, easy-to-answer question" in result
    assert "Carlos" in result


@pytest.mark.asyncio
async def test_builtin_middleware_skips_startup_greeting_in_none_mode(
    tmp_workspace: Path, base_config
) -> None:
    (tmp_workspace / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (tmp_workspace / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text("# User\n", encoding="utf-8")
    base_config.prompt_mode = "none"

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    result = await hooks.on_input[0](SESSION_START_TOKEN, base_config, context)

    assert result == SESSION_START_TOKEN


@pytest.mark.asyncio
async def test_blank_default_agent_onboarding_ignores_saved_profile_and_uses_woke_up_opening(
    tmp_workspace: Path, base_config
) -> None:
    (tmp_workspace / "IDENTITY.md").write_text(
        "<!-- lionclaw:first-run -->\n# Identity\n\n<!-- lionclaw:blank-persona -->\n**Name:** *Not chosen yet*\n",
        encoding="utf-8",
    )
    (tmp_workspace / "SOUL.md").write_text(
        "# Soul\n\n<!-- lionclaw:blank-persona -->\n", encoding="utf-8"
    )
    (tmp_workspace / "USER.md").write_text(
        "# User\n\n## Setup Profile\n\n- Preferred name: jrivera\n",
        encoding="utf-8",
    )

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    result = await hooks.on_input[0](ONBOARDING_START_TOKEN, base_config, context)

    assert "I just woke up" in result
    assert "Do not act like you already know their name" in result
    assert "jrivera" not in result


@pytest.mark.asyncio
async def test_blank_default_agent_startup_greeting_ignores_saved_profile(
    tmp_workspace: Path, base_config
) -> None:
    (tmp_workspace / "IDENTITY.md").write_text(
        "<!-- lionclaw:first-run -->\n# Identity\n\n<!-- lionclaw:blank-persona -->\n**Name:** *Not chosen yet*\n",
        encoding="utf-8",
    )
    (tmp_workspace / "SOUL.md").write_text(
        "# Soul\n\n<!-- lionclaw:blank-persona -->\n", encoding="utf-8"
    )
    (tmp_workspace / "USER.md").write_text(
        "# User\n\n## Setup Profile\n\n- Preferred name: jrivera\n",
        encoding="utf-8",
    )

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {
        "olano": {
            "onboarding": {
                "workspace_root": str(tmp_workspace),
                "onboarding_active": False,
                "turn_count": 0,
                "skip_requested": False,
                "operational_turns": 0,
                "saved": {
                    "preferred_name": False,
                    "goals": False,
                    "working_style": False,
                    "soul": False,
                    "identity": False,
                },
            }
        }
    }

    result = await hooks.on_input[0](SESSION_START_TOKEN, base_config, context)

    assert "I just woke up" in result
    assert "Do not greet the human by name" in result
    assert "jrivera" not in result


@pytest.mark.asyncio
async def test_blank_default_agent_startup_greeting_uses_saved_context_after_onboarding(
    tmp_workspace: Path, base_config
) -> None:
    (tmp_workspace / "IDENTITY.md").write_text(
        "# Identity\n\n<!-- lionclaw:blank-persona -->\n**Name:** Marcus\n",
        encoding="utf-8",
    )
    (tmp_workspace / "SOUL.md").write_text(
        "# Soul\n\n<!-- lionclaw:blank-persona -->\n"
        "## User-Guided Communication Preferences\n"
        "- **working_style**: Curious and proactive  _(updated 2026-04-12)_\n",
        encoding="utf-8",
    )
    (tmp_workspace / "USER.md").write_text(
        "# User Profile\n\n## About the User\n"
        "- **Preferred name**: Carlos  _(updated 2026-04-12)_\n"
        "- **Projects**: Building AI systems  _(updated 2026-04-12)_\n",
        encoding="utf-8",
    )

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    result = await hooks.on_input[0](SESSION_START_TOKEN, base_config, context)

    assert "I just woke up" not in result
    assert "If you already know the user's preferred name" in result
    assert "Carlos" in result


@pytest.mark.asyncio
async def test_getting_to_know_nudge_stops_after_operational_turns(
    tmp_workspace: Path, base_config
) -> None:
    identity = tmp_workspace / "IDENTITY.md"
    identity.write_text("# Identity\n", encoding="utf-8")
    (tmp_workspace / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text("# User\n", encoding="utf-8")

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    for text in ["can you run pwd", "check this page https://example.com"]:
        await hooks.on_input[0](text, base_config, context)

    onboarding_state = context["olano"]["onboarding"]
    onboarding_state["turn_count"] = 6

    result = await hooks.on_input[0]("hello there", base_config, context)

    assert "GETTING-TO-KNOW-HUMAN MODE" not in result
    assert result == "hello there"


@pytest.mark.asyncio
async def test_explicit_user_fact_injects_profile_save_guidance(
    tmp_workspace: Path, base_config
) -> None:
    (tmp_workspace / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (tmp_workspace / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text("# User\n", encoding="utf-8")

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    result = await hooks.on_input[0]("you can call me Carlos", base_config, context)

    assert "user_profile_save" in result
    assert "Do not claim the information was saved" in result


@pytest.mark.asyncio
async def test_output_does_not_claim_memory_saved_without_tool_result(
    tmp_workspace: Path, base_config
) -> None:
    (tmp_workspace / "IDENTITY.md").write_text("# Identity\n", encoding="utf-8")
    (tmp_workspace / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text("# User\n", encoding="utf-8")

    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    context: dict = {}

    await hooks.on_input[0]("I love spicy food", base_config, context)
    result = await hooks.on_output[0](
        "Nice! I'll save that to your profile. Any favorite dishes?",
        base_config,
        context,
    )

    assert "save that to your profile" not in result.lower()


def test_is_first_run_self_heals_when_workspace_already_personalized(tmp_workspace: Path) -> None:
    identity = tmp_workspace / "IDENTITY.md"
    identity.write_text("<!-- lionclaw:first-run -->\n# Identity\n", encoding="utf-8")
    (tmp_workspace / "USER.md").write_text(
        "# User Profile\n\n"
        "*Not yet set. Will be filled in during first conversation.*\n\n"
        "## Setup Profile\n\n"
        "- Preferred name: Carlos\n",
        encoding="utf-8",
    )
    (tmp_workspace / "SOUL.md").write_text(
        "# Soul\n\n## User-Guided Communication Preferences\n- **tone**: Direct\n",
        encoding="utf-8",
    )

    assert is_first_run(tmp_workspace) is False
    assert "<!-- lionclaw:first-run -->" not in identity.read_text(encoding="utf-8")


def test_blank_default_agent_stays_first_run_even_if_user_profile_has_name(
    tmp_workspace: Path,
) -> None:
    identity = tmp_workspace / "IDENTITY.md"
    identity.write_text(
        "<!-- lionclaw:first-run -->\n# Identity\n\n<!-- lionclaw:blank-persona -->\n**Name:** *Not chosen yet*\n",
        encoding="utf-8",
    )
    (tmp_workspace / "USER.md").write_text(
        "# User Profile\n\n## Setup Profile\n\n- Preferred name: jrivera\n",
        encoding="utf-8",
    )
    (tmp_workspace / "SOUL.md").write_text(
        "# Soul\n\n<!-- lionclaw:blank-persona -->\n", encoding="utf-8"
    )

    assert is_first_run(tmp_workspace) is True
    assert "<!-- lionclaw:first-run -->" in identity.read_text(encoding="utf-8")
    assert "Setup Profile" not in (tmp_workspace / "USER.md").read_text(encoding="utf-8")


def test_blank_default_agent_is_not_first_run_once_user_and_style_are_saved(
    tmp_workspace: Path,
) -> None:
    identity = tmp_workspace / "IDENTITY.md"
    identity.write_text(
        "<!-- lionclaw:first-run -->\n# Identity\n\n<!-- lionclaw:blank-persona -->\n**Name:** Marcus\n",
        encoding="utf-8",
    )
    (tmp_workspace / "USER.md").write_text(
        "# User Profile\n\n## About the User\n"
        "- **Preferred name**: Carlos  _(updated 2026-04-12)_\n"
        "- **Projects**: Building AI systems  _(updated 2026-04-12)_\n",
        encoding="utf-8",
    )
    (tmp_workspace / "SOUL.md").write_text(
        "# Soul\n\n<!-- lionclaw:blank-persona -->\n"
        "## User-Guided Communication Preferences\n"
        "- **working_style**: Curious and proactive  _(updated 2026-04-12)_\n",
        encoding="utf-8",
    )

    assert is_first_run(tmp_workspace) is False
    assert "<!-- lionclaw:first-run -->" not in identity.read_text(encoding="utf-8")
