"""``OLANO_*`` / ``LIONCLAW_*`` environment aliases (``olano/env_aliases.py``)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

from olano import env_aliases
from olano.env_aliases import EXCLUDED_SUFFIXES, apply_env_aliases, spellings

LOGGER = "olano.env_aliases"
# Every variable these tests own carries this mark, so assertions can filter
# by it: ``apply_env_aliases`` also mirrors whatever ``LIONCLAW_*`` the conftest
# fixtures set for the test (``LIONCLAW_SEAL_STATE``, ``LIONCLAW_CHECKPOINT_BACKEND``,
# ...), and which of those still lack a mirror depends on session history.
MARK = "ALIAS_T"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    """Fresh module memory, and the whole environment put back afterwards.

    monkeypatch only undoes what it set itself; the mirrors the function writes
    during a test (its own copies of the fixtures' variables included) need a
    plain snapshot to be removed again.
    """
    monkeypatch.setattr(env_aliases, "_written", {})
    monkeypatch.setattr(env_aliases, "_reported", set())
    before = dict(os.environ)
    yield
    for key in set(os.environ) - set(before):
        del os.environ[key]
    for key, value in before.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


def _absent(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


def _own(mapping: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in mapping.items() if MARK in k}


def _own_lines(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if name in r.getMessage()]


def test_olano_is_mirrored_onto_lionclaw(monkeypatch):
    _absent(monkeypatch, "OLANO_ALIAS_T1", "LIONCLAW_ALIAS_T1")
    monkeypatch.setenv("OLANO_ALIAS_T1", "from-new")

    written = apply_env_aliases()

    assert os.environ["LIONCLAW_ALIAS_T1"] == "from-new"
    assert _own(written) == {"LIONCLAW_ALIAS_T1": "from-new"}


def test_lionclaw_is_mirrored_onto_olano(monkeypatch):
    _absent(monkeypatch, "OLANO_ALIAS_T2", "LIONCLAW_ALIAS_T2")
    monkeypatch.setenv("LIONCLAW_ALIAS_T2", "from-legacy")

    written = apply_env_aliases()

    assert os.environ["OLANO_ALIAS_T2"] == "from-legacy"
    assert _own(written) == {"OLANO_ALIAS_T2": "from-legacy"}


def test_conflict_leaves_both_alone_and_legacy_wins(monkeypatch, caplog):
    _absent(monkeypatch, "OLANO_ALIAS_T3", "LIONCLAW_ALIAS_T3")
    monkeypatch.setenv("LIONCLAW_ALIAS_T3", "legacy")
    monkeypatch.setenv("OLANO_ALIAS_T3", "new")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        written = apply_env_aliases()
        apply_env_aliases()  # the debug line fires once per name, not per pass

    assert os.environ["LIONCLAW_ALIAS_T3"] == "legacy"
    assert os.environ["OLANO_ALIAS_T3"] == "new"
    assert _own(written) == {}
    lines = _own_lines(caplog, "ALIAS_T3")
    assert len(lines) == 1
    assert "LIONCLAW_ALIAS_T3 wins" in lines[0].getMessage()
    assert lines[0].levelno == logging.DEBUG


def test_same_value_on_both_is_not_a_conflict(monkeypatch, caplog):
    _absent(monkeypatch, "OLANO_ALIAS_T4", "LIONCLAW_ALIAS_T4")
    monkeypatch.setenv("LIONCLAW_ALIAS_T4", "same")
    monkeypatch.setenv("OLANO_ALIAS_T4", "same")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        written = apply_env_aliases()

    assert _own(written) == {}
    assert _own_lines(caplog, "ALIAS_T4") == []


def test_non_matching_names_are_untouched(monkeypatch):
    names = (
        "OLANOALIAS_T5",  # no underscore after the prefix
        "MY_OLANO_ALIAS_T5",  # prefix not at the start
        "OLANO_",  # empty suffix
        "OLANO__ALIAS_T5",  # suffix starting with an underscore
        "OLANO_alias_t5",  # lowercase
        "LIONCLAW_ALIAS-T5",  # a character outside [A-Z0-9_]
        "PLAIN_ALIAS_T5",
    )
    _absent(monkeypatch, *names, "LIONCLAW_ALIAS_T5", "OLANO_ALIAS_T5")
    for name in names:
        monkeypatch.setenv(name, "x")
    before = _own(dict(os.environ))

    written = apply_env_aliases()

    assert _own(written) == {}
    assert _own(dict(os.environ)) == before
    assert "LIONCLAW_ALIAS_T5" not in os.environ
    assert "OLANO_ALIAS_T5" not in os.environ


def test_idempotent_on_repeat_calls(monkeypatch):
    _absent(monkeypatch, "OLANO_ALIAS_T6", "LIONCLAW_ALIAS_T6")
    monkeypatch.setenv("OLANO_ALIAS_T6", "v")

    first = apply_env_aliases()
    snapshot = dict(os.environ)
    second = apply_env_aliases()
    third = apply_env_aliases()

    assert _own(first) == {"LIONCLAW_ALIAS_T6": "v"}
    assert second == {} and third == {}  # the first pass left nothing to mirror
    assert dict(os.environ) == snapshot


def test_empty_counts_as_unset(monkeypatch):
    _absent(
        monkeypatch, "OLANO_ALIAS_T7", "LIONCLAW_ALIAS_T7", "OLANO_ALIAS_T8", "LIONCLAW_ALIAS_T8"
    )
    monkeypatch.setenv("LIONCLAW_ALIAS_T7", "")
    monkeypatch.setenv("OLANO_ALIAS_T7", "v")
    monkeypatch.setenv("OLANO_ALIAS_T8", "")

    apply_env_aliases()

    assert os.environ["LIONCLAW_ALIAS_T7"] == "v"  # an empty legacy spelling never shadows a value
    assert "LIONCLAW_ALIAS_T8" not in os.environ  # an empty value is never copied


def test_own_mirror_is_refreshed_when_the_source_changes(monkeypatch, caplog):
    """A later ``.env`` (override=True) rewrite of ``OLANO_X`` must still reach the readers."""
    _absent(monkeypatch, "OLANO_ALIAS_T9", "LIONCLAW_ALIAS_T9")
    monkeypatch.setenv("OLANO_ALIAS_T9", "shell")
    apply_env_aliases()
    assert os.environ["LIONCLAW_ALIAS_T9"] == "shell"

    monkeypatch.setenv("OLANO_ALIAS_T9", "dotenv")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        written = apply_env_aliases()

    assert os.environ["LIONCLAW_ALIAS_T9"] == "dotenv"
    assert _own(written) == {"LIONCLAW_ALIAS_T9": "dotenv"}
    assert _own_lines(caplog, "ALIAS_T9") == []


def test_explicitly_set_value_is_never_mistaken_for_a_mirror(monkeypatch, caplog):
    _absent(monkeypatch, "OLANO_ALIAS_T10", "LIONCLAW_ALIAS_T10")
    monkeypatch.setenv("LIONCLAW_ALIAS_T10", "legacy")
    apply_env_aliases()
    assert os.environ["OLANO_ALIAS_T10"] == "legacy"

    monkeypatch.setenv("OLANO_ALIAS_T10", "explicit")  # somebody set the new spelling on purpose
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        apply_env_aliases()

    assert os.environ["LIONCLAW_ALIAS_T10"] == "legacy"
    assert os.environ["OLANO_ALIAS_T10"] == "explicit"
    assert len(_own_lines(caplog, "ALIAS_T10")) == 1


def test_home_is_excluded(monkeypatch):
    assert "HOME" in EXCLUDED_SUFFIXES
    _absent(monkeypatch, "OLANO_HOME", "LIONCLAW_HOME")
    monkeypatch.setenv("LIONCLAW_HOME", "/srv/legacy-home")

    apply_env_aliases()

    assert "OLANO_HOME" not in os.environ  # config/home.py owns that pair, with its own precedence
    assert spellings("OLANO_HOME") == ("OLANO_HOME",)
    assert spellings("LIONCLAW_HOME") == ("LIONCLAW_HOME",)


def test_deployment_image_pins_are_not_mirrored(monkeypatch):
    """A managed box's .env carries OLANO_SANDBOX_IMAGE (the update agent pins a
    registry tag there so a later `docker compose up` keeps the same images).
    The runtime reads LIONCLAW_SANDBOX_IMAGE for something else: the image it
    spawns per agent, which defaults to the local olano-sandbox:latest tag.
    Mirroring the pin made every update recreate every sandbox under a new
    image name, and boot agents without a shell while that private image was
    not yet pulled. The pin stays on the deploy side."""
    from olano.config.backend_config import _default_sandbox_image

    for suffix in ("BACKEND_IMAGE", "DASHBOARD_IMAGE", "SANDBOX_IMAGE", "HOME_DIR", "PULL_IMAGES"):
        assert suffix in EXCLUDED_SUFFIXES
    _absent(monkeypatch, "OLANO_SANDBOX_IMAGE", "LIONCLAW_SANDBOX_IMAGE")
    monkeypatch.setenv("OLANO_SANDBOX_IMAGE", "ghcr.io/olano-ai/olano-sandbox:abc123")
    monkeypatch.setenv("OLANO_BACKEND_IMAGE", "ghcr.io/olano-ai/olano-backend:abc123")

    apply_env_aliases()

    assert "LIONCLAW_SANDBOX_IMAGE" not in os.environ
    assert "LIONCLAW_BACKEND_IMAGE" not in os.environ
    assert _default_sandbox_image() == "olano-sandbox:latest"
    assert spellings("OLANO_SANDBOX_IMAGE") == ("OLANO_SANDBOX_IMAGE",)
    # An operator who sets the legacy name on purpose is still honoured.
    monkeypatch.setenv("LIONCLAW_SANDBOX_IMAGE", "ghcr.io/olano-ai/olano-sandbox:pinned")
    assert _default_sandbox_image() == "ghcr.io/olano-ai/olano-sandbox:pinned"


def test_every_deploy_side_name_with_a_legacy_reader_is_excluded():
    """The guard for the next collision: every OLANO_<X> the deploy layer writes
    (compose file, deploy scripts, .env.example) whose LIONCLAW_<X> twin the
    runtime reads must be in EXCLUDED_SUFFIXES, unless the runtime also reads
    OLANO_<X> by name, in which case it is a genuine runtime setting that the
    deploy layer sets deliberately (OLANO_HOME)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    deploy_text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in [*sorted((root / "deploy").rglob("*")), root / ".env.example"]
        if p.is_file() and p.suffix in {".sh", ".yml", ".yaml", ".example", ""}
    )
    deploy_names = set(re.findall(r"\bOLANO_([A-Z0-9][A-Z0-9_]*)", deploy_text))
    assert "SANDBOX_IMAGE" in deploy_names  # the scan reaches the pin that bit

    code_text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((root / "olano").rglob("*.py"))
        if p.name != "env_aliases.py"
    )
    legacy_read = set(re.findall(r"['\"]LIONCLAW_([A-Z0-9][A-Z0-9_]*)['\"]", code_text))
    new_read = set(re.findall(r"['\"]OLANO_([A-Z0-9][A-Z0-9_]*)['\"]", code_text))

    collisions = sorted((deploy_names & legacy_read) - new_read - EXCLUDED_SUFFIXES)
    assert collisions == [], (
        f"OLANO_{collisions[0]} is written by the deploy layer and LIONCLAW_{collisions[0]} "
        "is read by the runtime: add the suffix to env_aliases.EXCLUDED_SUFFIXES "
        "or make the runtime read both spellings on purpose"
    )
    # And HOME is the one case covered by the "reads both" clause.
    assert "HOME" in deploy_names & legacy_read & new_read


def test_spellings():
    assert spellings("LIONCLAW_VAULT_KEY") == ("LIONCLAW_VAULT_KEY", "OLANO_VAULT_KEY")
    assert spellings("OLANO_SETTINGS_SIGNING_KEY") == (
        "LIONCLAW_SETTINGS_SIGNING_KEY",
        "OLANO_SETTINGS_SIGNING_KEY",
    )
    assert spellings("PATH") == ("PATH",)


def test_shell_guard_strips_both_spellings_of_every_infra_secret(monkeypatch):
    """A mirror is a second copy of the secret; the agent shell must see neither."""
    import olano.security.shell_guard as sg

    for name in sg._ALWAYS_STRIP_ENV:
        assert set(spellings(name)) <= sg._ALWAYS_STRIP_ENV, name

    monkeypatch.setattr(
        sg, "_managed_posture", lambda: False
    )  # open posture: only the always-strip set
    monkeypatch.setenv("OLANO_VAULT_KEY", "copy-of-master-key")
    monkeypatch.setenv("OLANO_OLANO_GATEWAY_KEY", "copy-of-gateway-key")
    monkeypatch.setenv("LIONCLAW_SETTINGS_SIGNING_KEY", "copy-of-signing-key")

    env = sg.sanitized_shell_env()

    for name in ("OLANO_VAULT_KEY", "OLANO_OLANO_GATEWAY_KEY", "LIONCLAW_SETTINGS_SIGNING_KEY"):
        assert name not in env, name


def test_import_olano_applies_the_aliases_in_a_fresh_process():
    """The one honest test of an at-import mechanism: a child interpreter."""
    code = "import olano, os; print(os.environ.get('LIONCLAW_ALIAS_SUBPROC'))"
    env = {k: v for k, v in os.environ.items() if k != "LIONCLAW_ALIAS_SUBPROC"}
    env["OLANO_ALIAS_SUBPROC"] = "seen-by-legacy-readers"

    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "seen-by-legacy-readers"


def test_cli_load_env_mirrors_a_dotenv_written_with_the_new_spelling(monkeypatch, tmp_path):
    """``.env`` is loaded after import, so ``_load_env`` has to re-apply the aliases."""
    pytest.importorskip("dotenv")
    from olano.cli.main import _load_env

    _absent(monkeypatch, "OLANO_ALIAS_DOTENV", "LIONCLAW_ALIAS_DOTENV")
    monkeypatch.setenv("LIONCLAW_BOOTSTRAP_ENV", str(tmp_path / "no-bootstrap.env"))
    (tmp_path / ".env").write_text("OLANO_ALIAS_DOTENV=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    _load_env(tmp_path / "home")

    assert os.environ["LIONCLAW_ALIAS_DOTENV"] == "from-dotenv"
