# Test agents

Project-local agents used for testing/verifying olano features. They are
auto-discovered when you run olano from this repo (project-local `agents/`
takes precedence over `~/.olano/agents/`), so they are available to the CLI
and test harness as soon as the runtime starts.

**These fixtures are dev-only and never reach a customer box**, three ways:

1. They are excluded from every shipped artifact — `.dockerignore` keeps
   `agents/` out of the backend image (dev containers get it back via the
   `docker-compose.yml` bind mount `./agents:/app/agents:ro`), and
   Olano Cloud's `ship.sh` excludes it from host deploys.
2. On a managed deployment the loader refuses the project-local `agents/`
   search dir entirely (`olano/config/loader.py:_local_agents_dir_enabled` —
   env markers / installation manifest evidence, escape hatch
   `LIONCLAW_DISABLE_LOCAL_AGENTS_DIR=0`), so even a stale image cannot
   surface them.
3. `list_agents()` excludes `hidden: true` agents by default; callers that
   need the full on-disk set opt in with `include_hidden=True`, and the opt-in
   set is frozen by `tests/test_hidden_agents_policy.py`.

Each one sets **`hidden: true`** in its `agent.json5`. A hidden agent is an
internal-only tool: it is excluded from the dashboard agent roster
(`GET /api/agents`), the org chart, admin access views, `/api/status`, and
every other agent's prompt roster. It is
**not booted by the long-running runtime** (`olano serve` / the dashboard
skip it), so a hidden test agent is completely inactive in production and
consumes no resources. You still use it exactly as before for testing:
one-shot `olano --agent <name> -n "<task>"` runs (the standalone CLI builds a
single agent directly, bypassing the runtime boot loop), the test harness, and `olano
serve --agent <name>` (naming it explicitly boots it). To boot hidden agents in
a running dev runtime, set `LIONCLAW_BOOT_HIDDEN_AGENTS=1`. `olano agents
list` still shows them, tagged `(hidden)`.

| Agent | Model | Use for |
|-------|-------|---------|
| `test-agent-claude-haiku` | `anthropic:claude-haiku-4-5` | **Default** for general feature tests (toolkits, connectors, platforms, chat) — fast + cheap. |
| `test-agent-claude-sonnet` | `anthropic:claude-sonnet-4-6` | Feature tests needing stronger reasoning/tool-use. |
| `test-agent-openai` | `openai:gpt-5.4-mini` | Targeted OpenAI-specific tests. |
| `test-agent-deepseek` | `deepseek:deepseek-flash` | Targeted DeepSeek-specific tests. |
| `test-agent-gemini` | `google_genai:gemini-2.5-flash` | Targeted Gemini-specific tests. |
| `test-agent-fake` | `fake:instant` | Offline/keyless: agent-loop profiling (`scripts/bench_agent_turn.py`) and deterministic end-to-end tests. |

## Keys

These configs contain **no secrets** — each model reads its provider key from the
environment at build time:

| Agent | Required env var |
|-------|------------------|
| claude-* | `ANTHROPIC_API_KEY` |
| openai | `OPENAI_API_KEY` |
| deepseek | `DEEPSEEK_API_KEY` |
| gemini | `GOOGLE_API_KEY` (+ `uv sync --extra google`) |
| fake | none — deterministic offline model (`olano/models/fake.py`) |

Put keys in a git-ignored `.env` at the repo root (auto-loaded) or the vault.
An agent whose key is missing simply errors at chat time; the others still work.

## Convention

General feature tests should default to **`test-agent-claude-haiku`** so they run
on one cheap, capable model. Reserve the openai/deepseek/gemini agents for tests
that specifically assert *that provider's* behaviour. These base agents are lean
(`builtin_subagents: []`); a toolkit/connector test should enable the specific
subagent it needs (e.g. `builtin_subagents: ["StripeAgent"]`).
