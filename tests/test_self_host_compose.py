"""``deploy/self-host/``: the compose path for a box Olano does not host.

Property under test: the self-hosted stack carries NOTHING that belongs to a
managed deployment. A deployment token without its manifest quarantines the
runtime (``deployment/sources.managed_markers``), the manifest mount is what
puts a box in the managed posture, and the docker socket is root on the host;
none of them may be in this stack, by text and by rendered config. Beside
that, ``setup.sh`` must produce a bootable ``.env`` with no printed default
password, idempotently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SELF_HOST = REPO / "deploy" / "self-host"
COMPOSE = SELF_HOST / "docker-compose.yml"
SETUP = SELF_HOST / "setup.sh"
FILES = (
    "docker-compose.yml",
    "docker-compose.https.yml",
    "docker-compose.sandbox.yml",
    "Caddyfile",
    ".env.example",
    "setup.sh",
    "install.sh",
    "README.md",
)

MANAGED_ONLY = (
    "/etc/olano",
    "/opt/olano",
    "LIONCLAW_DEPLOYMENT_TOKEN",
    "LIONCLAW_DEPLOYMENT_MANAGED",
    "OLANO_DEPLOYMENT_TOKEN",
    "installation.json",
    "olano.env",
    "OLANO_GATEWAY_KEY",
    "caddy.env",
    "olano-sealer",
    "OLANO_GHCR_TOKEN",
)


def _stub_env(tmp_path: Path, *, public_ip: str = "", iface_ip: str = "10.0.0.5") -> dict[str, str]:
    """PATH with stand-ins for the network lookups setup.sh makes.

    ``curl`` answers the what-is-my-ip query with ``public_ip`` (or fails, as
    on a machine with no network), and ``ip`` lists ``iface_ip`` on eth0. The
    suite must never ask a real service for this machine's address.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    curl = stubs / "curl"
    if public_ip:
        curl.write_text(f"#!/bin/sh\necho {public_ip}\n", encoding="utf-8")
    else:
        curl.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    ip = stubs / "ip"
    ip.write_text(
        f"#!/bin/sh\necho '2: eth0    inet {iface_ip}/24 brd 0.0.0.0 scope global eth0'\n",
        encoding="utf-8",
    )
    for stub in (curl, ip):
        stub.chmod(0o755)
    return {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}


def _copy_files(tmp_path: Path) -> Path:
    work = tmp_path / "self-host"
    work.mkdir()
    for name in FILES:
        shutil.copy(SELF_HOST / name, work / name)
    return work


def _setup(work: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(work / "setup.sh"), "--no-start", *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _pins(work: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (work / ".env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }


def _uncommented(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_the_self_host_files_exist() -> None:
    for name in FILES:
        assert (SELF_HOST / name).is_file(), name
    assert os.access(SETUP, os.X_OK)


def test_compose_carries_nothing_managed() -> None:
    """Not even as a comment that could be uncommented into a token line."""
    text = COMPOSE.read_text(encoding="utf-8")
    for needle in ("LIONCLAW_DEPLOYMENT_TOKEN", "LIONCLAW_DEPLOYMENT_MANAGED", "OLANO_GATEWAY_KEY"):
        assert needle not in text, needle
    live = _uncommented(text)
    for needle in MANAGED_ONLY:
        assert needle not in live, needle
    # The docker socket is documented as an opt-in and never mounted by default.
    assert "/var/run/docker.sock" not in live
    # Public images, pulled, never built on the box.
    assert "ghcr.io/olano-ai/olano-backend" in live
    assert "ghcr.io/olano-ai/olano-dashboard" in live
    assert "build:" not in live
    # No printed default password: the stack refuses to start without the pin.
    assert "LIONCLAW_PG_PASSWORD:?" in live
    assert "LIONCLAW_PG_PASSWORD:-" not in live
    assert "LIONCLAW_DASHBOARD_TOKEN=" not in live


def test_caddyfile_keeps_one_origin_and_no_sealer() -> None:
    text = _uncommented((SELF_HOST / "Caddyfile").read_text(encoding="utf-8"))
    assert "{$OLANO_SITE_ADDRESS::80}" in text
    assert "reverse_proxy olano:8765" in text
    assert "/.well-known/*" in text
    assert "try_files {path} /index.html" in text
    assert "sealer" not in text


def test_setup_script_writes_no_managed_marker() -> None:
    text = _uncommented(SETUP.read_text(encoding="utf-8"))
    for needle in (
        "LIONCLAW_DEPLOYMENT_TOKEN",
        "LIONCLAW_DEPLOYMENT_MANAGED",
        "/etc/olano",
        "/opt/olano",
    ):
        assert needle not in text, needle
    assert "OLANO_CREDITS_KEY" in text  # the one credit credential it may write


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_setup_script_pins_a_random_password_once(tmp_path: Path) -> None:
    work = tmp_path / "self-host"
    work.mkdir()
    for name in FILES:
        shutil.copy(SELF_HOST / name, work / name)
    env = _stub_env(tmp_path)
    # No docker on PATH is fine: --no-start never reaches it, and the volume
    # probe (docker volume inspect) must degrade to "no volume".
    for _ in range(2):
        run = subprocess.run(
            ["bash", str(work / "setup.sh"), "--no-start"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert run.returncode == 0, run.stdout + run.stderr
    dotenv = (work / ".env").read_text(encoding="utf-8")
    pins = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in dotenv.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert pins["COMPOSE_PROJECT_NAME"] == "olano"
    assert pins["OLANO_PG_NAME"] == "olano"
    assert len(pins["LIONCLAW_PG_PASSWORD"]) >= 32
    live = _uncommented(dotenv)
    assert live.count("LIONCLAW_PG_PASSWORD=") == 1, "the second run must keep the first pin"
    for needle in ("LIONCLAW_DEPLOYMENT_TOKEN", "LIONCLAW_DEPLOYMENT_MANAGED"):
        assert needle not in dotenv
    assert (work / "data").is_dir()
    assert oct((work / ".env").stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_setup_script_takes_a_host_and_an_olano_key(tmp_path: Path) -> None:
    work = tmp_path / "self-host"
    work.mkdir()
    for name in FILES:
        shutil.copy(SELF_HOST / name, work / name)
    env = _stub_env(tmp_path)
    run = subprocess.run(
        [
            "bash",
            str(work / "setup.sh"),
            "--no-start",
            "--host",
            "olano.example.com",
            "--olano-key",
            "sk-olano-" + "a" * 40,
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    dotenv = (work / ".env").read_text(encoding="utf-8")
    assert "OLANO_SITE_ADDRESS=olano.example.com\n" in dotenv
    # Ports 80 and 443 come from the https override, never from OLANO_HTTP_PORT
    # (which stays the loopback-only plain port).
    assert "OLANO_HTTP_PORT=80\n" not in dotenv
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.https.yml\n" in dotenv
    assert "LIONCLAW_PUBLIC_URL=https://olano.example.com\n" in dotenv
    assert "OLANO_CREDITS_KEY=sk-olano-" + "a" * 40 + "\n" in dotenv
    # A key of the other family is refused: it is never an account key.
    run = subprocess.run(
        ["bash", str(work / "setup.sh"), "--no-start", "--olano-key", "olano_dep_abc"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 2
    assert "olano_dep_abc" not in (work / ".env").read_text(encoding="utf-8")


def _compose_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        return (
            subprocess.run(
                [docker, "compose", "version"], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _compose_available(), reason="docker compose not available")
def test_rendered_compose_mounts_only_the_data_dir(tmp_path: Path) -> None:
    """``docker compose config`` with the generated .env: the runtime sees its
    data directory and nothing of the host's /etc or /opt."""
    import json

    work = tmp_path / "self-host"
    work.mkdir()
    for name in FILES:
        shutil.copy(SELF_HOST / name, work / name)
    env = _stub_env(tmp_path)
    subprocess.run(
        ["bash", str(work / "setup.sh"), "--no-start"],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    run = subprocess.run(
        ["docker", "compose", "-f", str(work / "docker-compose.yml"), "config", "--format", "json"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    cfg = json.loads(run.stdout)
    olano = cfg["services"]["olano"]
    sources = [v["source"] for v in olano["volumes"]]
    assert sources == [str(work / "data")]
    assert olano["environment"]["OLANO_HOME"] == "/root/.olano"
    assert "@postgres:5432/olano" in olano["environment"]["LIONCLAW_CHECKPOINT_DB_URL"]
    assert "lionclaw" not in olano["environment"]["LIONCLAW_CHECKPOINT_DB_URL"]
    assert not any(k.startswith("LIONCLAW_DEPLOYMENT") for k in olano["environment"])
    assert cfg["services"]["web"]["environment"]["OLANO_SITE_ADDRESS"] == ":80"
    assert cfg["name"] == "olano"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_setup_script_parses() -> None:
    run = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_setup_script_reads_a_quoted_data_dir(tmp_path: Path) -> None:
    """A hand-edited `OLANO_DATA_DIR="…"` names the same directory as the
    unquoted spelling (compose strips the quotes too); the script must not
    create a directory literally named with quotes, nor choke on `=` in it."""
    work = tmp_path / "self-host"
    work.mkdir()
    for name in FILES:
        shutil.copy(SELF_HOST / name, work / name)
    (work / ".env").write_text(
        (SELF_HOST / ".env.example").read_text(encoding="utf-8")
        + '\nOLANO_DATA_DIR="./state=here"\n',
        encoding="utf-8",
    )
    env = _stub_env(tmp_path)
    run = subprocess.run(
        ["bash", str(work / "setup.sh"), "--no-start"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert (work / "state=here").is_dir()
    assert not (work / '"./state=here"').exists()


# ── first-run setup code, address modes, sandbox mode ────────────────────────


def test_overrides_publish_only_what_they_say() -> None:
    """The https override publishes 80 and 443 and nothing else; the Docker
    socket appears in the sandbox override and nowhere else in the stack."""
    https = _uncommented((SELF_HOST / "docker-compose.https.yml").read_text(encoding="utf-8"))
    assert '"80:80"' in https and '"443:443"' in https
    assert "docker.sock" not in https
    sandbox = _uncommented((SELF_HOST / "docker-compose.sandbox.yml").read_text(encoding="utf-8"))
    assert "/var/run/docker.sock:/var/run/docker.sock" in sandbox
    # The same-path mount the host daemon needs for the sandboxes' workspaces.
    assert "${OLANO_DATA_DIR}:${OLANO_DATA_DIR}" in sandbox
    assert "OLANO_SANDBOX_EXEC=1" in sandbox
    for text in (https, sandbox):
        for needle in MANAGED_ONLY:
            assert needle not in text, needle
    # The plain-http port is loopback-only unless the operator says otherwise.
    base = _uncommented(COMPOSE.read_text(encoding="utf-8"))
    assert '"${OLANO_HTTP_BIND:-127.0.0.1}:${OLANO_HTTP_PORT:-3200}:8080"' in base


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_setup_pins_a_one_time_setup_code_once(tmp_path: Path) -> None:
    import re

    work = _copy_files(tmp_path)
    env = _stub_env(tmp_path)
    first = _setup(work, env)
    assert first.returncode == 0, first.stdout + first.stderr
    code = _pins(work)["OLANO_SETUP_CODE"]
    assert re.fullmatch(r"[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}", code), code
    assert code in first.stdout  # printed next to the address
    second = _setup(work, env)
    assert second.returncode == 0
    assert _pins(work)["OLANO_SETUP_CODE"] == code


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_default_stays_on_localhost_behind_nat(tmp_path: Path) -> None:
    """A public IPv4 that is not on any interface is a NAT gateway's."""
    work = _copy_files(tmp_path)
    run = _setup(work, _stub_env(tmp_path, public_ip="203.0.113.7", iface_ip="10.0.0.5"))
    assert run.returncode == 0, run.stdout + run.stderr
    pins = _pins(work)
    assert pins["OLANO_SITE_ADDRESS"] == ":80"
    assert pins["COMPOSE_FILE"] == "docker-compose.yml"
    assert pins["LIONCLAW_PUBLIC_URL"] == "http://localhost:3200"
    assert "--host auto" in run.stdout  # the hint for a NAT'd cloud VM


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_default_goes_https_on_a_public_interface(tmp_path: Path) -> None:
    work = _copy_files(tmp_path)
    env = _stub_env(tmp_path, public_ip="198.51.100.9", iface_ip="198.51.100.9")
    run = _setup(work, env)
    assert run.returncode == 0, run.stdout + run.stderr
    pins = _pins(work)
    assert pins["OLANO_SITE_ADDRESS"] == "198-51-100-9.sslip.io"
    assert pins["LIONCLAW_PUBLIC_URL"] == "https://198-51-100-9.sslip.io"
    assert pins["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.https.yml"
    # A re-run keeps the address even when the lookup now says otherwise.
    again = _setup(work, _stub_env(tmp_path, public_ip="203.0.113.7"))
    assert again.returncode == 0
    assert _pins(work)["OLANO_SITE_ADDRESS"] == "198-51-100-9.sslip.io"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_host_auto_and_local(tmp_path: Path) -> None:
    work = _copy_files(tmp_path)
    run = _setup(work, _stub_env(tmp_path, public_ip="203.0.113.7"), "--host", "auto")
    assert run.returncode == 0, run.stdout + run.stderr
    assert _pins(work)["OLANO_SITE_ADDRESS"] == "203-0-113-7.sslip.io"
    run = _setup(work, _stub_env(tmp_path), "--local")
    assert run.returncode == 0, run.stdout + run.stderr
    pins = _pins(work)
    assert pins["OLANO_SITE_ADDRESS"] == ":80"
    assert pins["LIONCLAW_PUBLIC_URL"] == "http://localhost:3200"
    assert pins["COMPOSE_FILE"] == "docker-compose.yml"
    # auto with no answer from either lookup service is an error, not a guess.
    failed = _setup(work, _stub_env(tmp_path), "--host", "auto")
    assert failed.returncode == 2
    assert _pins(work)["OLANO_SITE_ADDRESS"] == ":80"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_old_https_env_is_migrated_off_port_80(tmp_path: Path) -> None:
    """A setup from before the https override put 80 in OLANO_HTTP_PORT, which
    would now collide with the override's own port 80."""
    work = _copy_files(tmp_path)
    (work / ".env").write_text(
        "COMPOSE_PROJECT_NAME=olano\nLIONCLAW_PG_PASSWORD=" + "a" * 48 + "\n"
        "OLANO_SITE_ADDRESS=olano.example.com\nOLANO_HTTP_PORT=80\nOLANO_HTTPS_PORT=443\n",
        encoding="utf-8",
    )
    run = _setup(work, _stub_env(tmp_path))
    assert run.returncode == 0, run.stdout + run.stderr
    pins = _pins(work)
    assert pins["OLANO_HTTP_PORT"] == "3200"
    assert pins["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.https.yml"
    assert pins["OLANO_SITE_ADDRESS"] == "olano.example.com"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_sandbox_mode_pins_what_the_override_needs(tmp_path: Path) -> None:
    work = _copy_files(tmp_path)
    env = _stub_env(tmp_path)
    run = _setup(work, env, "--sandbox")
    assert run.returncode == 0, run.stdout + run.stderr
    pins = _pins(work)
    assert pins["OLANO_SANDBOX_EXEC"] == "1"
    assert pins["OLANO_DATA_DIR"] == str(work / "data")  # absolute: the host path
    assert pins["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.sandbox.yml"
    # Remembered across runs, and switched off on request.
    assert _setup(work, env).returncode == 0
    assert _pins(work)["COMPOSE_FILE"].endswith("docker-compose.sandbox.yml")
    assert _setup(work, env, "--no-sandbox").returncode == 0
    pins = _pins(work)
    assert pins["OLANO_SANDBOX_EXEC"] == "0"
    assert pins["COMPOSE_FILE"] == "docker-compose.yml"


@pytest.mark.skipif(not _compose_available(), reason="docker compose not available")
def test_rendered_sandbox_mode_mounts_the_home_at_its_host_path(tmp_path: Path) -> None:
    import json

    work = _copy_files(tmp_path)
    env = _stub_env(tmp_path)
    assert _setup(work, env, "--sandbox", "--host", "olano.example.com").returncode == 0
    run = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    cfg = json.loads(run.stdout)
    olano = cfg["services"]["olano"]
    data = str(work / "data")
    mounts = {(v["source"], v["target"]) for v in olano["volumes"]}
    assert (data, data) in mounts
    assert (data, "/root/.olano") in mounts
    assert ("/var/run/docker.sock", "/var/run/docker.sock") in mounts
    assert olano["environment"]["OLANO_HOME"] == data
    assert olano["environment"]["LIONCLAW_HOME"] == data
    assert olano["environment"]["OLANO_SANDBOX_EXEC"] == "1"
    ports = {
        (p.get("host_ip"), str(p["published"]), p["target"])
        for p in cfg["services"]["web"]["ports"]
    }
    assert ("127.0.0.1", "3200", 8080) in ports
    assert {str(p["published"]) for p in cfg["services"]["web"]["ports"]} == {"3200", "80", "443"}


def test_install_script_is_safe_to_pipe() -> None:
    text = (SELF_HOST / "install.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    # Questions are read from the terminal, never from the piped script.
    assert "/dev/tty" in text
    for name in (
        "docker-compose.yml",
        "docker-compose.https.yml",
        "docker-compose.sandbox.yml",
        "Caddyfile",
        ".env.example",
        "setup.sh",
    ):
        assert name in text, name
    live = _uncommented(text)
    for needle in MANAGED_ONLY:
        assert needle not in live, needle
