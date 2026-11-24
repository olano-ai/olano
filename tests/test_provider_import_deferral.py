"""The first agent build must not import provider SDKs the model does not use.

deepagents imports ``langchain_openai`` at module level (two middleware modules
keep its chat classes in a constant; through 0.7.11 it was ``langchain_google_genai``
too) and tries ``langchain_aws`` and ``langchain_fireworks`` on every
``create_deep_agent`` (to construct middleware that is a no-op for every other
provider), so a cold build on the fake model, on Anthropic, or on the olano
gateway paid ~60 MB of RSS for packages it never used — and olano's own
``web_search`` added tavily's ``hybrid_rag`` extra on top.
``builder._ensure_deepagents_provider_imports_deferred`` and the in-function
import are what keep that off the build path; these tests are
the regression guard. Numbers and method: ``agent_memory_profile.md`` in the
team's internal engineering notes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from olano.core import builder

# Everything the deferral is meant to keep out of a build that does not use it.
# ``openai`` / ``botocore`` are the SDKs behind the langchain packages; if the
# langchain package leaks back in, so do they, and they are the bulk of the RSS.
DEFERRED_PACKAGES = (
    "langchain_openai",
    "openai",
    "langchain_google_genai",
    "langchain_aws",
    "botocore",
    "langchain_fireworks",
    "fireworks",
    "tavily",
)


def test_cold_build_and_turn_load_none_of_the_deferred_packages() -> None:
    """A fresh interpreter builds the fake agent, runs a turn, and has loaded none of them.

    A subprocess is the only honest way to test this: the pytest process has
    long since imported half of these for other tests, and the deferral is a
    once-per-process, first-import-wins mechanism.
    """
    home = Path(os.environ["LIONCLAW_HOME"])
    agent_dir = home / "agents" / "deferral-probe"
    agent_dir.mkdir(parents=True)
    workspace = home / "deferral-workspace"
    workspace.mkdir()
    (agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "deferral-probe",
                "model": "fake:instant",
                "hidden": True,
                "backend": {"type": "filesystem", "root_dir": str(workspace)},
                "builtin_subagents": [],
            }
        )
    )

    script = textwrap.dedent(
        f"""
        import asyncio, json, sys
        from olano.config.loader import load_agent_config
        from olano.core.builder import build_agent_async
        from olano.core.runner import AgentRunner

        cfg = load_agent_config("deferral-probe")
        agent = asyncio.run(build_agent_async(cfg))

        async def turn():
            n = 0
            async for _ in AgentRunner(agent, cfg).astream_events("hi", thread_id="t"):
                n += 1
            return n

        events = asyncio.run(turn())
        loaded = {{name: name in sys.modules for name in {DEFERRED_PACKAGES!r}}}
        # No package may be left masked: a later genuine import has to work.
        import langchain_openai
        # ...and once it has happened, deepagents answers as if it had never been
        # masked: an OpenAI Responses model still takes a .docx block, and a gpt-6
        # one still gets skill tools disclosed inline.
        import deepagents.middleware.skills as skills_mw
        import deepagents.middleware.unsupported_content as uc
        responses = langchain_openai.ChatOpenAI(
            api_key="test", model="gpt-6-mini", use_responses_api=True
        )
        docx = {{
            "type": "file",
            "base64": "AA==",
            "mime_type": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        }}
        restored = {{
            "docx": uc._openai_responses_accepts(docx, responses),
            "inline": skills_mw._inline_block_builder(responses) is not None,
        }}
        print("RESULT " + json.dumps({{"events": events, "loaded": loaded, "restored": restored}}))
        # A built agent owns a non-daemon aiosqlite worker thread, so a normal
        # interpreter shutdown never completes — leave by the short door once
        # the result is out (scripts/bench_agent_memory.py does the same).
        sys.stdout.flush()
        import os
        os._exit(0)
        """
    )
    env = dict(os.environ)
    env["LIONCLAW_DISABLE_LOCAL_AGENTS_DIR"] = "1"
    env.setdefault("LIONCLAW_CHECKPOINT_BACKEND", "sqlite")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT "))
    result = json.loads(line[len("RESULT ") :])
    assert result["events"] > 0
    leaked = [name for name, present in result["loaded"].items() if present]
    assert leaked == [], f"provider/TUI packages imported on a fake-model build: {leaked}"
    assert result["restored"] == {"docx": True, "inline": True}


def test_every_patch_target_still_exists() -> None:
    """A renamed or removed target must fail here, not turn the patch into a no-op.

    Assigning to a module attribute that no longer exists succeeds silently, which
    is how the 0.7.11 non-PDF predicate patch kept "applying" to a function 0.7.23
    had moved into another module.
    """
    builder._ensure_deepagents_provider_imports_deferred()
    import deepagents.middleware._prompt_caching as pc
    import deepagents.middleware._skill_tools as st
    import deepagents.middleware.skills as skills_mw
    import deepagents.middleware.unsupported_content as uc

    assert hasattr(uc, "_OPENAI_FILE_MODEL_TYPES")
    assert getattr(uc._openai_responses_accepts, "_olano_fills_openai_types", False)
    assert hasattr(st, "_CHAT_OPENAI_TYPE")
    assert getattr(st._inline_block_builder, "_olano_fills_openai_types", False)
    # skills.py imported the function by name; that binding is the one called.
    assert skills_mw._inline_block_builder is st._inline_block_builder
    for factory in builder._GATED_PROMPT_CACHING_FACTORIES:
        assert hasattr(pc, f"{factory}_eager"), factory
    # The skills host-scan patch rebinds the class deepagents.graph constructs
    # the middleware from; the binding must still exist and still be that class.
    import deepagents.graph as graph

    assert issubclass(graph.SkillsMiddleware, skills_mw.SkillsMiddleware)


def test_emptied_openai_constants_are_filled_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The masked import leaves deepagents' OpenAI constants empty; the wrapped
    readers fill them from ``sys.modules`` and then answer as deepagents would,
    subclasses included where deepagents uses ``isinstance``, exact type where it
    uses ``type(...) is``."""
    langchain_openai = pytest.importorskip("langchain_openai")

    builder._ensure_deepagents_provider_imports_deferred()
    import deepagents.middleware._skill_tools as st
    import deepagents.middleware.skills as skills_mw
    import deepagents.middleware.unsupported_content as uc

    monkeypatch.setattr(uc, "_OPENAI_FILE_MODEL_TYPES", ())
    monkeypatch.setattr(st, "_CHAT_OPENAI_TYPE", None)

    class GatewayLike(langchain_openai.ChatOpenAI):
        """Stands in for the olano gateway model, which subclasses ChatOpenAI."""

    direct = langchain_openai.ChatOpenAI(api_key="test", model="gpt-6-mini", use_responses_api=True)
    sub = GatewayLike(api_key="test", model="gpt-6-mini", use_responses_api=True)
    docx = {
        "type": "file",
        "base64": "AA==",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    assert uc._openai_responses_accepts(docx, direct) is True
    assert uc._OPENAI_FILE_MODEL_TYPES == (
        langchain_openai.AzureChatOpenAI,
        langchain_openai.ChatOpenAI,
    )
    assert uc._openai_responses_accepts(docx, sub) is True
    assert skills_mw._inline_block_builder(direct) is not None
    assert st._CHAT_OPENAI_TYPE is langchain_openai.ChatOpenAI
    # deepagents' own exact-type rule still decides: a subclass is not disclosed inline.
    assert skills_mw._inline_block_builder(sub) is None


def test_openai_constants_stay_empty_while_the_package_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder._ensure_deepagents_provider_imports_deferred()
    import deepagents.middleware._skill_tools as st
    import deepagents.middleware.unsupported_content as uc

    monkeypatch.setattr(uc, "_OPENAI_FILE_MODEL_TYPES", ())
    monkeypatch.setattr(st, "_CHAT_OPENAI_TYPE", None)
    monkeypatch.setitem(sys.modules, "langchain_openai", None)
    builder._fill_deferred_openai_types()
    assert uc._OPENAI_FILE_MODEL_TYPES == ()
    assert st._CHAT_OPENAI_TYPE is None


def test_bedrock_prompt_caching_waits_for_its_package(monkeypatch: pytest.MonkeyPatch) -> None:
    builder._ensure_deepagents_provider_imports_deferred()
    import deepagents.middleware._prompt_caching as pc

    monkeypatch.delitem(sys.modules, "langchain_aws", raising=False)
    assert pc._create_bedrock_prompt_caching_middleware() is None

    sentinel = object()
    monkeypatch.setitem(sys.modules, "langchain_aws", types.ModuleType("langchain_aws"))
    monkeypatch.setattr(pc, "_create_bedrock_prompt_caching_middleware_eager", lambda: sentinel)
    assert pc._create_bedrock_prompt_caching_middleware() is sentinel


def test_fireworks_prompt_caching_waits_for_its_package(monkeypatch: pytest.MonkeyPatch) -> None:
    builder._ensure_deepagents_provider_imports_deferred()
    import deepagents.middleware._prompt_caching as pc

    monkeypatch.delitem(sys.modules, "langchain_fireworks", raising=False)
    assert pc._create_fireworks_prompt_caching_middleware() is None

    sentinel = object()
    monkeypatch.setitem(sys.modules, "langchain_fireworks", types.ModuleType("langchain_fireworks"))
    monkeypatch.setattr(pc, "_create_fireworks_prompt_caching_middleware_eager", lambda: sentinel)
    assert pc._create_fireworks_prompt_caching_middleware() is sentinel


def test_masking_never_outlives_the_deepagents_import() -> None:
    """A ``None`` left in sys.modules would turn every later import into ModuleNotFoundError."""
    builder._ensure_deepagents_provider_imports_deferred()
    for name in builder._DEFERRED_PROVIDER_MODULES:
        assert sys.modules.get(name, False) is not None, f"{name} is still masked"
