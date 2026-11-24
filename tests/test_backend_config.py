"""Unit tests for BackendConfig discriminated union."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from olano.config.backend_config import (
    AgentCoreBackendConfig,
    CompositeBackendConfig,
    DaytonaBackendConfig,
    FilesystemBackendConfig,
    LangSmithBackendConfig,
    LocalShellBackendConfig,
    ModalBackendConfig,
    OlanoDockerBackendConfig,
    RunloopBackendConfig,
    StateBackendConfig,
    StoreBackendConfig,
    default_backend,
    is_remote_sandbox_backend,
    is_sandbox_backend,
)


class TestStateBackend:
    def test_default_type(self) -> None:
        cfg = StateBackendConfig()
        assert cfg.type == "state"

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            StateBackendConfig(root_dir="/tmp")  # type: ignore[call-arg]


class TestFilesystemBackend:
    def test_defaults(self) -> None:
        cfg = FilesystemBackendConfig()
        assert cfg.type == "filesystem"
        assert cfg.root_dir == "workspace"
        assert cfg.virtual_mode is False

    def test_custom_root_dir(self) -> None:
        cfg = FilesystemBackendConfig(root_dir="/data")
        assert cfg.root_dir == "/data"

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            FilesystemBackendConfig(bogus="x")  # type: ignore[call-arg]


class TestLocalShellBackend:
    def test_defaults(self) -> None:
        cfg = LocalShellBackendConfig()
        assert cfg.type == "local_shell"
        assert cfg.root_dir == "workspace"
        assert cfg.virtual_mode is False

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            LocalShellBackendConfig(shell="/bin/zsh")  # type: ignore[call-arg]


class TestStoreBackend:
    def test_defaults(self) -> None:
        cfg = StoreBackendConfig()
        assert cfg.type == "store"
        assert cfg.namespace == []

    def test_custom_namespace(self) -> None:
        cfg = StoreBackendConfig(namespace=["a", "b"])
        assert cfg.namespace == ["a", "b"]


class TestCompositeBackend:
    def test_basic(self) -> None:
        cfg = CompositeBackendConfig(
            default=FilesystemBackendConfig(),
            routes={"docs": StateBackendConfig()},
        )
        assert cfg.type == "composite"
        assert isinstance(cfg.default, FilesystemBackendConfig)
        assert isinstance(cfg.routes["docs"], StateBackendConfig)

    def test_nested_composite(self) -> None:
        inner = CompositeBackendConfig(default=StateBackendConfig())
        outer = CompositeBackendConfig(default=inner)
        assert isinstance(outer.default, CompositeBackendConfig)


class TestLionclawDockerBackend:
    def test_defaults(self) -> None:
        cfg = OlanoDockerBackendConfig()
        assert cfg.type == "lionclaw_docker"
        assert cfg.persistent is False
        assert cfg.image == "olano-sandbox:latest"
        assert cfg.network == "none"

    def test_network_validation(self) -> None:
        cfg = OlanoDockerBackendConfig(network="bridge")
        assert cfg.network == "bridge"

    def test_invalid_network_raises(self) -> None:
        with pytest.raises(ValidationError):
            OlanoDockerBackendConfig(network="macvlan")  # type: ignore[call-arg]

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            OlanoDockerBackendConfig(snapshot="my-image")  # type: ignore[call-arg]

    def test_resource_limits_default_to_undecided(self) -> None:
        """None means "nobody decided here" — the builder resolves the real
        value, and the backend keeps the historical caps if nothing did."""
        cfg = OlanoDockerBackendConfig()
        assert cfg.memory is None
        assert cfg.cpus is None

    def test_resource_limits_are_normalized(self) -> None:
        cfg = OlanoDockerBackendConfig(memory=" 2G ", cpus=2)
        assert cfg.memory == "2g"
        assert cfg.cpus == 2.0

    @pytest.mark.parametrize("bad", ["lots", "2gb", "", "5m"])
    def test_invalid_memory_raises(self, bad: str) -> None:
        """Raise rather than coerce: a typo in an agent's own config should fail
        its boot, not silently run it on a limit nobody asked for. '5m' is below
        docker's own 6 MiB floor, so accepting it would only move the failure to
        `docker run`."""
        with pytest.raises(ValidationError):
            OlanoDockerBackendConfig(memory=bad)

    @pytest.mark.parametrize("bad", [0, 0.05, 128, "many"])
    def test_invalid_cpus_raises(self, bad: object) -> None:
        with pytest.raises(ValidationError):
            OlanoDockerBackendConfig(cpus=bad)  # type: ignore[arg-type]


class TestRemoteBackends:
    def test_daytona_defaults(self) -> None:
        cfg = DaytonaBackendConfig()
        assert cfg.type == "daytona"

    def test_modal_defaults(self) -> None:
        cfg = ModalBackendConfig()
        assert cfg.type == "modal"

    def test_runloop_defaults(self) -> None:
        cfg = RunloopBackendConfig()
        assert cfg.type == "runloop"

    def test_agentcore_defaults(self) -> None:
        cfg = AgentCoreBackendConfig()
        assert cfg.type == "agentcore"

    def test_langsmith_defaults(self) -> None:
        cfg = LangSmithBackendConfig()
        assert cfg.type == "langsmith"


class TestHelpers:
    def test_is_sandbox_local_docker(self) -> None:
        assert is_sandbox_backend(OlanoDockerBackendConfig())

    def test_is_sandbox_daytona(self) -> None:
        assert is_sandbox_backend(DaytonaBackendConfig())

    def test_is_not_sandbox_filesystem(self) -> None:
        assert not is_sandbox_backend(FilesystemBackendConfig())

    def test_is_not_sandbox_local_shell(self) -> None:
        assert not is_sandbox_backend(LocalShellBackendConfig())

    def test_is_remote_daytona(self) -> None:
        assert is_remote_sandbox_backend(DaytonaBackendConfig())

    def test_is_not_remote_local_docker(self) -> None:
        assert not is_remote_sandbox_backend(OlanoDockerBackendConfig())

    def test_default_backend_is_local_shell(self) -> None:
        cfg = default_backend()
        assert isinstance(cfg, LocalShellBackendConfig)


class TestRootDirSentinel:
    def test_workspace_sentinel_preserved_at_load_time(self) -> None:
        cfg = FilesystemBackendConfig(root_dir="workspace")
        assert cfg.root_dir == "workspace"

    def test_absolute_path_preserved(self) -> None:
        cfg = FilesystemBackendConfig(root_dir="/home/user/project")
        assert cfg.root_dir == "/home/user/project"


class TestAgentConfigBackendParsing:
    def test_structured_filesystem_accepted(self) -> None:
        from olano.config.agent_config import AgentConfig

        config = AgentConfig(
            name="test",
            model="anthropic:claude-haiku-4-5-20251001",
            backend={"type": "filesystem", "root_dir": "workspace", "virtual_mode": False},
        )
        assert isinstance(config.backend, FilesystemBackendConfig)

    def test_structured_local_shell_accepted(self) -> None:
        from olano.config.agent_config import AgentConfig

        config = AgentConfig(
            name="test",
            model="anthropic:claude-haiku-4-5-20251001",
            backend={"type": "local_shell", "root_dir": "workspace"},
        )
        assert isinstance(config.backend, LocalShellBackendConfig)

    def test_legacy_string_backend_raises(self) -> None:
        from olano.config.agent_config import AgentConfig

        with pytest.raises(ValueError, match="old string format"):
            AgentConfig(
                name="test",
                model="anthropic:claude-haiku-4-5-20251001",
                backend="filesystem",
            )

    def test_legacy_filesystem_shell_string_raises(self) -> None:
        from olano.config.agent_config import AgentConfig

        with pytest.raises(ValueError, match="old string format"):
            AgentConfig(
                name="test",
                model="anthropic:claude-haiku-4-5-20251001",
                backend="filesystem+shell",
            )

    def test_legacy_sandbox_key_raises(self) -> None:
        from pydantic import ValidationError

        from olano.config.agent_config import AgentConfig

        with pytest.raises(ValidationError, match="sandbox"):
            AgentConfig(
                name="test",
                model="anthropic:claude-haiku-4-5-20251001",
                sandbox=True,
            )

    def test_legacy_sandbox_backend_key_raises(self) -> None:
        from pydantic import ValidationError

        from olano.config.agent_config import AgentConfig

        with pytest.raises(ValidationError, match="sandbox_backend"):
            AgentConfig(
                name="test",
                model="anthropic:claude-haiku-4-5-20251001",
                sandbox_backend="docker",
            )

    def test_legacy_sandbox_mode_in_security_raises(self) -> None:
        from pydantic import ValidationError

        from olano.config.agent_config import SecurityConfig

        with pytest.raises(ValidationError, match="sandbox_mode"):
            SecurityConfig(sandbox_mode="docker")

    def test_legacy_shell_use_docker_in_security_raises(self) -> None:
        from pydantic import ValidationError

        from olano.config.agent_config import SecurityConfig

        with pytest.raises(ValidationError, match="shell_use_docker"):
            SecurityConfig(shell_use_docker=True)

    def test_lionclaw_docker_unknown_key_raises(self) -> None:
        from olano.config.agent_config import AgentConfig

        with pytest.raises((ValueError, Exception)):
            AgentConfig(
                name="test",
                model="anthropic:claude-haiku-4-5-20251001",
                backend={"type": "lionclaw_docker", "snapshot": "my-image"},
            )


class TestPerAgentSandboxBlock:
    """``sandbox:`` was a removed key and is now a block of resource ceilings.

    Only the old scalar spellings are rejected, and by name, so an ancient
    config gets a migration hint rather than a bare type error.
    """

    def test_resource_block_parses(self) -> None:
        from olano.config.agent_config import AgentConfig

        cfg = AgentConfig(name="test", sandbox={"memory": "4G", "cpus": 2})
        assert cfg.sandbox is not None
        assert cfg.sandbox.memory == "4g"
        assert cfg.sandbox.cpus == 2.0

    def test_one_field_alone_leaves_the_other_inheriting(self) -> None:
        from olano.config.agent_config import AgentConfig

        cfg = AgentConfig(name="test", sandbox={"memory": "4g"})
        assert cfg.sandbox is not None
        assert cfg.sandbox.cpus is None

    def test_omitted_and_explicit_null_both_mean_inherit(self) -> None:
        """save_agent_config writes explicit nulls, so both spellings reach the
        loader on a perfectly ordinary round-trip."""
        from olano.config.agent_config import AgentConfig

        assert AgentConfig(name="test").sandbox is None
        assert AgentConfig(name="test", sandbox=None).sandbox is None

    @pytest.mark.parametrize("legacy", [True, False, "docker"])
    def test_legacy_scalar_spelling_raises_with_a_migration_hint(self, legacy: object) -> None:
        from olano.config.agent_config import AgentConfig

        with pytest.raises(ValidationError, match="old on/off spelling"):
            AgentConfig(name="test", sandbox=legacy)  # type: ignore[arg-type]

    def test_sandbox_backend_stays_rejected(self) -> None:
        from olano.config.agent_config import AgentConfig

        with pytest.raises(ValidationError, match="sandbox_backend"):
            AgentConfig(name="test", sandbox_backend="docker")  # type: ignore[call-arg]

    def test_invalid_limits_fail_the_agents_boot(self) -> None:
        from olano.config.agent_config import AgentConfig

        with pytest.raises(ValidationError):
            AgentConfig(name="test", sandbox={"memory": "enormous"})
        with pytest.raises(ValidationError):
            AgentConfig(name="test", sandbox={"cpus": 999})
