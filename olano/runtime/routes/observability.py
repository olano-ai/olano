"""``/api/observability/*``: Langfuse tracing status, credentials, connection test.

Admin-only (``auth._ADMIN_ONLY_PREFIXES``): these routes decide whether agent
conversations leave the box, and they handle a key that reads every trace in
a project. The settings themselves (on/off, host, capture, sample rate) are
saved through ``PUT /api/system/config`` like every other ``config.yaml``
block; the keys never are, they are reserved vault rows written only here.

- ``GET  /api/observability/langfuse/status``: effective state, where the keys
  come from (never the keys), export counters, the agents that override it.
- ``PUT  /api/observability/langfuse/credentials``: save a key pair (and an
  optional host), deployment-wide or for one agent (``?agent=``).
- ``DELETE /api/observability/langfuse/credentials``: forget them.
- ``POST /api/observability/langfuse/test``: authenticate against Langfuse with
  the given keys (or the stored ones), store the project id the dashboard
  builds links with, and say plainly what is wrong when it fails.
- ``GET  /api/observability/langfuse/links?agent=``: host and project id for
  that agent's trace links (the deployment's, or its own keys' project).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_TEST_TIMEOUT_S = 15.0
_KNOWN_REGIONS = (
    "https://cloud.langfuse.com",
    "https://us.cloud.langfuse.com",
    "https://jp.cloud.langfuse.com",
    "https://hipaa.cloud.langfuse.com",
)


class _Credentials(BaseModel):
    public_key: str
    secret_key: str
    host: str = ""


class _TestBody(BaseModel):
    public_key: str = ""
    secret_key: str = ""
    host: str = ""
    agent: str = ""


def _check_keys(public_key: str, secret_key: str) -> None:
    if not public_key.startswith("pk-lf-"):
        raise HTTPException(
            status_code=400,
            detail="The public key should start with pk-lf- (Langfuse → Settings → API keys).",
        )
    if not secret_key.startswith("sk-lf-"):
        raise HTTPException(
            status_code=400,
            detail="The secret key should start with sk-lf- (Langfuse → Settings → API keys).",
        )


def _check_agent(agent: str) -> str:
    agent = (agent or "").strip()
    if not agent:
        return ""
    from olano.config.loader import find_agent_config_path

    if find_agent_config_path(agent) is None:
        raise HTTPException(status_code=404, detail=f"No agent named {agent!r}.")
    return agent


def test_connection(host: str, public_key: str, secret_key: str) -> dict[str, Any]:
    """Authenticate against ``host`` and return the project, or a plain reason.

    ``GET /api/public/projects`` with the key pair is what the SDK's own
    ``auth_check`` does; it is called directly so the answer names the cause
    (wrong region, wrong key, unreachable) instead of a bare ``False``.
    """
    import httpx

    url = f"{host.rstrip('/')}/api/public/projects"
    try:
        resp = httpx.get(url, auth=(public_key, secret_key), timeout=_TEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "reason": "unreachable",
            "message": f"Could not reach {host} ({type(exc).__name__}). Check the address "
            "and that this server can reach it.",
        }
    if resp.status_code in (401, 403):
        others = [r for r in _KNOWN_REGIONS if r != host.rstrip("/")]
        hint = (
            " Langfuse Cloud keys only work in the region they were created in; if this "
            f"project lives in another region, use its address ({', '.join(others)})."
            if host.rstrip("/") in _KNOWN_REGIONS
            else ""
        )
        return {
            "ok": False,
            "reason": "unauthorized",
            "message": "Langfuse refused these keys." + hint,
        }
    if resp.status_code != 200:
        return {
            "ok": False,
            "reason": "unexpected",
            "message": f"Langfuse answered HTTP {resp.status_code}. Is {host} a Langfuse server?",
        }
    try:
        projects = resp.json().get("data") or []
    except ValueError:
        projects = []
    if not projects:
        return {"ok": False, "reason": "no_project", "message": "These keys belong to no project."}
    project = projects[0]
    return {
        "ok": True,
        "project_id": str(project.get("id") or ""),
        "project_name": str(project.get("name") or ""),
    }


def _status() -> dict[str, Any]:
    from olano.config.loader import list_agents, load_agent_config
    from olano.config.system_settings import load_observability_settings
    from olano.observability.credentials import credential_source, resolve_langfuse_credentials
    from olano.observability.policy import DEFAULT_LANGFUSE_HOST, effective_trace_policy
    from olano.observability.tracing import client_stats

    settings = load_observability_settings().langfuse
    creds = resolve_langfuse_credentials(None)
    from olano.config.system_settings import normalize_langfuse_host

    host = (
        settings.host
        or (normalize_langfuse_host(creds.host) if creds else "")
        or DEFAULT_LANGFUSE_HOST
    )
    agents: list[dict[str, Any]] = []
    try:
        for name in list_agents():
            try:
                cfg = load_agent_config(name)
            except Exception:
                continue
            override = cfg.observability.langfuse
            policy = effective_trace_policy(cfg)
            agents.append(
                {
                    "name": name,
                    "traced": policy is not None,
                    "capture": policy.capture if policy else None,
                    "sample_rate": policy.sample_rate if policy else None,
                    "credential_source": credential_source(name),
                    "override": override.model_dump(),
                }
            )
    except Exception:
        logger.debug("observability status: agent list failed", exc_info=True)
    return {
        "settings": settings.model_dump(),
        "effective_host": host,
        "credential_source": creds.source if creds else "none",
        "public_key": (creds.public_key[:10] + "…") if creds else "",
        "project_url": f"{host}/project/{settings.project_id}" if settings.project_id else "",
        "agents": agents,
        "clients": client_stats(),
    }


def _links(agent: str) -> dict[str, Any]:
    from olano.config.loader import find_agent_config_path, load_agent_config
    from olano.observability.credentials import agent_project_id
    from olano.observability.policy import effective_trace_policy

    if not agent or find_agent_config_path(agent) is None:
        return {"available": False}
    try:
        policy = effective_trace_policy(load_agent_config(agent))
    except Exception:
        policy = None
    if policy is None:
        return {"available": False, "traced": False}
    project_id = (
        agent_project_id(policy.agent)
        if policy.credential_source == "agent vault"
        else policy.project_id
    )
    effective = {
        "traced": True,
        "capture": policy.capture,
        "sample_rate": policy.sample_rate,
        "credential_source": policy.credential_source,
    }
    if not project_id:
        return {"available": False, "reason": "no_project", **effective}
    return {"available": True, "host": policy.host, "project_id": project_id, **effective}


def register_observability_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    @app.get("/api/observability/langfuse/status")
    async def langfuse_status() -> dict:
        return await asyncio.to_thread(_status)

    @app.put("/api/observability/langfuse/credentials")
    async def langfuse_save_credentials(body: _Credentials, agent: str = "") -> dict:
        from olano.config.system_settings import normalize_langfuse_host
        from olano.observability.credentials import store_credentials

        pk, sk = body.public_key.strip(), body.secret_key.strip()
        _check_keys(pk, sk)
        host = normalize_langfuse_host(body.host) if body.host.strip() else ""
        if body.host.strip() and not host:
            raise HTTPException(
                status_code=400,
                detail="The host must be an https:// address (http:// only for localhost).",
            )
        name = _check_agent(agent)
        try:
            await asyncio.to_thread(store_credentials, pk, sk, host, agent=name or None)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"saved": True, "agent": name or None}

    @app.delete("/api/observability/langfuse/credentials")
    async def langfuse_clear_credentials(agent: str = "") -> dict:
        from olano.observability.credentials import clear_credentials

        name = _check_agent(agent)
        await asyncio.to_thread(clear_credentials, agent=name or None)
        return {"cleared": True, "agent": name or None}

    @app.post("/api/observability/langfuse/test")
    async def langfuse_test(body: _TestBody) -> dict:
        from olano.config.system_settings import (
            load_observability_settings,
            normalize_langfuse_host,
            set_langfuse_settings,
        )
        from olano.observability.credentials import resolve_langfuse_credentials
        from olano.observability.policy import DEFAULT_LANGFUSE_HOST

        name = _check_agent(body.agent)
        pk, sk = body.public_key.strip(), body.secret_key.strip()
        stored = None
        if not (pk and sk):
            stored = await asyncio.to_thread(resolve_langfuse_credentials, name or None)
            if stored is None:
                raise HTTPException(
                    status_code=400,
                    detail="No Langfuse keys are saved yet. Enter a public and a secret key.",
                )
            pk, sk = stored.public_key, stored.secret_key
        else:
            _check_keys(pk, sk)
        settings = load_observability_settings().langfuse
        host = (
            normalize_langfuse_host(body.host)
            or settings.host
            or (normalize_langfuse_host(stored.host) if stored else "")
            or DEFAULT_LANGFUSE_HOST
        )
        result = await asyncio.to_thread(test_connection, host, pk, sk)
        result["host"] = host
        # Links are built from the project id: the deployment's lives in
        # config.yaml, an agent's own (its own keys) next to those keys.
        if result.get("ok") and result.get("project_id"):
            if name:
                from olano.observability.credentials import store_agent_project_id

                if stored is None or stored.source == "agent vault":
                    await asyncio.to_thread(store_agent_project_id, name, result["project_id"])
            else:
                await asyncio.to_thread(set_langfuse_settings, {"project_id": result["project_id"]})
        return result

    @app.get("/api/observability/langfuse/links")
    async def langfuse_links(agent: str = "") -> dict:
        """Where ``agent``'s traces can be opened (``available``, ``host``,
        ``project_id``) and whether it is traced at all (``traced``, ``capture``,
        ``sample_rate``, ``credential_source``).

        Cheap (no agent scan, no network), so the chat can ask once per agent.
        ``available`` is false when the agent is not traced or the project is
        unknown (Test connection has not been run for its keys).
        """
        return await asyncio.to_thread(_links, agent.strip())
