"""The hidden-agent policy: enumeration is safe by default.

Three layers keep internal test fixtures (and any deliberately hidden agent)
out of customer-facing surfaces:

1. Shipped artifacts exclude the repo-local ``agents/`` dir (.dockerignore,
   the fleet tooling's ship.sh) — not testable here.
2. ``loader._search_paths("auto")`` drops the project-local dir on any box with
   managed-provisioning evidence, so even a stale image's ``/app/agents``
   cannot resurface fixtures.
3. ``loader.list_agents()`` excludes ``hidden: true`` agents unless a caller
   passes ``include_hidden=True`` — and the set of opt-in callers is frozen by
   the allowlist test below, so a future enumeration surface is safe by
   construction and a new opt-in is a deliberate, reviewed change.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from olano.config import loader
from olano.config.home import get_olano_paths

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_agent(base: Path, name: str, *, hidden: object = None, raw: str | None = None) -> Path:
    agent_dir = base / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    if raw is None:
        data: dict = {"name": name, "model": "fake:instant"}
        if hidden is not None:
            data["hidden"] = hidden
        raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    return path


@pytest.fixture
def home_agents(lionclaw_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Empty CWD so the repo's own agents/ fixtures stay out of the picture.
    monkeypatch.chdir(tmp_path)
    return get_olano_paths().agents_dir


# ── list_agents() hidden filtering ───────────────────────────────────────────


def test_hidden_agents_excluded_by_default(home_agents: Path) -> None:
    _write_agent(home_agents, "visible")
    _write_agent(home_agents, "fixture", hidden=True)
    assert loader.list_agents() == ["visible"]
    assert loader.list_agents(include_hidden=True) == ["fixture", "visible"]


def test_only_a_literal_true_hides(home_agents: Path) -> None:
    """Identity-test semantics: anything but a parseable ``hidden: true`` is visible."""
    _write_agent(home_agents, "a", hidden=False)
    _write_agent(home_agents, "b", raw='{"name": "b", "hidden": "true"}')
    _write_agent(home_agents, "c", raw='{"name": "c", "hidden": 1}')
    assert loader.list_agents() == ["a", "b", "c"]


def test_unparseable_config_stays_visible(home_agents: Path) -> None:
    """A broken config must never make a real agent vanish from every listing."""
    _write_agent(home_agents, "broken", raw="{this is not json5")
    assert "broken" in loader.list_agents()


def test_editing_the_config_invalidates_the_hidden_memo(home_agents: Path) -> None:
    path = _write_agent(home_agents, "agent", hidden=True)
    assert loader.list_agents() == []
    _write_agent(home_agents, "agent", hidden=False)
    # Force a distinct mtime in case both writes land in one timestamp tick.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert loader.list_agents() == ["agent"]


def test_local_file_decides_hidden_over_home(home_agents: Path, tmp_path: Path) -> None:
    """Precedence: the file that would actually load decides the hidden flag."""
    _write_agent(tmp_path / "agents", "dup", hidden=True)  # local, wins
    _write_agent(home_agents, "dup", hidden=False)
    assert "dup" not in loader.list_agents()
    assert "dup" in loader.list_agents(source="home")


# ── the managed-box gate for the project-local agents/ dir ───────────────────


class TestLocalAgentsDirGate:
    @pytest.fixture(autouse=True)
    def _clean_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LIONCLAW_DEPLOYMENT_MANAGED", raising=False)
        monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
        monkeypatch.delenv("LIONCLAW_DISABLE_LOCAL_AGENTS_DIR", raising=False)
        monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
        monkeypatch.setattr(loader, "_local_agents_dir_memo", None)

    @staticmethod
    def _local() -> Path:
        return loader._LOCAL_AGENTS_DIR.resolve()

    def test_unmanaged_box_keeps_local_dir(self) -> None:
        assert loader._search_paths("auto")[0] == self._local()

    def test_managed_flag_drops_local_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_MANAGED", "1")
        assert self._local() not in loader._search_paths("auto")

    def test_deployment_token_drops_local_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_x")
        assert self._local() not in loader._search_paths("auto")

    def test_managed_manifest_drops_local_dir(self, tmp_path: Path) -> None:
        (tmp_path / "installation.json").write_text(
            json.dumps({"managed": True, "mode": "cloud", "deployment_id": "dep-1"}),
            encoding="utf-8",
        )
        assert self._local() not in loader._search_paths("auto")

    def test_broken_manifest_fails_closed(self, tmp_path: Path) -> None:
        (tmp_path / "installation.json").write_text("{not json", encoding="utf-8")
        assert self._local() not in loader._search_paths("auto")

    def test_override_keeps_local_dir_despite_managed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_MANAGED", "1")
        monkeypatch.setenv("LIONCLAW_DISABLE_LOCAL_AGENTS_DIR", "0")
        assert loader._search_paths("auto")[0] == self._local()

    def test_override_drops_local_dir_on_a_dev_box(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIONCLAW_DISABLE_LOCAL_AGENTS_DIR", "1")
        assert self._local() not in loader._search_paths("auto")

    def test_explicit_local_source_always_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_MANAGED", "1")
        assert loader._search_paths("local") == [self._local()]

    def test_home_agents_survive_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guardrail: real home-dir agents are untouched on a managed box —
        only the image-resident local dir goes dark."""
        monkeypatch.setenv("LIONCLAW_DEPLOYMENT_MANAGED", "1")
        _write_agent(tmp_path / "agents", "test-agent-fake", hidden=True)  # stale image
        home = get_olano_paths().agents_dir
        _write_agent(home, "agentfather")
        assert loader.list_agents() == ["agentfather"]
        assert loader.find_agent_config_path("agentfather") is not None
        assert loader.find_agent_config_path("test-agent-fake") is None


# ── the frozen opt-in allowlist ──────────────────────────────────────────────

# Every include_hidden=True call site, by file. These are the surfaces that
# genuinely need the complete on-disk set: the boot loop (its own filter point,
# with LIONCLAW_BOOT_HIDDEN_AGENTS), plan-limit counting (own fail-closed
# skip), the admin listing that *shows* the hidden flag, the operator CLI
# table, maintenance sweeps, an install-existence check, issue-principal
# resolution, and the collision set a new handle is minted against (a hidden
# agent's folder is exactly what a fresh handle must not land on). Adding an
# entry here means you have checked the new caller never
# feeds a customer-facing listing — that is the review this test forces.
_INCLUDE_HIDDEN_ALLOWLIST = Counter(
    {
        # Boot, plus the checkpoint sweep's live-agent set: a hidden agent's
        # threads are not orphans, and the report never reaches a listing.
        "olano/runtime/server.py": 2,
        "olano/deployment/enforcement.py": 1,
        "olano/admin/agents.py": 1,
        "olano/config/agent_names.py": 1,
        "olano/cli/main.py": 2,
        "olano/cli/quick_setup.py": 1,
        "olano/update/refresh.py": 1,
        "olano/issues/principals.py": 1,
    }
)


def test_include_hidden_opt_ins_are_frozen() -> None:
    found: Counter[str] = Counter()
    for path in (REPO_ROOT / "olano").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "olano/config/loader.py":  # the definition itself
            continue
        if rel.startswith("olano/dashboard_2.0/"):
            continue
        count = path.read_text(encoding="utf-8").count("include_hidden=True")
        if count:
            found[rel] = count
    assert found == _INCLUDE_HIDDEN_ALLOWLIST, (
        "include_hidden=True call sites changed. If the new caller can feed a "
        "customer-facing listing, use the default instead; otherwise update "
        "_INCLUDE_HIDDEN_ALLOWLIST deliberately."
    )
