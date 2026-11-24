# Contributing to Olano

Thanks for taking the time to help. This page covers how to set up a
development checkout, what a change needs before it can merge, and where to
ask questions.

By contributing you agree that your contribution is released under the
project's [MIT License](LICENSE), and that you will follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Bugs and feature ideas** go in [GitHub issues](https://github.com/olano-ai/olano/issues).
  Search first; a short reproduction (the `agent.json5` involved, the steps, what
  you expected and what happened) gets a bug fixed much faster.
- **Security problems** never go in a public issue. Follow
  [SECURITY.md](SECURITY.md).
- **Larger changes** (a new subsystem, a schema change to `agent.json5` or
  `config.yaml`, a new dependency) are worth an issue before the code, so we can
  agree on the shape first.

## Development setup

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
Node.js is needed for the dashboard and the WhatsApp bridge, Docker for the
shell sandbox and Postgres.

```bash
git clone https://github.com/olano-ai/olano.git
cd olano
make dev          # uv sync --group dev, then installs the pre-commit hooks
```

Talk to an agent on the deterministic offline model, with no provider key and
no Postgres (the checkout's `agents/` folder holds the test agents):

```bash
OLANO_CHECKPOINT_BACKEND=sqlite uv run olano --agent test-agent-fake --message "Hello" --stream
```

Without `OLANO_CHECKPOINT_BACKEND=sqlite` the runtime expects Postgres
(`make postgres-up` starts one and prints the connection URL to set).

Dashboard work runs Vite against a separately started runtime:

```bash
uv run olano serve                                        # API on :8765
cd olano/dashboard_2.0 && npm install && npm run dev      # UI on :3201, proxies /api
```

## Checks a change must pass

CI runs these on every pull request; run them locally first.

```bash
make check        # ruff lint, ruff format check, mypy
make test-fast    # the full pytest suite in parallel (CI runs it the same way)
```

Depending on what you touched:

| You changed | Also run |
|---|---|
| `olano/backends/`, the egress firewall, `docker/olano-sandbox/`, `tests/integration/` | `make test-sandbox` (needs Docker; see `CLAUDE.md` for running it inside a container) |
| `olano/dashboard_2.0/` | `npm run build && npm run typecheck && npm run test:unit` in the dashboard, and `make test-e2e` |
| `docs/user-guide/` | `uv run python scripts/gen_user_guide.py` and `uv sync --group docs && uv run mkdocs build --strict` |
| A tool or toolkit name | `uv run python scripts/gen_tool_name_index.py` |
| Dependencies | `uv lock`, and `make audit` |

Every pull request also runs a secret scan ([gitleaks](https://github.com/gitleaks/gitleaks),
config in `.gitleaks.toml`) over each commit it adds and over the whole tree.
If it flags a real key, revoke the key with its provider first: deleting it from
the branch does not take it back. If it flags a test value or a lookalike, add a
`gitleaks:allow` comment on that line.

Tests use `asyncio_mode = "auto"`, so async tests need no marker. Never skip,
disable or loosen a test to get a change green; fix the cause or explain in the
pull request why the test itself was wrong.

## Conventions

`CLAUDE.md` is the detailed engineering reference for this repository (it is
written for coding agents and humans alike). The rules that most often come up
in review:

- **User-visible changes update the user guide in the same pull request.** A
  new or changed `agent.json5` / `config.yaml` key, dashboard tab or field,
  default, connector or slash command gets its page under `docs/user-guide/`.
- **Every dashboard field ships with a full explanation**: what it does, the
  accepted format with an example, the default when empty, and what each
  dropdown option means.
- **Dashboard changes are checked at both desktop and phone widths.** The e2e
  suite already runs every page test at both.
- **No secrets in the tree.** Credentials resolve through the vault and the
  agent's `config.env`; `.env.example` holds placeholders only.
- **Keep the default install light.** Don't add `torch` (or anything that pulls
  it) to the base dependencies.
- **Only redistributable content under `olano/`.** Everything there ships in
  the public wheel and images, so bundled skills, fonts and assets must carry
  a license that allows that, listed in `THIRD_PARTY_NOTICES.md`.

## Pull requests

- Keep a pull request to one change. A refactor and a behaviour change are two
  pull requests.
- The description says what changed, why, and how you verified it (the
  commands you ran). Link the issue it closes.
- Commit messages explain the reason for the change, not only what the diff
  already shows.
- A maintainer reviews every pull request. Expect questions; they are about the
  code, never about you.

## Questions

Open an issue with the `question` label, or start from the user guide at
[docs.olano.ai](https://docs.olano.ai).
