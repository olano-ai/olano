"""The runtime provisions its own admin account before the API binds.

runtime/first_boot.py runs quick-setup at ``serve`` startup when the
environment carries first-run credentials and the home has no users. These pin
the three rules: only an explicit password provisions, only a never-provisioned
home is touched, and a failed step is retried while a finished one never is —
plus the ordering inside ``RuntimeServer.start`` (before the vault unlock and
the API bind, with the roster refreshed so the new agent boots).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from olano.runtime import first_boot as fb
from olano.users.store import get_user_store, reset_user_store


@pytest.fixture(autouse=True)
def _reset_user_store():
    reset_user_store()
    yield
    reset_user_store()


@pytest.fixture
def provisioning_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """What cloud-init + the compose env_file put in the container."""
    monkeypatch.setenv("LIONCLAW_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("LIONCLAW_ADMIN_PASSWORD", "temp-pass-9f2c")
    monkeypatch.setenv("LIONCLAW_VAULT_PASSWORD", "vault-pass-1a")
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", "vault-pass-1a")
    monkeypatch.setenv("LIONCLAW_DEFAULT_AGENT", "merli")


# ── what the environment asks for ────────────────────────────────────────────


def test_no_admin_password_means_no_provisioning() -> None:
    assert fb.provisioning_from_env({}) is None
    assert fb.provisioning_from_env({"LIONCLAW_ADMIN_EMAIL": "x@y.z"}) is None
    assert fb.provisioning_from_env({"LIONCLAW_ADMIN_PASSWORD": "   "}) is None


def test_env_precedence_mirrors_quick_setup() -> None:
    from olano.cli.quick_setup import DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_NAME, DEFAULT_AGENT

    spec = fb.provisioning_from_env({"LIONCLAW_ADMIN_PASSWORD": "pw", "LIONCLAW_VAULT_KEY": "vk"})
    assert spec is not None
    assert spec.admin_email == DEFAULT_ADMIN_EMAIL
    assert spec.admin_name == DEFAULT_ADMIN_NAME
    assert spec.agent == DEFAULT_AGENT
    # The vault is CREATED with the passphrase the runtime will UNLOCK it with.
    assert spec.vault_password == "vk"

    spec = fb.provisioning_from_env(
        {
            "LIONCLAW_ADMIN_PASSWORD": "pw",
            "LIONCLAW_ADMIN_EMAIL": "o@x.io",
            "LIONCLAW_ADMIN_NAME": "Dana",
            "LIONCLAW_VAULT_PASSWORD": "vp",
            "LIONCLAW_VAULT_KEY": "vk",
            "LIONCLAW_DEFAULT_AGENT": "agentfather",
        }
    )
    assert spec == fb.Provisioning("o@x.io", "pw", "Dana", "vp", "agentfather")


def test_no_vault_variable_means_a_generated_vault_passphrase() -> None:
    """Neither LIONCLAW_VAULT_PASSWORD nor LIONCLAW_VAULT_KEY: quick-setup
    generates the passphrase (there is no weak default to fall back on)."""
    spec = fb.provisioning_from_env({"LIONCLAW_ADMIN_PASSWORD": "pw"})
    assert spec is not None
    assert spec.vault_password is None


def test_provisioning_without_a_vault_key_leaves_a_boot_key_the_unlock_reads(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compose box with only an admin password in .env: the vault is created
    under a generated passphrase and the boot-key file carries it to the
    unlock that `serve` runs right after this hook, so the runtime opens the
    same vault quick-setup made, with no env key anywhere."""
    from olano.vault.runtime import read_boot_key, unlock_vault_for_session

    monkeypatch.setenv("LIONCLAW_ADMIN_PASSWORD", "temp-pass-9f2c")
    monkeypatch.delenv("LIONCLAW_VAULT_PASSWORD", raising=False)
    monkeypatch.delenv("LIONCLAW_VAULT_KEY", raising=False)

    result = fb.first_boot_provisioning()
    assert result is not None
    assert dict(result.steps)["vault"] == "initialized"
    assert result.vault_passphrase_generated is True
    assert result.vault_password and read_boot_key() == result.vault_password

    unlocked = unlock_vault_for_session(register=False)
    assert unlocked.vault is not None and unlocked.mode == "bootfile"


# ── the hook itself ──────────────────────────────────────────────────────────


def test_without_credentials_the_hook_is_a_no_op(lionclaw_home: Path) -> None:
    assert fb.first_boot_provisioning() is None
    assert not fb.state_path().exists()
    assert get_user_store().db.count_users() == 0


def test_an_empty_home_is_provisioned_from_the_environment(
    lionclaw_home: Path, provisioning_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.config.home import load_home_config
    from olano.config.loader import list_agents
    from olano.vault.store import default_vault_path

    clones: list[str] = []
    monkeypatch.setattr(
        "olano.archetypes.sync.fetch_lionclaw_agents", lambda **k: clones.append("a") or (True, "")
    )
    monkeypatch.setattr(
        "olano.skills.sync.fetch_olano_skills", lambda *a, **k: clones.append("s") or (True, "")
    )

    result = fb.first_boot_provisioning()
    assert result is not None
    statuses = dict(result.steps)
    assert statuses["admin"] == "created"
    assert statuses["agent"] == "installed"
    assert statuses["vault"] == "initialized"

    # The provisioned sign-in: the owner's email, the temporary password, handle `admin`.
    store = get_user_store()
    admin = store.authenticate("owner@example.com", "temp-pass-9f2c")
    assert admin is not None and admin.is_admin and admin.username == "admin"
    assert load_home_config().multi_user_enabled is True
    assert load_home_config().default_agent == "merli"
    assert "merli" in list_agents(include_hidden=True)
    assert default_vault_path().exists()
    # Never a network wait at boot: the pack clones belong to the deploy script.
    assert clones == []

    state = json.loads(fb.state_path().read_text())
    assert state["source"] == "env"
    assert state["steps"]["admin"] == "created"

    # Final: the next boot does nothing, and asks nothing of the DB.
    monkeypatch.setattr(fb, "_user_count", lambda: (_ for _ in ()).throw(AssertionError("asked")))
    assert fb.first_boot_provisioning() is None


def test_a_home_someone_else_provisioned_is_left_alone(
    lionclaw_home: Path, provisioning_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older box upgrading into this: quick-setup already ran (or the owner
    created the first account). Nothing is (re)installed — not even the
    default agent, which that owner may have deleted on purpose."""
    get_user_store().bootstrap_admin(email="me@x.io", password="pw", name="Me")

    installs: list[str] = []
    monkeypatch.setattr(
        "olano.archetypes.installer.install_archetype", lambda *a, **k: installs.append("x")
    )
    assert fb.first_boot_provisioning() is None
    assert installs == []
    assert json.loads(fb.state_path().read_text())["source"] == "external"
    # And it stays final.
    assert fb.first_boot_provisioning() is None
    assert installs == []


def test_a_failed_agent_install_is_retried_on_the_next_boot(
    lionclaw_home: Path, provisioning_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quarantine race: first boot's agent install is refused, the admin
    lands anyway, and the agent is installed on the boot where it can be."""
    from olano.archetypes import installer
    from olano.config.loader import list_agents

    real_install = installer.install_archetype
    refuse = {"on": True}

    def _gated(*a, **k):
        if refuse["on"]:
            raise RuntimeError("This deployment is quarantined — creating agents is disabled")
        return real_install(*a, **k)

    monkeypatch.setattr("olano.archetypes.installer.install_archetype", _gated)
    first = fb.first_boot_provisioning()
    assert first is not None
    assert dict(first.steps)["admin"] == "created"
    assert dict(first.steps)["agent"].startswith("failed: ")
    assert fb.steps_to_retry(fb.load_state()) == ["agent"]
    assert get_user_store().db.count_users() == 1

    refuse["on"] = False
    reset_user_store()
    second = fb.first_boot_provisioning()
    assert second is not None
    assert dict(second.steps)["admin"] == "skipped"
    assert dict(second.steps)["agent"] == "installed"
    assert "merli" in list_agents(include_hidden=True)
    assert fb.steps_to_retry(fb.load_state()) == []
    assert get_user_store().db.count_users() == 1

    # Done for good — a later delete of the agent is respected.
    assert fb.first_boot_provisioning() is None


# ── the owner's first sign-in, end to end ────────────────────────────────────
#
# The contract the wizard is built on, pinned against the provisioning path:
# the box is reachable ONLY through the sign-in screen (never account
# creation), the owner signs in with the temporary password, the profile step
# turns that account into their own user (name, handle, password), and when
# the tour ends they are signed in as that user — the temporary password dead.


def test_the_owner_signs_in_with_the_temporary_password_and_leaves_the_tour_as_their_own_user(
    lionclaw_home: Path, provisioning_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.deployment import credential_rotation as cr
    from olano.runtime.api import create_api
    from olano.users.db import PROVISIONED_ADMIN_HANDLE

    monkeypatch.setattr(cr, "schedule_report", lambda home=None: None)  # no cloud to report to
    monkeypatch.setattr(
        "olano.archetypes.sync.fetch_lionclaw_agents", lambda **k: (True, "stubbed")
    )
    monkeypatch.setattr("olano.skills.sync.fetch_olano_skills", lambda *a, **k: (True, "stubbed"))

    # What `serve` does before its API answers.
    assert fb.first_boot_provisioning() is not None

    runtime = MagicMock()
    runtime._runners = {}
    app = create_api(runtime)
    browser = TestClient(app)

    # 1. The first screen is Sign in — `auth_required`, never `setup_required`
    #    (which is the account-creation screen an empty user table produces).
    anon = browser.get("/api/auth/me")
    assert anon.status_code == 401
    assert anon.json()["detail"] == "auth_required"

    # 2. The temporary password from the welcome email signs the owner in, on
    #    the provisioned account, and the wizard still has a claim to offer.
    r = browser.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "temp-pass-9f2c"}
    )
    assert r.status_code == 200, r.text
    me = browser.get("/api/auth/me").json()
    assert me["user"]["username"] == PROVISIONED_ADMIN_HANDLE
    assert me["onboarding"]["required"] is True
    assert me["onboarding"]["admin_password_rotated"] is False

    # 3. The profile step: the owner creates their own user on that account.
    claimed = browser.post(
        "/api/auth/claim", json={"name": "Carlos Rivera", "password": "my-own-password"}
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["user"]["username"] == "carlos-rivera"

    # 4. The tour ends — and the same browser is signed in as that user.
    done = browser.post(
        "/api/onboarding/complete",
        json={"steps": {"profile": "done", "provider": "skipped", "agentfather": "done"}},
    )
    assert done.status_code == 200, done.text
    after = browser.get("/api/auth/me")
    assert after.status_code == 200
    assert after.json()["user"]["name"] == "Carlos Rivera"
    assert after.json()["user"]["username"] == "carlos-rivera"
    assert after.json()["user"]["email"] == "owner@example.com"
    assert after.json()["user"]["is_admin"] is True
    assert after.json()["onboarding"]["required"] is False

    # The temporary password is dead; the owner's own one is the sign-in now.
    fresh = TestClient(app)
    assert (
        fresh.post(
            "/api/auth/login", json={"email": "owner@example.com", "password": "temp-pass-9f2c"}
        ).status_code
        == 401
    )
    assert (
        fresh.post(
            "/api/auth/login", json={"email": "owner@example.com", "password": "my-own-password"}
        ).status_code
        == 200
    )


# ── the sign-in screen's temporary-password hint ─────────────────────────────
#
# A just-deployed managed box shows an email + password form to an owner who
# has never chosen a password. The hint tells that form where the temporary
# one is (the welcome email, the console's Security tab), and only while it is
# still the password: never on a box it was not provisioned onto, never after
# it was replaced, never off a managed-cloud deployment.

DEP_ID = "0b6f7c1e-3a2d-4c8e-9f10-2d5b7e8a9c41"


@pytest.fixture
def cloud_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the installation manifest at a tmp file the test writes on demand.

    Written after provisioning, never before: a managed manifest with no
    settings artifact is the quarantine posture, and provisioning would sit out
    its settle wait for the artifact that never comes.
    """
    from olano.deployment import resolver

    path = tmp_path / "installation.json"
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(path))
    resolver.invalidate()

    def write(mode: str = "cloud") -> None:
        path.write_text(json.dumps({"managed": True, "mode": mode, "deployment_id": DEP_ID}))
        resolver.invalidate()

    yield write
    resolver.invalidate()


def test_the_sign_in_screen_points_a_new_owner_at_the_temporary_password(
    lionclaw_home: Path, provisioning_env, cloud_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.deployment import credential_rotation as cr
    from olano.runtime.api import create_api

    monkeypatch.setattr(cr, "schedule_report", lambda home=None: None)  # no cloud to report to
    assert fb.first_boot_provisioning() is not None
    cloud_manifest()

    runtime = MagicMock()
    runtime._runners = {}
    app = create_api(runtime)
    anon = TestClient(app)

    # Nobody is signed in and the hint still answers: the sign-in screen reads it.
    assert anon.get("/api/auth/me").json()["detail"] == "auth_required"
    hint = anon.get("/api/auth/sign-in-hint")
    assert hint.status_code == 200, hint.text
    assert hint.json() == {"temporary_password": {"deployment_id": DEP_ID}}

    # The owner signs in with it and makes the account their own; from then on
    # the sign-in screen is the ordinary one.
    owner = TestClient(app)
    login = owner.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "temp-pass-9f2c"}
    )
    assert login.status_code == 200, login.text
    claimed = owner.post(
        "/api/auth/claim", json={"name": "Carlos Rivera", "password": "my-own-password"}
    )
    assert claimed.status_code == 200, claimed.text
    assert anon.get("/api/auth/sign-in-hint").json() == {"temporary_password": None}


def test_a_password_change_retires_the_hint_too(
    lionclaw_home: Path, provisioning_env, cloud_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner who skips the tour and changes the password from the account
    menu instead: the other way the temporary password dies."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.deployment import credential_rotation as cr
    from olano.runtime.api import create_api

    monkeypatch.setattr(cr, "schedule_report", lambda home=None: None)
    assert fb.first_boot_provisioning() is not None
    cloud_manifest()
    runtime = MagicMock()
    runtime._runners = {}
    owner = TestClient(create_api(runtime))
    owner.post("/api/auth/login", json={"email": "owner@example.com", "password": "temp-pass-9f2c"})
    assert owner.get("/api/auth/sign-in-hint").json()["temporary_password"] is not None

    changed = owner.post(
        "/api/auth/password",
        json={"current_password": "temp-pass-9f2c", "new_password": "a-better-one"},
    )
    assert changed.status_code == 200, changed.text
    assert owner.get("/api/auth/sign-in-hint").json() == {"temporary_password": None}


def test_the_hint_needs_a_managed_cloud_manifest(lionclaw_home: Path, cloud_manifest) -> None:
    fb._write_state({"source": "env", "steps": {"admin": "created"}})
    # No manifest: a self-hosted box has no console holding a copy.
    assert fb.temporary_password_deployment_id() is None
    # Managed-local: licensed offline, no console either.
    cloud_manifest("local")
    assert fb.temporary_password_deployment_id() is None
    cloud_manifest("cloud")
    assert fb.temporary_password_deployment_id() == DEP_ID


def test_the_hint_is_never_offered_on_a_home_someone_else_provisioned(
    lionclaw_home: Path, cloud_manifest
) -> None:
    """An older box upgrading into this, or an owner who created the first
    account on the setup screen: nothing says that password was ever temporary."""
    cloud_manifest()
    assert fb.temporary_password_deployment_id() is None  # no first-boot record at all
    fb._write_state({"source": "external", "steps": {}})
    assert fb.temporary_password_deployment_id() is None


def test_the_hint_follows_the_admin_step(lionclaw_home: Path, cloud_manifest) -> None:
    cloud_manifest()
    # The retry pass after a refused agent install records the admin step
    # `skipped`: the same provisioned account, still on its temporary password.
    fb._write_state({"source": "env", "steps": {"admin": "skipped", "agent": "installed"}})
    assert fb.temporary_password_deployment_id() == DEP_ID
    # A failed admin step provisioned nothing, so there is no password to find.
    fb._write_state({"source": "env", "steps": {"admin": "failed: users.db is read-only"}})
    assert fb.temporary_password_deployment_id() is None


def test_the_hint_goes_quiet_once_the_password_is_replaced(
    lionclaw_home: Path, cloud_manifest
) -> None:
    from olano.deployment import credential_rotation as cr

    cloud_manifest()
    fb._write_state({"source": "env", "steps": {"admin": "created"}})
    assert fb.temporary_password_deployment_id() == DEP_ID
    cr.record_admin_password_rotation()
    assert fb.temporary_password_deployment_id() is None


# ── inside RuntimeServer.start ───────────────────────────────────────────────


async def test_start_provisions_before_the_vault_unlock_and_refreshes_the_roster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from olano.runtime import server as server_mod

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")

    calls: list[str] = []
    rosters = iter([[], ["agentfather"]])
    monkeypatch.setattr(server_mod, "list_agents", lambda **k: next(rosters))

    server = server_mod.RuntimeServer()  # no explicit agents → the on-disk roster
    assert server.agent_names == []

    class _StopBoot(Exception):
        pass

    def fake_provision():
        calls.append("provision")
        return SimpleNamespace(steps=[("admin", "created"), ("agent", "installed")])

    monkeypatch.setattr("olano.runtime.first_boot.first_boot_provisioning", fake_provision)
    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(server.webhook_dispatcher, "start", lambda: None)
    monkeypatch.setattr(server, "_unlock_vault_for_boot", lambda: calls.append("unlock"))
    monkeypatch.setattr(server, "_should_boot_on_start", lambda n: True)

    async def fake_api() -> None:
        calls.append("api")

    monkeypatch.setattr(server, "_start_api", fake_api)

    async def fake_boot(name: str) -> None:
        calls.append(f"boot:{name}")
        raise _StopBoot()

    monkeypatch.setattr(server, "_boot_agent", fake_boot)

    with pytest.raises(_StopBoot):
        await server.start()

    # Provisioned before the vault opened and before the API could answer with
    # zero users; the agent it installed is in the roster the loop boots.
    assert calls == ["provision", "unlock", "api", "boot:agentfather"]
    assert server.agent_names == ["agentfather"]


async def test_start_keeps_an_explicit_roster_and_survives_a_provisioning_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from olano.runtime.server import RuntimeServer

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")

    server = RuntimeServer(agent_names=["merli"])

    def broken():
        raise RuntimeError("users.db is read-only")

    monkeypatch.setattr("olano.runtime.first_boot.first_boot_provisioning", broken)
    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(server.webhook_dispatcher, "start", lambda: None)
    monkeypatch.setattr(server, "_unlock_vault_for_boot", lambda: None)
    monkeypatch.setattr(server, "_should_boot_on_start", lambda n: True)

    async def fake_api() -> None:
        pass

    class _StopBoot(Exception):
        pass

    async def fake_boot(name: str) -> None:
        raise _StopBoot(name)

    monkeypatch.setattr(server, "_start_api", fake_api)
    monkeypatch.setattr(server, "_boot_agent", fake_boot)

    with pytest.raises(_StopBoot):  # the boot went on to its agents
        await server.start()
    assert server.agent_names == ["merli"]


# ── the settled hook: retry the refused step when the settings arrive ────────


async def test_settled_hook_retries_the_refused_agent_install_and_boots_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A box that lost the quarantine race no longer waits for "the next boot"
    (which a running box never has): the moment the settings poller lands the
    posture outside quarantine, the failed step re-runs, the roster is
    refreshed and the newly installed agent is booted and announced."""
    from olano.runtime import server as server_mod

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")

    rosters = iter([[], ["agentfather"]])
    monkeypatch.setattr(server_mod, "list_agents", lambda **k: next(rosters))
    server = server_mod.RuntimeServer()
    assert server.agent_names == []

    calls: list[str] = []
    monkeypatch.setattr(
        "olano.runtime.first_boot.load_state",
        lambda: {"steps": {"admin": "created", "agent": "failed: quarantined"}},
    )

    def fake_provision():
        calls.append("provision")
        return SimpleNamespace(
            agent="agentfather", steps=[("admin", "skipped"), ("agent", "installed")]
        )

    monkeypatch.setattr("olano.runtime.first_boot.first_boot_provisioning", fake_provision)

    async def fake_boot(name: str) -> None:
        calls.append(f"boot:{name}")

    monkeypatch.setattr(server, "_boot_agent", fake_boot)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "olano.runtime.api._push_event", lambda kind, payload: events.append((kind, payload))
    )

    await server._retry_provisioning_after_settings()

    assert calls == ["provision", "boot:agentfather"]
    assert server.agent_names == ["agentfather"]
    assert events == [("agents_update", {"name": "agentfather"})]


async def test_settled_hook_is_a_no_op_when_nothing_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The poller calls the hook on every settled transition for the life of
    the process; with nothing recorded failed it must touch nothing."""
    from olano.runtime.server import RuntimeServer

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")
    server = RuntimeServer(agent_names=["merli"])

    monkeypatch.setattr(
        "olano.runtime.first_boot.load_state",
        lambda: {"steps": {"admin": "created", "agent": "installed"}},
    )

    def never():
        raise AssertionError("provisioning must not run")

    monkeypatch.setattr("olano.runtime.first_boot.first_boot_provisioning", never)
    await server._retry_provisioning_after_settings()
    assert server.agent_names == ["merli"]


async def test_settled_hook_survives_a_retry_that_fails_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from olano.runtime.server import RuntimeServer

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")
    server = RuntimeServer(agent_names=["merli"])

    monkeypatch.setattr(
        "olano.runtime.first_boot.load_state",
        lambda: {"steps": {"admin": "created", "agent": "failed: quarantined"}},
    )
    monkeypatch.setattr(
        "olano.runtime.first_boot.first_boot_provisioning",
        lambda: SimpleNamespace(agent="agentfather", steps=[("agent", "failed: still")]),
    )

    async def never(name: str) -> None:
        raise AssertionError("nothing to boot")

    monkeypatch.setattr(server, "_boot_agent", never)
    await server._retry_provisioning_after_settings()  # no raise, no boot
    assert server.agent_names == ["merli"]
