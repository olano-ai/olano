"""GitHub connector — repos, issues, PRs, git over HTTPS + the GitHub MCP server.

Two sign-in methods, one record:

- **OAuth** — create an OAuth app at github.com/settings/developers (or an
  org-owned one) and register ``<dashboard origin>/api/auth/github/callback``;
  or use Olano's official app when the catalog publishes one. GitHub Apps
  work too and rotate their user tokens (8h ``expires_in`` + refresh grant);
  classic OAuth-app tokens never expire, so a record with no ``expires_in``
  is treated as non-expiring.
- **Device code** — the GitHub CLI's own *public* client id (the one every
  ``gh`` binary ships; the consent screen says "GitHub CLI"), so a phone or a
  self-hosted box signs in with a short code and no app registration at all.
  Overridable with ``LIONCLAW_GH_OAUTH_CLIENT_ID`` / ``LIONCLAW_GH_OAUTH_SCOPES``
  for enterprises fronting their own app.

Either way the token is exposed as ``GITHUB_TOKEN`` (read by
``tools/github_tools.py`` and the ``git`` toolkit's job container) and as
``GITHUB_PERSONAL_ACCESS_TOKEN`` (name parity with github/github-mcp-server)
for anything that resolves credentials via ``get_credential`` — including a
sandboxed stdio MCP server's ``${vault:GITHUB_TOKEN}`` env ref. The broker
resolves above the vault, so while connected this token shadows a
hand-pasted vault ``GITHUB_TOKEN`` for agents resolving through this scope.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider, DeviceAuth
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="github",
        name="GitHub",
        device=DeviceAuth(
            # gh's own public client id; `gh auth login` sends exactly this.
            client_id="178c6fc778ccc68e1d6a",
            # gh's default login scopes.
            scopes=("repo", "read:org", "gist"),
            device_code_url="https://github.com/login/device/code",
            token_url="https://github.com/login/oauth/access_token",
            verification_url="https://github.com/login/device",
            client_id_env="LIONCLAW_GH_OAUTH_CLIENT_ID",
            scopes_env="LIONCLAW_GH_OAUTH_SCOPES",
            account_url="https://api.github.com/user",
            account_field="login",
        ),
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        # repo covers code/issues/PRs; workflow lets pushes touch .github/
        # workflows; read:org resolves org membership; read:user for identity.
        scopes=("repo", "workflow", "read:org", "read:user"),
        creds_vault_key="GITHUB_OAUTH_CLIENT",
        client_env_pair=("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"),
        exposes={
            "GITHUB_TOKEN": "access_token",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "access_token",
        },
        client_auth="post",
        # GitHub Apps refresh via the standard grant; OAuth-app records simply
        # have no refresh_token and fall through to "non-expiring".
        supports_refresh=True,
    )
)
