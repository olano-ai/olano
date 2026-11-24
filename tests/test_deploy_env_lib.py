"""Tests for ``deploy/env-lib.sh`` — the sticky ``.env`` pins the deploy scripts
apply: ``ensure_relay_wiring`` (central-relay MCP OAuth wiring, one registered
redirect URI ``https://auth.olano.ai/mcp/callback`` for the fleet) and
``ensure_home_dir_name`` (the data-dir basename: ``.olano`` on fresh boxes,
``olano`` where a box was provisioned while the visible name was the default
and that directory is not a source checkout, ``.lionclaw`` kept wherever the
original data exists — existing boxes are never migrated).

The critical invariant is **atomicity**: ``LIONCLAW_MCP_RELAY_URL`` is written only
when a secret is also present, because the runtime's ``resolve_relay_config``
raises when the URL is set without a secret — which breaks *all* MCP OAuth, not
just the pre-registration providers (HubSpot) the relay exists for. These tests
drive the bash function directly with an isolated environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ENV_LIB = Path(__file__).resolve().parents[1] / "deploy" / "env-lib.sh"
RELAY_KEYS = ("LIONCLAW_MCP_RELAY_URL", "LIONCLAW_MCP_RELAY_SECRET", "LIONCLAW_INSTANCE_ID")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _wire(env_file: Path, env: dict[str, str], runs: int = 1) -> int:
    """Source env-lib.sh and call ensure_relay_wiring `runs` times; return last rc.

    The subprocess env is built fresh (only PATH + the caller's keys) so ambient
    LIONCLAW_MCP_* vars from the test runner can never leak into the function.
    """
    body = (
        f'. "{ENV_LIB}"\nrc=0\n'
        + "".join(f'ensure_relay_wiring "{env_file}"; rc=$?\n' for _ in range(runs))
        + "exit $rc\n"
    )
    proc = subprocess.run(
        ["bash", "-c", body],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
    )
    return proc.returncode


def _values(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in env_file.read_text().splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, _, val = raw.partition("=")
            out[key.strip()] = val.strip()
    return out


def test_no_secret_writes_nothing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    rc = _wire(env_file, {})  # no relay secret anywhere
    assert rc == 1  # unwired → caller falls back to ensure_public_url (origin mode)
    assert not any(k in _values(env_file) for k in RELAY_KEYS)


def test_secret_writes_url_and_secret_together(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    rc = _wire(env_file, {"LIONCLAW_MCP_RELAY_SECRET": "sek"})
    assert rc == 0
    got = _values(env_file)
    # The core invariant: URL is present, and never present without the secret.
    assert got["LIONCLAW_MCP_RELAY_URL"] == "https://auth.olano.ai"
    assert got["LIONCLAW_MCP_RELAY_SECRET"] == "sek"


def test_idempotent(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    _wire(env_file, {"LIONCLAW_MCP_RELAY_SECRET": "sek"}, runs=3)
    text = env_file.read_text()
    assert text.count("LIONCLAW_MCP_RELAY_URL=") == 1
    assert text.count("LIONCLAW_MCP_RELAY_SECRET=") == 1


def test_secret_already_in_file_adds_url_without_duplicating(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LIONCLAW_MCP_RELAY_SECRET=already\n")
    rc = _wire(env_file, {})  # secret comes from the file, not the environment
    assert rc == 0
    got = _values(env_file)
    assert got["LIONCLAW_MCP_RELAY_URL"] == "https://auth.olano.ai"
    assert got["LIONCLAW_MCP_RELAY_SECRET"] == "already"  # untouched
    assert env_file.read_text().count("LIONCLAW_MCP_RELAY_SECRET=") == 1


def _pin_home_dir(
    env_file: Path, env: dict[str, str], runs: int = 1, with_host_home: bool = False
) -> str:
    """Source env-lib.sh and call ensure_home_dir_name `runs` times.

    Returns the exported OLANO_HOME_DIR from the last run (via stdout marker).
    with_host_home=True runs ensure_host_home first, as bootstrap does.
    """
    prelude = f'ensure_host_home "{env_file}"\n' if with_host_home else ""
    body = (
        f'. "{ENV_LIB}"\n'
        + prelude
        + "".join(f'ensure_home_dir_name "{env_file}"\n' for _ in range(runs))
        + 'printf "RESOLVED=%s\\n" "$OLANO_HOME_DIR"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", body],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESOLVED="):
            return line.partition("=")[2]
    return ""


def test_home_dir_name_fresh_box_pins_dot_olano(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    resolved = _pin_home_dir(env_file, {"HOME": str(tmp_path)}, runs=3)
    assert resolved == ".olano"
    assert _values(env_file)["OLANO_HOME_DIR"] == ".olano"
    # Idempotent across re-runs: exactly one line, never duplicated.
    assert env_file.read_text().count("OLANO_HOME_DIR=") == 1


def test_home_dir_name_legacy_data_pins_lionclaw(tmp_path):
    (tmp_path / ".lionclaw").mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("")
    resolved = _pin_home_dir(env_file, {"HOME": str(tmp_path)})
    assert resolved == ".lionclaw"
    assert _values(env_file)["OLANO_HOME_DIR"] == ".lionclaw"


def test_home_dir_name_existing_dot_olano_pins_dot_olano(tmp_path):
    (tmp_path / ".olano").mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("")
    resolved = _pin_home_dir(env_file, {"HOME": str(tmp_path)})
    assert resolved == ".olano"


def test_home_dir_name_existing_visible_dir_pins_olano(tmp_path):
    """A plain ~/olano (no checkout markers) is the data dir, as the runtime
    resolves it: a box provisioned while the visible name was the default."""
    (tmp_path / "olano").mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("")
    assert _pin_home_dir(env_file, {"HOME": str(tmp_path)}) == "olano"


def test_home_dir_name_dot_olano_beats_the_visible_dir(tmp_path):
    (tmp_path / ".olano").mkdir()
    (tmp_path / "olano").mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("")
    assert _pin_home_dir(env_file, {"HOME": str(tmp_path)}) == ".olano"


@pytest.mark.parametrize("marker", ["pyproject.toml", ".git"])
def test_home_dir_name_checkout_at_olano_pins_dot_olano(tmp_path, marker):
    """The manual deploys put this repository at ~/olano: it is never the data
    dir, so a fresh box beside it gets the default."""
    checkout = tmp_path / "olano"
    checkout.mkdir()
    if marker == ".git":
        (checkout / marker).mkdir()
    else:
        (checkout / marker).write_text("")
    env_file = tmp_path / ".env"
    env_file.write_text("")
    resolved = _pin_home_dir(env_file, {"HOME": str(tmp_path)})
    assert resolved == ".olano"
    assert _values(env_file)["OLANO_HOME_DIR"] == ".olano"


def test_home_dir_name_legacy_data_beats_the_checkout_fallback(tmp_path):
    """A checkout decides where NEW data goes, never which existing data is served."""
    (tmp_path / "olano").mkdir()
    (tmp_path / "olano" / "pyproject.toml").write_text("")
    (tmp_path / ".lionclaw").mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("")
    assert _pin_home_dir(env_file, {"HOME": str(tmp_path)}) == ".lionclaw"


def test_home_dir_name_existing_pin_kept(tmp_path):
    # A pinned value always wins — even when no legacy dir backs it up.
    env_file = tmp_path / ".env"
    env_file.write_text("OLANO_HOME_DIR=.lionclaw\n")
    resolved = _pin_home_dir(env_file, {"HOME": str(tmp_path)})
    assert resolved == ".lionclaw"
    assert env_file.read_text().count("OLANO_HOME_DIR=") == 1


def test_home_dir_name_probes_pinned_host_home_not_shell_home(tmp_path):
    # The legacy probe must key on the LIONCLAW_HOST_HOME pin, not $HOME:
    # a legacy box's data can live under /root while bootstrap runs as the
    # service user (ensure_host_home pins /root in that case).
    data_home = tmp_path / "data-home"
    (data_home / ".lionclaw").mkdir(parents=True)
    shell_home = tmp_path / "shell-home"
    shell_home.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(f"LIONCLAW_HOST_HOME={data_home}\n")
    resolved = _pin_home_dir(env_file, {"HOME": str(shell_home)}, with_host_home=True)
    assert resolved == ".lionclaw"


def test_custom_url_and_instance_id_preserved(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    _wire(
        env_file,
        {
            "LIONCLAW_MCP_RELAY_SECRET": "sek",
            "LIONCLAW_MCP_RELAY_URL": "https://relay.example.com",
            "LIONCLAW_INSTANCE_ID": "box-7",
        },
    )
    got = _values(env_file)
    assert got["LIONCLAW_MCP_RELAY_URL"] == "https://relay.example.com"
    assert got["LIONCLAW_INSTANCE_ID"] == "box-7"


# ── ensure_pg_name: the sidecar's role, database and volume stem ─────────────
# Postgres applies the names only when it initialises the data dir, so the pin
# must say what an existing data dir was created with and may say `olano` only
# on a fresh box. The function asks docker for the project-prefixed volumes; a
# fake `docker` on PATH answers with whatever the test puts in FAKE_VOLUMES, so
# the real daemon (which may hold volumes from other work) never takes part.


def _pin_pg_name(env_file: Path, env: dict[str, str], *, volumes: str = "", runs: int = 1) -> str:
    shim_dir = env_file.parent / "fake-bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "docker"
    shim.write_text("#!/bin/sh\nprintf '%s\\n' $FAKE_VOLUMES\n")
    shim.chmod(0o755)
    body = (
        f'. "{ENV_LIB}"\n'
        + "".join(f'ensure_pg_name "{env_file}"\n' for _ in range(runs))
        + 'printf "RESOLVED=%s\\n" "$OLANO_PG_NAME"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", body],
        env={
            "PATH": f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE_VOLUMES": volumes,
            **env,
        },
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESOLVED="):
            return line.partition("=")[2]
    return ""


def test_pg_name_fresh_box_pins_olano(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    assert _pin_pg_name(env_file, {}, runs=3) == "olano"
    assert _values(env_file)["OLANO_PG_NAME"] == "olano"
    assert env_file.read_text().count("OLANO_PG_NAME=") == 1


def test_pg_name_existing_pin_kept(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OLANO_PG_NAME=lionclaw\n")
    assert _pin_pg_name(env_file, {}, volumes="proj_olano_pgdata") == "lionclaw"
    assert env_file.read_text().count("OLANO_PG_NAME=") == 1


@pytest.mark.parametrize(
    ("volumes", "expected"),
    [
        ("proj_lionclaw_pgdata", "lionclaw"),
        ("proj_olano_pgdata", "olano"),
        ("proj_olano_pgdata proj_lionclaw_pgdata", "lionclaw"),  # the older one has the history
        ("proj_caddy_data unrelated", "olano"),
    ],
)
def test_pg_name_follows_the_volume_that_already_exists(tmp_path, volumes, expected):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    assert _pin_pg_name(env_file, {}, volumes=volumes) == expected


def test_pg_name_bind_mounted_data_dir_reads_as_lionclaw_only_once_initialised(tmp_path):
    """A sealed box: LIONCLAW_PGDATA is a path. Before Postgres initialised it
    the box is fresh (olano); afterwards the dir cannot say which role it
    carries, and every such dir created before this pin existed is lionclaw."""
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(f"LIONCLAW_PGDATA={pgdata}\n")
    assert _pin_pg_name(env_file, {}) == "olano"
    env_file.write_text(f"LIONCLAW_PGDATA={pgdata}\n")
    (pgdata / "PG_VERSION").write_text("16\n")
    assert _pin_pg_name(env_file, {}) == "lionclaw"


def test_bootstrap_pins_the_name_right_after_the_password():
    text = (ENV_LIB.parent / "bootstrap-allinone.sh").read_text()
    assert text.index("ensure_pg_password .env") < text.index("ensure_pg_name .env")


# ── print_login_hint: the bootstrap banner's sign-in lines ──────────────────
# The banner lands in the install log, and an Olano-managed box ships that
# log's tail to the console, where the customer reads it. So it never prints a
# password: the configured one is named by where it is kept, and the one-time
# password quick-setup generates (when .env configures none) by the note it
# leaves on the box.

BOOTSTRAP = ENV_LIB.parent / "bootstrap-allinone.sh"


def _login_hint(env_file: Path) -> subprocess.CompletedProcess[str]:
    # The bootstrap runs under `set -euo pipefail`; so does the helper here.
    body = f'set -euo pipefail\n. "{ENV_LIB}"\nprint_login_hint "{env_file}"\n'
    return subprocess.run(
        ["bash", "-c", body],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
    )


def test_login_hint_never_prints_a_configured_password(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIONCLAW_ADMIN_EMAIL=owner@example.com\nLIONCLAW_ADMIN_PASSWORD=Tr0ub4dor-per-box\n"
    )
    proc = _login_hint(env_file)
    assert proc.returncode == 0, proc.stderr
    assert "Sign in as owner@example.com" in proc.stdout
    assert "Security tab" in proc.stdout
    for leaked in ("Tr0ub4dor-per-box", "88888888", "admin@example.com"):
        assert leaked not in proc.stdout


def test_login_hint_strips_quotes_around_the_email(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('LIONCLAW_ADMIN_EMAIL="owner@example.com"\nLIONCLAW_ADMIN_PASSWORD=x\n')
    assert "Sign in as owner@example.com with" in _login_hint(env_file).stdout


@pytest.mark.parametrize(
    "body", ["", "LIONCLAW_ADMIN_PASSWORD=\n", "LIONCLAW_ADMIN_EMAIL=owner@example.com\n"]
)
def test_login_hint_points_at_the_generated_password_when_none_is_configured(tmp_path, body):
    env_file = tmp_path / ".env"
    env_file.write_text(body)
    proc = _login_hint(env_file)
    assert proc.returncode == 0, proc.stderr
    assert "one-time password" in proc.stdout
    assert "run/initial-admin-password" in proc.stdout
    assert "88888888" not in proc.stdout
    assert "Sign in as" not in proc.stdout
    expected_email = "owner@example.com" if "ADMIN_EMAIL" in body else "admin@example.com"
    assert f"Admin login: {expected_email}" in proc.stdout
    assert f"reset-password --email {expected_email}" in proc.stdout


def test_login_hint_names_the_note_inside_the_pinned_home(tmp_path):
    """The note lives in the olano home the compose file mounts, which .env
    pins through LIONCLAW_HOST_HOME + OLANO_HOME_DIR."""
    env_file = tmp_path / ".env"
    env_file.write_text('LIONCLAW_HOST_HOME=/home/olano\nOLANO_HOME_DIR=".olano"\n')
    assert "sudo cat /home/olano/.olano/run/initial-admin-password" in _login_hint(env_file).stdout
    env_file.write_text("LIONCLAW_HOST_HOME=/home/olano\nOLANO_HOME_DIR=olano\n")
    assert "sudo cat /home/olano/olano/run/initial-admin-password" in _login_hint(env_file).stdout


def test_login_hint_survives_a_missing_env_file(tmp_path):
    proc = _login_hint(tmp_path / "absent.env")
    assert proc.returncode == 0, proc.stderr
    assert "one-time password" in proc.stdout
    # No .env and no HOME in the helper's environment: the compose defaults.
    assert "/root/.lionclaw/run/initial-admin-password" in proc.stdout


@pytest.mark.parametrize("script", ["bootstrap-allinone.sh", "bootstrap-remote.sh"])
def test_bootstrap_prints_credentials_only_through_the_helper(script):
    """No bootstrap names a password in a line it executes, or anywhere else:
    every sign-in line goes through print_login_hint."""
    text = (ENV_LIB.parent / script).read_text()
    assert "print_login_hint .env" in text
    assert "88888888" not in text
