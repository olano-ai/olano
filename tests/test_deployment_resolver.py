"""Resolver state machine — full posture matrices for every installation state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from olano.deployment import resolver, sources
from olano.deployment.envelope import sign_license

NOW = 1_780_000_000
KEY_ID = "settings-test-2026-07"
DEP_ID = "dep_resolver_test"

_PRIVATE = ed25519.Ed25519PrivateKey.generate()
_KEYS = {KEY_ID: _PRIVATE.public_key()}


def make_payload(**overrides) -> dict:
    payload: dict = {
        "schema": "olano.deployment-settings",
        "schema_version": 1,
        "issuer": "cloud.olano.ai",
        "audience": "olano-deployment-settings",
        "channel": "local",
        "license_id": "lic_test",
        "deployment_id": DEP_ID,
        "plan": "pro",
        "policy_revision": 5,
        "issued_at": NOW - 1000,
        "not_before": NOW - 1000,
        "refresh_after": NOW + 900,
        "expires_at": NOW + 86400,
        "settings": {"resources": {"max_agents": 7}},
    }
    payload.update(overrides)
    return payload


def mint(**overrides) -> str:
    return sign_license(make_payload(**overrides), _PRIVATE, key_id=KEY_ID)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Isolated home + no manifest + test verification keys wired in."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
    monkeypatch.delenv("LIONCLAW_LICENSE", raising=False)
    monkeypatch.delenv("LIONCLAW_LICENSE_PATH", raising=False)
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_MANAGED", raising=False)
    # The resolver verifies against the embedded production keys; tests
    # substitute their own keypair at the envelope seam.
    monkeypatch.setattr("olano.deployment.envelope.load_verify_keys", lambda: _KEYS)
    resolver.invalidate()
    yield tmp_path
    resolver.invalidate()


def write_manifest(home: Path, *, mode: str, deployment_id: str = DEP_ID, managed=True) -> None:
    Path(home / "installation.json").write_text(
        json.dumps({"managed": managed, "mode": mode, "deployment_id": deployment_id})
    )


def install_license(home: Path, text: str) -> Path:
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.deployment_license_path.write_text(text + "\n")
    return paths.deployment_license_path


class TestUnmanaged:
    def test_no_manifest_is_open(self, home):
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "open"
        assert status.source == "none"
        assert status.settings.resources.max_agents is None

    def test_managed_false_manifest_is_open(self, home):
        write_manifest(home, mode="local", managed=False)
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "open"

    def test_license_without_manifest_is_still_open(self, home):
        # A license alone doesn't make an install managed (and can't restrict it).
        install_license(home, mint())
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "open"

    def test_invalid_manifest_fails_closed(self, home):
        Path(home / "installation.json").write_text("{not json")
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "invalid_manifest"

    def test_manifest_missing_deployment_id_fails_closed(self, home):
        Path(home / "installation.json").write_text(json.dumps({"managed": True, "mode": "local"}))
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"


class TestManagedLocal:
    def test_valid_license_is_enforced(self, home):
        write_manifest(home, mode="local")
        install_license(home, mint())
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "enforced"
        assert status.source == "local_license"
        assert status.settings.resources.max_agents == 7
        assert status.plan == "pro"
        assert status.policy_revision == 5

    def test_missing_license_quarantines_never_opens(self, home):
        write_manifest(home, mode="local")
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "missing_license"
        # Quarantine settings, not open ones.
        assert status.settings.resources.max_agents == 1
        assert status.settings.feature_enabled("shell") is False

    def test_deleted_license_quarantines(self, home):
        write_manifest(home, mode="local")
        path = install_license(home, mint())
        assert resolver.deployment_status(now=NOW).posture == "enforced"
        path.unlink()
        resolver.invalidate()
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "missing_license"

    def test_mismatched_deployment_id_quarantines(self, home):
        write_manifest(home, mode="local", deployment_id="dep_other")
        install_license(home, mint())
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "deployment_mismatch"

    def test_expired_license_quarantines(self, home):
        write_manifest(home, mode="local")
        install_license(home, mint(expires_at=NOW - 10))
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "expired"

    def test_tampered_license_quarantines(self, home):
        write_manifest(home, mode="local")
        text = mint()
        prefix, key_id, payload, sig = text.split(".")
        install_license(home, f"{prefix}.{key_id}.{payload}x.{sig}")
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"

    def test_symlinked_license_quarantines(self, home):
        write_manifest(home, mode="local")
        from olano.config.home import get_olano_paths

        paths = get_olano_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        real = home / "elsewhere.lic"
        real.write_text(mint())
        paths.deployment_license_path.symlink_to(real)
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "unreadable_license"

    def test_cloud_channel_license_rejected_in_local_mode(self, home):
        write_manifest(home, mode="local")
        install_license(home, mint(channel="cloud"))
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "wrong_channel"


class TestManagedCloud:
    def _configure(self, home, monkeypatch, responses):
        """Wire a scripted fetch_remote: each call pops the next item —
        a license string to return or an exception to raise."""
        write_manifest(home, mode="cloud")
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_test")
        calls = list(responses)

        def fake_fetch(cfg):
            item = calls.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(sources, "fetch_remote", fake_fetch)

    def test_fresh_fetch_is_enforced_cloud(self, home, monkeypatch):
        self._configure(home, monkeypatch, [mint(channel="cloud")])
        status = resolver.refresh(now=NOW)
        assert status.posture == "enforced"
        assert status.source == "cloud"
        assert status.settings.resources.max_agents == 7
        assert status.last_refresh == NOW

    def test_local_license_ignored_in_cloud_mode(self, home, monkeypatch):
        # A (more permissive) local license must not resurrect via cloud outage.
        install_license(home, mint(settings={"resources": {"max_agents": 999}}))
        self._configure(home, monkeypatch, [sources.RemoteFetchError("transient", "unreachable")])
        status = resolver.refresh(now=NOW)
        assert status.posture == "quarantine"  # no cache, no fetch — never the license
        assert status.settings.resources.max_agents == 1

    def test_cache_survives_transient_failures(self, home, monkeypatch):
        self._configure(
            home,
            monkeypatch,
            [mint(channel="cloud"), sources.RemoteFetchError("transient", "HTTP 503")],
        )
        assert resolver.refresh(now=NOW).source == "cloud"
        status = resolver.refresh(now=NOW + 2000)  # past refresh_after (NOW+900)
        assert status.posture == "enforced"
        assert status.source == "cloud_cache"
        assert status.error and "transient" in status.error

    def test_cache_seeds_from_disk_across_restart(self, home, monkeypatch):
        self._configure(home, monkeypatch, [mint(channel="cloud")])
        resolver.refresh(now=NOW)
        resolver.invalidate()  # simulate process restart
        status = resolver.deployment_status(now=NOW + 100)
        assert status.posture == "enforced"
        assert status.source == "cloud_cache"  # no live fetch this process
        assert status.settings.resources.max_agents == 7

    def test_cache_past_signed_expiry_quarantines(self, home, monkeypatch):
        self._configure(home, monkeypatch, [mint(channel="cloud", expires_at=NOW + 100)])
        resolver.refresh(now=NOW)
        status = resolver.deployment_status(now=NOW + 101)
        assert status.posture == "quarantine"
        assert status.error == "expired"

    def test_unsigned_cache_meta_cannot_extend_expiry(self, home, monkeypatch):
        from olano.config.home import get_olano_paths

        self._configure(home, monkeypatch, [mint(channel="cloud", expires_at=NOW + 100)])
        resolver.refresh(now=NOW)
        # Attacker edits the diagnostic meta file to a fresh fetched_at.
        meta = get_olano_paths().deployment_dir / sources.CACHE_META_FILE
        meta.write_text(json.dumps({"fetched_at": NOW + 10_000_000}))
        resolver.invalidate()
        status = resolver.deployment_status(now=NOW + 101)
        assert status.posture == "quarantine"
        assert status.error == "expired"

    def test_revoked_401_quarantines_immediately(self, home, monkeypatch):
        self._configure(
            home,
            monkeypatch,
            [mint(channel="cloud"), sources.RemoteFetchError("revoked", "HTTP 401")],
        )
        assert resolver.refresh(now=NOW).posture == "enforced"
        status = resolver.refresh(now=NOW + 10)
        assert status.posture == "quarantine"
        assert status.error == "revoked"

    def test_successful_fetch_clears_revocation(self, home, monkeypatch):
        self._configure(
            home,
            monkeypatch,
            [
                sources.RemoteFetchError("revoked", "HTTP 401"),
                mint(channel="cloud"),
            ],
        )
        assert resolver.refresh(now=NOW).posture == "quarantine"
        status = resolver.refresh(now=NOW + 10)
        assert status.posture == "enforced"
        assert status.source == "cloud"

    def test_rollback_lower_revision_rejected(self, home, monkeypatch):
        self._configure(
            home,
            monkeypatch,
            [
                mint(
                    channel="cloud", policy_revision=10, settings={"resources": {"max_agents": 2}}
                ),
                mint(
                    channel="cloud", policy_revision=3, settings={"resources": {"max_agents": 99}}
                ),
            ],
        )
        assert resolver.refresh(now=NOW).settings.resources.max_agents == 2
        status = resolver.refresh(now=NOW + 10)
        # Old artifact rejected; current one kept.
        assert status.settings.resources.max_agents == 2
        assert status.policy_revision == 10
        assert status.error == "rollback"

    def test_rollback_survives_restart_via_revision_mark(self, home, monkeypatch):
        from olano.config.home import get_olano_paths

        self._configure(home, monkeypatch, [mint(channel="cloud", policy_revision=10)])
        resolver.refresh(now=NOW)
        # Replay attack across restart: swap an older signed artifact into the cache.
        old = mint(channel="cloud", policy_revision=3, settings={"resources": {"max_agents": 99}})
        cache = get_olano_paths().deployment_dir / sources.CACHE_FILE
        cache.write_text(old + "\n")
        resolver.invalidate()
        status = resolver.deployment_status(now=NOW + 10)
        assert status.posture == "quarantine"
        assert status.error == "rollback"

    def test_higher_revision_accepted(self, home, monkeypatch):
        self._configure(
            home,
            monkeypatch,
            [
                mint(channel="cloud", policy_revision=10),
                mint(
                    channel="cloud",
                    policy_revision=11,
                    issued_at=NOW,
                    settings={"resources": {"max_agents": 4}},
                ),
            ],
        )
        resolver.refresh(now=NOW)
        status = resolver.refresh(now=NOW + 10)
        assert status.policy_revision == 11
        assert status.settings.resources.max_agents == 4

    def test_local_channel_artifact_from_cloud_rejected(self, home, monkeypatch):
        self._configure(home, monkeypatch, [mint(channel="local")])
        status = resolver.refresh(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "wrong_channel"

    def test_invalid_body_keeps_last_known_good(self, home, monkeypatch):
        self._configure(
            home,
            monkeypatch,
            [mint(channel="cloud"), sources.RemoteFetchError("invalid", "no license field")],
        )
        resolver.refresh(now=NOW)
        status = resolver.refresh(now=NOW + 10)
        assert status.posture == "enforced"

    def test_no_token_with_no_cache_quarantines(self, home, monkeypatch):
        write_manifest(home, mode="cloud")
        status = resolver.refresh(now=NOW)
        assert status.posture == "quarantine"
        assert "not_configured" in (status.error or "")

    def test_corrupt_cache_quarantines(self, home, monkeypatch):
        from olano.config.home import get_olano_paths

        write_manifest(home, mode="cloud")
        d = get_olano_paths().deployment_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / sources.CACHE_FILE).write_text("LCDS1.garbage")
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"

    def test_wrong_deployment_cache_rejected(self, home, monkeypatch):
        from olano.config.home import get_olano_paths

        write_manifest(home, mode="cloud")
        d = get_olano_paths().deployment_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / sources.CACHE_FILE).write_text(
            mint(channel="cloud", deployment_id="dep_someone_else") + "\n"
        )
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "deployment_mismatch"


class TestFetchClassification:
    """HTTP → RemoteFetchError.kind mapping in sources.fetch_remote."""

    def _fetch(self, monkeypatch, response=None, error=None):
        import httpx

        def fake_get(url, **kwargs):
            if error is not None:
                raise error
            return response

        monkeypatch.setattr(httpx, "get", fake_get)
        return sources.fetch_remote(sources.CloudConfig(url="https://x", token="t"))

    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_failure_is_revoked(self, monkeypatch, code):
        import httpx

        with pytest.raises(sources.RemoteFetchError) as exc:
            self._fetch(monkeypatch, response=httpx.Response(code))
        assert exc.value.kind == "revoked"

    @pytest.mark.parametrize("code", [429, 500, 502, 503])
    def test_server_errors_are_transient(self, monkeypatch, code):
        import httpx

        with pytest.raises(sources.RemoteFetchError) as exc:
            self._fetch(monkeypatch, response=httpx.Response(code))
        assert exc.value.kind == "transient"

    def test_network_error_is_transient(self, monkeypatch):
        import httpx

        with pytest.raises(sources.RemoteFetchError) as exc:
            self._fetch(monkeypatch, error=httpx.ConnectError("boom"))
        assert exc.value.kind == "transient"

    def test_missing_license_field_is_invalid(self, monkeypatch):
        import httpx

        with pytest.raises(sources.RemoteFetchError) as exc:
            self._fetch(monkeypatch, response=httpx.Response(200, json={"nope": 1}))
        assert exc.value.kind == "invalid"

    def test_ok_returns_license(self, monkeypatch):
        import httpx

        text = self._fetch(
            monkeypatch, response=httpx.Response(200, json={"license": "LCDS1.x.y.z"})
        )
        assert text == "LCDS1.x.y.z"


class TestNeverRaises:
    def test_status_never_raises_on_internal_error(self, home, monkeypatch):
        write_manifest(home, mode="local")
        monkeypatch.setattr(
            sources, "load_local_license", lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
        )
        status = resolver.deployment_status(now=NOW)
        # Managed + internal failure ⇒ quarantine, not a crash, not open.
        assert status.posture == "quarantine"

    def test_unmanaged_internal_error_stays_open(self, home, monkeypatch):
        monkeypatch.setattr(
            resolver, "_resolve_locked", lambda now: (_ for _ in ()).throw(RuntimeError)
        )
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "open"


class TestBrokenManagedInstallNeverFallsOpen:
    """A box provisioned as managed must never resolve to the open posture.

    Production regression: cloud-init writes the manifest to /etc/olano on the
    HOST, but the runtime runs in a container and the compose file mounted only
    /opt/olano. read_manifest() therefore found nothing inside the container,
    the resolver called that "unmanaged", and EVERY protection gate went inert
    on a box holding /app + the vault — silently, with no error anywhere.

    The manifest mount is fixed (deploy/docker-compose.allinone.yml), but the
    resolver must not depend on that being right: any box carrying a managed
    marker with no usable manifest is BROKEN, and broken fails closed.
    """

    def test_deployment_token_without_manifest_quarantines(self, home, monkeypatch):
        """The exact production shape: token in the env, manifest unreachable."""
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "tok_live")
        resolver.invalidate()
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "manifest_missing"

    def test_managed_flag_without_manifest_quarantines(self, home, monkeypatch):
        """The marker is independent of the token, so rotation can't un-manage."""
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_MANAGED", "1")
        resolver.invalidate()
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "quarantine"
        assert status.error == "manifest_missing"

    def test_genuinely_unmanaged_box_stays_open(self, home):
        """Self-hosted installs are untouched — no markers, no manifest, open."""
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "open"

    def test_account_credits_key_is_not_a_managed_marker(self, home, monkeypatch):
        """A pasted Olano API key buys credits for a self-hosted box; it is the
        one credential that must NOT read as provisioning evidence, or pasting
        it would quarantine the box the way a stray deployment token does."""
        monkeypatch.setenv("OLANO_CREDITS_KEY", "sk-olano-account")
        monkeypatch.setenv("LIONCLAW_CREDITS_KEY", "sk-olano-account")
        resolver.invalidate()
        status = resolver.deployment_status(now=NOW)
        assert status.posture == "open"
        assert status.error is None

    def test_error_names_the_marker_and_support_contact(self, home, monkeypatch, caplog):
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "tok_live")
        resolver.invalidate()
        with caplog.at_level("ERROR", logger="olano.deployment.resolver"):
            resolver.deployment_status(now=NOW)
        assert "support@olano.ai" in caplog.text
        assert "LIONCLAW_DEPLOYMENT_TOKEN" in caplog.text  # names the evidence
        assert "quarantined" in caplog.text

    def test_quarantine_closes_every_execution_gate(self, home, monkeypatch):
        """The posture is only worth anything if the gates actually follow it."""
        from olano.core import mcp_setup
        from olano.deployment import enforcement
        from olano.runtime import terminal

        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "tok_live")
        monkeypatch.delenv("LIONCLAW_ENABLE_TERMINAL", raising=False)
        monkeypatch.delenv("LIONCLAW_DISABLE_TERMINAL", raising=False)
        resolver.invalidate()

        assert enforcement.require_sandboxed_exec() is True  # no raw host shell
        assert enforcement.sandbox_exec_enabled() is False  # and no sandbox either
        assert terminal.terminal_enabled() is False  # no PTY at all
        assert terminal.terminal_uses_sandbox() is True  # never a host shell
        assert mcp_setup.stdio_mcp_blocked() is True  # no arbitrary host command


class TestPostureGatesFailClosed:
    """If the entitlements layer breaks outright, restrict — never permit."""

    @staticmethod
    def _boom(*_args, **_kwargs):
        raise RuntimeError("entitlements layer exploded")

    def test_require_sandboxed_exec_fails_closed(self, monkeypatch):
        from olano.deployment import enforcement

        monkeypatch.setattr(enforcement.resolver, "deployment_status", self._boom)
        assert enforcement.require_sandboxed_exec() is True

    def test_terminal_uses_sandbox_fails_closed(self, monkeypatch):
        """A host PTY is the worst possible fallback — must not be the default."""
        from olano.deployment import enforcement
        from olano.runtime import terminal

        monkeypatch.setattr(enforcement, "require_sandboxed_exec", self._boom)
        assert terminal.terminal_uses_sandbox() is True

    def test_stdio_mcp_blocked_fails_closed(self, monkeypatch):
        from olano.core import mcp_setup
        from olano.deployment import enforcement

        monkeypatch.setattr(enforcement, "require_sandboxed_exec", self._boom)
        assert mcp_setup.stdio_mcp_blocked() is True


def test_compose_mounts_the_installation_manifest():
    """The mount whose absence caused the production fail-open.

    A unit test can't catch a missing bind mount, so assert it directly: the
    runtime reads /etc/olano/installation.json from its OWN filesystem, so the
    host directory must be mounted into the container or managed posture can
    never engage.
    """
    compose = Path(__file__).resolve().parents[1] / "deploy" / "docker-compose.allinone.yml"
    assert "/etc/olano:/etc/olano:ro" in compose.read_text(encoding="utf-8")


# ── settled / wait_until_settled: "no artifact yet" is not a verdict ─────────
#
# A managed-cloud box resolves to quarantine from boot until its first signed
# artifact is accepted. The first-boot agent install used to be judged by that
# absence and refused; the recovery was "the next boot", which a running box
# never has. `settled` names the difference, `wait_until_settled` waits for it.


def _posture(posture, *, mode="cloud", error=None, source="quarantine"):
    from olano.deployment.resolver import DeploymentStatus
    from olano.deployment.schema import DeploymentSettings

    return DeploymentStatus(
        posture=posture, source=source, settings=DeploymentSettings(), mode=mode, error=error
    )


def test_settled_tells_no_artifact_yet_from_a_real_quarantine():
    from olano.deployment.resolver import settled

    # Final for now: waiting would change nothing.
    assert settled(_posture("open", mode=None, source="none"))
    assert settled(_posture("enforced", source="cloud"))
    assert settled(_posture("quarantine", error="revoked: 401 from the control plane"))
    assert settled(_posture("quarantine", error="expired"))
    assert settled(_posture("quarantine", error="not_configured: no deployment token"))
    assert settled(_posture("quarantine", mode="local", error="no_license"))
    # Transient: the next fetch may lift it.
    assert not settled(_posture("quarantine", error="no_artifact"))
    assert not settled(_posture("quarantine", error="transient: 503 from the control plane"))
    assert not settled(_posture("quarantine", error="invalid: unparseable body"))


def test_wait_until_settled_refreshes_until_the_artifact_lands(monkeypatch):
    from olano.deployment import resolver

    answers = iter(
        [
            _posture("quarantine", error="no_artifact"),
            _posture("quarantine", error="transient: 503"),
            _posture("enforced", source="cloud"),
        ]
    )
    fetches: list[int] = []
    monkeypatch.setattr(resolver, "refresh", lambda **kw: fetches.append(1) or next(answers))
    slept: list[float] = []
    status = resolver.wait_until_settled(30, poll=2.0, sleep=slept.append, clock=lambda: 0.0)
    assert status.posture == "enforced"
    assert len(fetches) == 3, "one fetch up front, then one per poll"
    assert slept == [2.0, 2.0]


def test_wait_until_settled_gives_up_at_the_deadline_with_the_last_status(monkeypatch):
    from olano.deployment import resolver

    monkeypatch.setattr(
        resolver, "refresh", lambda **kw: _posture("quarantine", error="no_artifact")
    )
    now = [0.0]

    def _sleep(seconds: float) -> None:
        now[0] += seconds

    status = resolver.wait_until_settled(5, poll=2.0, sleep=_sleep, clock=lambda: now[0])
    assert status.quarantined and status.error == "no_artifact"
    assert now[0] == 5.0, "waited the whole budget and not a second longer"


def test_wait_until_settled_with_no_budget_refreshes_exactly_once_and_never_sleeps(monkeypatch):
    from olano.deployment import resolver

    fetches: list[int] = []
    monkeypatch.setattr(
        resolver,
        "refresh",
        lambda **kw: fetches.append(1) or _posture("quarantine", error="no_artifact"),
    )

    def _never(_seconds: float) -> None:
        raise AssertionError("slept with a zero budget")

    resolver.wait_until_settled(0, sleep=_never)
    assert fetches == [1]


def test_settle_timeout_reads_the_env_and_tolerates_garbage(monkeypatch):
    from olano.deployment import resolver

    monkeypatch.delenv(resolver.SETTLE_TIMEOUT_ENV, raising=False)
    assert resolver.settle_timeout() == resolver.DEFAULT_SETTLE_SECONDS
    monkeypatch.setenv(resolver.SETTLE_TIMEOUT_ENV, "0")
    assert resolver.settle_timeout() == 0.0
    monkeypatch.setenv(resolver.SETTLE_TIMEOUT_ENV, "12.5")
    assert resolver.settle_timeout() == 12.5
    monkeypatch.setenv(resolver.SETTLE_TIMEOUT_ENV, "-3")
    assert resolver.settle_timeout() == 0.0
    monkeypatch.setenv(resolver.SETTLE_TIMEOUT_ENV, "soon")
    assert resolver.settle_timeout() == resolver.DEFAULT_SETTLE_SECONDS
