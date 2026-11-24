#!/usr/bin/env python
"""A throwaway runtime against the local cloud sandbox, in every posture.

The cloud repo's ``scripts/sandbox/up.sh`` brings up a real LiteLLM 1.102.0
with mock tiers, the real cloud routes under the e2e auth bypass and the
product database. This script boots THIS runtime over a throwaway home in one
posture at a time and proves the account-key surface end to end:

* **open** (a self-hosted box): mint an account key through the real cloud
  route, connect it through ``PUT /api/olano/credits-key``, read every surface
  that must agree (``/api/credits``, ``/api/olano/credits``, the Providers
  catalog, the gateway diagnostics), run a real turn on ``olano:turbo`` through
  the gateway, then walk the failure ladder: an empty wallet is
  ``olano_credits_exhausted``, a key revoked on the cloud is
  ``olano_key_invalid`` (and the card reads rejected), a removed key is
  ``olano_key_missing``; an operator's ``OLANO_GATEWAY_KEY`` shadows a pasted
  key (``active: false``).
* **enforced** (a managed box, signed license + manifest + deployment token,
  the test verify key installed in-process): a stray ``OLANO_CREDITS_KEY`` in
  the environment changes nothing: the route answers 409, the catalog payload
  carries no account fields, the box enrolls its own key through the sandbox
  cloud and a turn runs on it.
* **quarantine** (a managed marker with no manifest): 409, no account fields,
  no credits.

Run (needs psql, the cloud sandbox up, and this repo's venv)::

    uv run python tests/sandbox/run_box.py --posture open
    uv run python tests/sandbox/run_box.py --posture all

Each check prints one row; exit status is non-zero if any failed. Demo keys
are printed once and never stored. Not collected by pytest (``tests/sandbox``
is not a test module path).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
CLOUD_REPO = Path(os.environ.get("CLOUD_REPO", REPO.parent / "cloud"))
CLOUD_URL = os.environ.get("CLOUD_URL", "http://127.0.0.1:3000")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:4000")
GATEWAY_MASTER_KEY = os.environ.get("GATEWAY_MASTER_KEY", "sk-sandbox-master-key-0000000000")
SBX_ORG = os.environ.get("SBX_ORG", "org_sbx")
PG_SOCK = os.environ.get("PG_SOCK", "/tmp/pgtest-olano")
PG_PORT = os.environ.get("PG_PORT", "54329")
PRODUCT_DB = os.environ.get("PRODUCT_DB", "olano_test")
AGENT = "sbx"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((name, passed, detail))
    print(f"  {'✓' if passed else '✗'} {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return passed


# ── the cloud sandbox side (SQL + the real routes) ───────────────────────────


def sql(text: str) -> str:
    out = subprocess.run(
        [
            "psql",
            "-X",
            "-q",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            PG_SOCK,
            "-p",
            PG_PORT,
            "-U",
            "postgres",
            "-d",
            PRODUCT_DB,
            "-c",
            text,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def q(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def cloud(
    method: str,
    path: str,
    *,
    bearer: str | None = None,
    body: Any = None,
    headers: dict | None = None,
) -> httpx.Response:
    h = dict(headers or {})
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return httpx.request(method, f"{CLOUD_URL}{path}", headers=h, json=body, timeout=60)


def gateway_admin(method: str, path: str, body: Any = None) -> httpx.Response:
    return httpx.request(
        method,
        f"{GATEWAY_URL}{path}",
        headers={"Authorization": f"Bearer {GATEWAY_MASTER_KEY}"},
        json=body,
        timeout=30,
    )


def reset_org_wallet() -> None:
    for key_id in filter(
        None,
        sql(
            f"select litellm_key_id from account_api_keys where org_id = {q(SBX_ORG)} and revoked_at is null"
        ).split("\n"),
    ):
        gateway_admin("POST", "/key/delete", {"keys": [key_id]})
    sql(
        f"insert into organizations (id, name) values ({q(SBX_ORG)}, {q(SBX_ORG)}) on conflict (id) do nothing"
    )
    sql(f"delete from account_gateway_calls where org_id = {q(SBX_ORG)}")
    sql(f"delete from account_api_keys where org_id = {q(SBX_ORG)}")
    sql(f"delete from credit_ledger where org_id = {q(SBX_ORG)} and wallet = 'account'")
    sql(
        f"update account_credit_balances set purchased_remaining = 0, lifetime_used = 0 where org_id = {q(SBX_ORG)}"
    )


def mint_account_key(name: str) -> tuple[str, str]:
    res = cloud("POST", "/api/v1/account/keys", body={"name": name})
    if res.status_code != 201:
        raise RuntimeError(f"mint failed: {res.status_code} {res.text[:200]}")
    data = res.json()
    return data["key"], data["id"]


def credit_account(credits: float) -> None:
    sql(
        f"select olano_account_credit_apply('purchase', {q(SBX_ORG)}, {credits}, {q(f'purchase:runbox:{time.time()}')})"
    )
    worker_sync()


def worker_sync() -> dict:
    out = subprocess.run(
        ["bash", str(CLOUD_REPO / "scripts/sandbox/worker-sync.sh")],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def make_deployment() -> tuple[str, str]:
    """A running deployment with a token and 1000 credits; returns (id, token)."""
    dep_id = str(uuid.uuid4())
    token = "olano_dep_" + secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    sql(
        f"insert into organizations (id, name) values ({q(SBX_ORG)}, {q(SBX_ORG)}) on conflict (id) do nothing"
    )
    sql(
        f"insert into deployments (id, org_id, owner_user_id, name, slug, provider, status, plan) values "
        f"({q(dep_id)}::uuid, {q(SBX_ORG)}, 'user_sbx', 'runbox', {q('runbox-' + dep_id[:8])}, 'digitalocean', 'running', 'pro')"
    )
    sql(
        f"insert into deployment_tokens (deployment_id, token_hash, prefix, status) values "
        f"({q(dep_id)}::uuid, decode({q(digest)}, 'hex'), {q(token[:14])}, 'active')"
    )
    sql(
        f"select olano_credit_apply('purchase', {q(dep_id)}::uuid, {q(SBX_ORG)}, 1000, {q(f'purchase:runbox:{dep_id}')})"
    )
    return dep_id, token


# ── the runtime side ────────────────────────────────────────────────────────


def seed_home(home: Path) -> None:
    agent_dir = home / "agents" / AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "description": "sandbox agent on the Olano tiers",
                "model": "olano:turbo",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "builtin_subagents": [],
                "tools": [],
                "default_internal_tools": False,
            }
        )
    )
    (home / "onboarding.json").write_text(
        json.dumps({"completed": True, "skipped": True, "completed_by": "run_box"})
    )


def start_runtime(port: int) -> Any:
    """Boot RuntimeServer in a daemon thread; returns the server (for stop)."""
    from olano.runtime.server import RuntimeServer

    box: dict[str, Any] = {}

    # Signal handlers can only be installed on the main thread; the sandbox
    # drives shutdown by exiting the process, so the runtime gets none.
    RuntimeServer._install_signal_handlers = lambda self: None  # type: ignore[method-assign]

    def run() -> None:
        server = RuntimeServer(agent_names=[AGENT])
        box["server"] = server
        asyncio.run(server.start())

    threading.Thread(target=run, daemon=True).start()
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=3)
            if r.status_code == 200 and not r.json().get("booting", False):
                return box.get("server")
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError("runtime did not come up")


class Box:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path: str) -> httpx.Response:
        return httpx.get(self.base + path, timeout=60)

    def put(self, path: str, body: Any) -> httpx.Response:
        return httpx.put(self.base + path, json=body, timeout=120)

    def delete(self, path: str) -> httpx.Response:
        return httpx.delete(self.base + path, timeout=120)

    def turn(self, message: str = "ping") -> dict:
        """One streamed chat turn; returns {text, error_code, error_message, usage}."""
        out: dict[str, Any] = {
            "text": "",
            "error_code": None,
            "error_message": None,
            "usage": None,
            "status": None,
        }
        with httpx.stream(
            "POST",
            self.base + "/api/chat/stream",
            json={"agent": AGENT, "message": message},
            timeout=180,
        ) as r:
            out["status"] = r.status_code
            if r.status_code != 200:
                out["error_code"] = f"http_{r.status_code}"
                return out
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    frame = json.loads(line[6:])
                except ValueError:
                    continue
                kind = frame.get("type")
                if kind == "text":
                    out["text"] += frame.get("text", "")
                elif kind == "error":
                    out["error_code"] = frame.get("code")
                    out["error_message"] = frame.get("message")
                elif kind == "usage":
                    out["usage"] = frame
        return out

    def wait_for_agent(self, name: str, timeout: float = 180) -> bool:
        """A managed box boots its tier agent only once its key is enrolled, after
        /api/status stops reading `booting`; the roster lists the agent from disk
        before the runner exists, so readiness is "a chat no longer 404s"."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = httpx.post(
                self.base + "/api/chat/stream",
                json={"agent": name, "message": "ready?"},
                timeout=120,
            )
            if r.status_code != 404:
                return True
            time.sleep(3)
        return False

    def olano_auth(self) -> dict | None:
        r = self.get("/api/catalog/providers?fields=basic")
        if r.status_code != 200:
            return None
        for p in r.json().get("providers", r.json() if isinstance(r.json(), list) else []):
            if p.get("key") == "olano":
                return p.get("auth") or {}
        return None


# ── postures ────────────────────────────────────────────────────────────────


def run_open(box: Box, shadow_env_key: str | None) -> None:
    print("\n▶ open posture (self-hosted box)")
    reset_org_wallet()
    key, key_id = mint_account_key("run_box")
    print(f"  demo account key (never stored): {key}")

    st = box.get("/api/olano/credits-key").json()
    check(
        "the route is manageable and holds no key",
        st.get("manageable") is True and st.get("connected") is False,
        json.dumps(st)[:120],
    )
    check(
        "an open box without a key has credits off",
        box.get("/api/credits").json().get("credits_enabled") is False,
    )
    auth = box.olano_auth()
    check(
        "the catalog shows the Olano card as manageable with no source yet",
        auth is not None and auth.get("manageable") is True and auth.get("source") in (None, "env"),
        json.dumps(auth)[:140],
    )

    res = box.put("/api/olano/credits-key", {"key": key})
    body = res.json()
    check(
        "PUT connects the key through the real verify route",
        res.status_code == 200 and body.get("connected") is True,
        f"{res.status_code} {json.dumps(body)[:160]}",
    )
    if shadow_env_key:
        check(
            "an operator's OLANO_GATEWAY_KEY shadows the pasted key",
            body.get("active") is False
            and body.get("shadowed_by") == "env"
            and body.get("source") == "env",
            json.dumps({k: body.get(k) for k in ("active", "shadowed_by", "source")}),
        )
        check(
            "the shadowed box still reports credits from the account wallet",
            box.get("/api/credits").json().get("credits_enabled") is True,
        )
        return
    check(
        "the pasted key is the active source",
        body.get("active") is True
        and body.get("shadowed_by") is None
        and body.get("source") == "account",
    )
    check(
        "the first connect rebuilt the tier agent",
        AGENT in (body.get("agents_restarted") or []),
        str(body.get("agents_restarted")),
    )
    check(
        "the verify payload rode back (schemaVersion 1 contract)",
        body.get("verified", {}).get("key", {}).get("id") == key_id,
    )
    check("/api/credits turns on", box.get("/api/credits").json().get("credits_enabled") is True)
    bal = box.get("/api/olano/credits").json()
    check(
        "/api/olano/credits reads the account wallet",
        bal.get("available") is True,
        json.dumps(bal)[:120],
    )
    auth = box.olano_auth()
    check(
        "the catalog's Olano card reads source=account",
        auth is not None and auth.get("source") == "account" and auth.get("manageable") is True,
        json.dumps(auth)[:140],
    )
    diag = box.get("/api/diagnostics/gateway").json()
    check(
        "the gateway diagnostics name the account key",
        diag.get("source") == "account" or diag.get("enrollment", {}).get("source") == "account",
        json.dumps(diag)[:160],
    )

    # Empty wallet: the team is blocked, so the turn is an out-of-credits error.
    t = box.turn()
    check(
        "a turn on an empty wallet is olano_credits_exhausted",
        t["error_code"] == "olano_credits_exhausted",
        f"{t['error_code']}: {str(t['error_message'])[:100]}",
    )

    credit_account(500)
    time.sleep(1)
    t = box.turn()
    check(
        "a funded wallet answers through the gateway (mock tier)",
        t["text"].strip() == "pong-turbo" and t["error_code"] is None,
        f"text={t['text']!r} err={t['error_code']}",
    )
    check(
        "the usage frame charges credits",
        bool(t["usage"]) and (t["usage"].get("credits") or 0) > 0,
        json.dumps(t["usage"] or {})[:120],
    )
    st = box.get("/api/olano/credits-key").json()
    check("a working key is not marked rejected", st.get("rejected") is False)

    # Revoked on the cloud: the next turn is a refusal nothing on the box can heal.
    rev = cloud("DELETE", f"/api/v1/account/keys/{key_id}")
    check("the cloud revokes the key", rev.status_code == 200)
    t = box.turn()
    check(
        "a turn on a revoked key is olano_key_invalid (not a retry)",
        t["error_code"] == "olano_key_invalid",
        f"{t['error_code']}: {str(t['error_message'])[:100]}",
    )
    for _ in range(10):  # the mark is recorded on the error path; give the frame a beat
        st = box.get("/api/olano/credits-key").json()
        if st.get("rejected") is True:
            break
        time.sleep(0.5)
    check("the card reads rejected", st.get("rejected") is True, json.dumps(st)[:120])

    # Removed: the live models hold the placeholder; the agent was rebuilt.
    res = box.delete("/api/olano/credits-key")
    body = res.json()
    check(
        "DELETE removes the key and leaves the tier agent running (it could not boot without one)",
        res.status_code == 200
        and body.get("removed") is True
        and body.get("agents_restarted") == [],
        json.dumps(body)[:160],
    )
    check("credits turn off again", box.get("/api/credits").json().get("credits_enabled") is False)
    t = box.turn()
    check(
        "a turn with no key is olano_key_missing",
        t["error_code"] == "olano_key_missing",
        f"{t['error_code']}: {str(t['error_message'])[:100]}",
    )

    # A second connect while a key is in use swaps in place (no rebuild).
    key2, _ = mint_account_key("run_box 2")
    first = box.put("/api/olano/credits-key", {"key": key2}).json()
    check(
        "re-connecting after removal rebuilds the tier agent again",
        AGENT in (first.get("agents_restarted") or []),
        str(first.get("agents_restarted")),
    )
    key3, _ = mint_account_key("run_box 3")
    again = box.put("/api/olano/credits-key", {"key": key3}).json()
    check(
        "replacing a live key swaps the models in place (no rebuild)",
        again.get("agents_restarted") == [] and again.get("connected") is True,
    )
    t = box.turn()
    check(
        "the replacement key answers",
        t["text"].strip() == "pong-turbo",
        f"text={t['text']!r} err={t['error_code']}",
    )


def run_managed(box: Box, posture: str) -> None:
    print(f"\n▶ {posture} posture (managed box with a stray OLANO_CREDITS_KEY)")
    st = box.get("/api/olano/credits-key").json()
    check(
        "the route is not manageable",
        st.get("manageable") is False and st.get("connected") is False,
        json.dumps(st)[:120],
    )
    res = box.put("/api/olano/credits-key", {"key": os.environ["OLANO_CREDITS_KEY"]})
    check(
        "PUT answers 409 managed_box",
        res.status_code == 409 and res.json().get("detail", {}).get("code") == "managed_box",
        f"{res.status_code}",
    )
    res = box.delete("/api/olano/credits-key")
    check("DELETE answers 409 managed_box", res.status_code == 409)
    auth = box.olano_auth()
    check(
        "the catalog's Olano card carries no account fields",
        auth is not None and "source" not in auth and "manageable" not in auth,
        json.dumps(auth)[:160],
    )
    credits = box.get("/api/credits").json()
    if posture == "enforced":
        check(
            "credits come from the deployment path",
            credits.get("credits_enabled") is True,
            json.dumps(credits)[:120],
        )
        diag = box.get("/api/diagnostics/gateway").json()
        src = diag.get("source") or diag.get("enrollment", {}).get("source")
        check(
            "the gateway key is the enrolled deployment key",
            src == "deployment",
            json.dumps(diag)[:160],
        )
        check("the tier agent booted on the enrolled key", box.wait_for_agent(AGENT))
        t = box.turn()
        check(
            "a turn runs on the deployment key",
            t["text"].strip() == "pong-turbo" and t["error_code"] is None,
            f"text={t['text']!r} err={t['error_code']}",
        )
        # The spend row belongs to the deployment key, never to the stray account key.
        stray = hashlib.sha256(os.environ["OLANO_CREDITS_KEY"].encode()).hexdigest()
        rows = gateway_admin("GET", f"/spend/logs?api_key={stray}").json()
        check(
            "the stray account key never reached the gateway",
            isinstance(rows, list) and len(rows) == 0,
            f"{len(rows) if isinstance(rows, list) else rows} rows",
        )
    else:
        check(
            "a quarantined box has no credits",
            credits.get("credits_enabled") is False,
            json.dumps(credits)[:120],
        )
        from olano.deployment import gateway_key as gk

        check(
            "the stray key is invisible to the key resolver",
            gk.gateway_key() is None
            and gk.gateway_key_source() is None
            and gk.account_key_if_unmanaged() is None,
        )


def install_enforced(home: Path, manifest_path: Path, dep_id: str) -> None:
    """A signed license the resolver trusts (test verify key patched in-process)."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from olano.deployment import envelope, resolver

    private = ed25519.Ed25519PrivateKey.generate()
    key_id = "sandbox-run-box"
    envelope.load_verify_keys = lambda: {key_id: private.public_key()}  # type: ignore[assignment]
    now = int(time.time())
    payload = {
        "schema": "olano.deployment-settings",
        "schema_version": 1,
        "issuer": "cloud.olano.ai",
        "audience": "olano-deployment-settings",
        "channel": "local",
        "license_id": "lic_sandbox",
        "deployment_id": dep_id,
        "plan": "pro",
        "policy_revision": 1,
        "issued_at": now - 60,
        "not_before": now - 60,
        "refresh_after": now + 3600,
        "expires_at": now + 86400,
        "settings": {},
    }
    # mode "local": the license is read from the home, as the resolver tests
    # install it; "cloud" would fetch the settings channel, which the sandbox
    # cloud signs with keys this process does not trust.
    manifest_path.write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": dep_id})
    )
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.deployment_license_path.write_text(
        envelope.sign_license(payload, private, key_id=key_id) + "\n"
    )
    resolver.invalidate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--posture",
        choices=["open", "open-shadowed", "enforced", "quarantine", "all"],
        default="all",
    )
    ap.add_argument("--port", type=int, default=int(os.environ.get("RUNBOX_PORT", "18765")))
    args = ap.parse_args()
    if args.posture == "all":
        failed = 0
        for p in ("open", "open-shadowed", "enforced", "quarantine"):
            r = subprocess.run(
                [sys.executable, __file__, "--posture", p, "--port", str(args.port)], env=os.environ
            )
            failed += r.returncode != 0
        return 1 if failed else 0

    home = Path(tempfile.mkdtemp(prefix=f"olano-runbox-{args.posture}-"))
    manifest = home / "installation.json"
    os.environ.update(
        {
            "OLANO_HOME": str(home / "home"),
            "LIONCLAW_HOME": str(home / "home"),
            "LIONCLAW_INSTALLATION_FILE": str(manifest),
            "LIONCLAW_CHECKPOINT_BACKEND": "sqlite",
            "LIONCLAW_STORE_BACKEND": "sqlite",
            "OLANO_VAULT_KEY": secrets.token_urlsafe(32),
            "OLANO_GATEWAY_URL": GATEWAY_URL,
            "LIONCLAW_CLOUD_API_URL": CLOUD_URL,
            "LIONCLAW_RUNTIME_PORT": str(args.port),
            "LIONCLAW_NO_EPHEMERAL_REAP": "1",
            "LIONCLAW_SYNC_CARTOGRAPHY": "0",
            "LIONCLAW_DISABLE_LOCAL_AGENTS_DIR": "1",
        }
    )
    for name in (
        "LIONCLAW_DEPLOYMENT_MANAGED",
        "LIONCLAW_DEPLOYMENT_TOKEN",
        "OLANO_GATEWAY_KEY",
        "LIONCLAW_GATEWAY_KEY",
        "OLANO_CREDITS_KEY",
        "LIONCLAW_CREDITS_KEY",
        "LIONCLAW_LICENSE",
        "LIONCLAW_LICENSE_PATH",
    ):
        os.environ.pop(name, None)
    seed_home(home / "home")

    shadow: str | None = None
    if args.posture == "open-shadowed":
        _, token = make_deployment()
        enrolled = cloud(
            "POST",
            "/api/v1/deployments/gateway-key",
            bearer=token,
            headers={"Idempotency-Key": f"runbox-{time.time()}"},
            body={},
        )
        shadow = enrolled.json()["key"]
        os.environ["OLANO_GATEWAY_KEY"] = shadow
    if args.posture in ("enforced", "quarantine"):
        reset_org_wallet()
        stray, _ = mint_account_key("stray")
        os.environ["OLANO_CREDITS_KEY"] = stray
        os.environ["LIONCLAW_DEPLOYMENT_MANAGED"] = "1"
        if args.posture == "enforced":
            dep_id, token = make_deployment()
            os.environ["LIONCLAW_DEPLOYMENT_TOKEN"] = token
            install_enforced(home / "home", manifest, dep_id)
    # Managed postures default the login gate on; this is about the key, not auth.
    if args.posture in ("enforced", "quarantine"):
        import olano.runtime.auth as auth

        auth.multi_user_enabled = lambda: False  # type: ignore[assignment]

    start_runtime(args.port)
    box = Box(args.port)
    try:
        if args.posture in ("open", "open-shadowed"):
            run_open(box, shadow)
        else:
            run_managed(box, args.posture)
    finally:
        failed = [r for r in RESULTS if not r[1]]
        print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed ({args.posture})")
        for name, _, detail in failed:
            print(f"  ✗ {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
