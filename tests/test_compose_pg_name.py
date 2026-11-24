"""The compose files take the Postgres sidecar's role, database and volume stem
from ``OLANO_PG_NAME``, and fall back to ``lionclaw`` when it is unset.

The fallback is the compatibility story: Postgres applies the names only when
it initialises the data dir, so a box whose ``.env`` carries no pin (every box
deployed before the pin existed) must keep resolving to the role and the
``<project>_lionclaw_pgdata`` volume it was initialised with, while a fresh box
pinned to ``olano`` gets ``olano``/``olano`` on ``<project>_olano_pgdata``.
These tests render each compose file with ``docker compose config`` under both
pins and read the result, so the interpolation (nested defaults included) is
proven by the tool that will run it, not by a regex.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROJECT = "pgnametest"


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


pytestmark = pytest.mark.skipif(not _compose_available(), reason="docker compose not available")


def _render(tmp_path: Path, compose_rel: str, env_lines: list[str]) -> dict:
    """``docker compose config`` for one compose file with one ``.env``."""
    root = tmp_path / "checkout"
    (root / "deploy").mkdir(parents=True)
    shutil.copy(REPO / compose_rel, root / compose_rel)
    (root / ".env").write_text("".join(f"{line}\n" for line in env_lines))
    (root / "deploy" / "caddy.env").write_text("")
    # Nothing from this process's environment may leak into the render: the
    # whole point is what the file resolves to from the .env alone.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OLANO_", "LIONCLAW_")) and k not in {"COMPOSE_PROJECT_NAME"}
    }
    env["HOME"] = str(tmp_path)
    # No --project-directory: the deploy files are written for compose's
    # default (the directory of the first -f file), which is what resolves
    # their `env_file: ../.env` and `./caddy.env` to the checkout's .env and
    # deploy/caddy.env, exactly as bootstrap runs them from the repo root.
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            PROJECT,
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / compose_rel),
            "--profile",
            "postgres",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _postgres(cfg: dict) -> dict:
    return cfg["services"]["postgres"]


def _volume_source(service: dict) -> tuple[str, str]:
    (mount,) = [m for m in service["volumes"] if m["target"] == "/var/lib/postgresql/data"]
    return mount["type"], mount["source"]


@pytest.mark.parametrize(
    "compose_rel", ["deploy/docker-compose.allinone.yml", "deploy/docker-compose.prod.yml"]
)
def test_unpinned_box_keeps_the_lionclaw_role_and_volume(tmp_path, compose_rel):
    cfg = _render(tmp_path, compose_rel, [])
    pg = _postgres(cfg)
    assert pg["container_name"] == "olano_postgres"
    assert pg["environment"]["POSTGRES_USER"] == "lionclaw"
    assert pg["environment"]["POSTGRES_DB"] == "lionclaw"
    assert pg["environment"]["POSTGRES_PASSWORD"] == "lionclaw"
    assert " ".join(pg["healthcheck"]["test"]).endswith("pg_isready -U lionclaw -d lionclaw")
    assert _volume_source(pg) == ("volume", "lionclaw_pgdata")
    assert cfg["volumes"]["lionclaw_pgdata"]["name"] == f"{PROJECT}_lionclaw_pgdata"
    runtime = cfg["services"]["olano"]["environment"]
    assert (
        runtime["LIONCLAW_CHECKPOINT_DB_URL"]
        == "postgresql://lionclaw:lionclaw@postgres:5432/lionclaw"
    )
    assert (
        runtime["LIONCLAW_STORE_DB_URL"] == "postgresql://lionclaw:lionclaw@postgres:5432/lionclaw"
    )


@pytest.mark.parametrize(
    "compose_rel", ["deploy/docker-compose.allinone.yml", "deploy/docker-compose.prod.yml"]
)
def test_pinned_box_runs_as_olano_on_its_own_volume(tmp_path, compose_rel):
    cfg = _render(tmp_path, compose_rel, ["OLANO_PG_NAME=olano", "LIONCLAW_PG_PASSWORD=s3cret"])
    pg = _postgres(cfg)
    assert pg["environment"]["POSTGRES_USER"] == "olano"
    assert pg["environment"]["POSTGRES_DB"] == "olano"
    assert pg["environment"]["POSTGRES_PASSWORD"] == "s3cret"
    assert " ".join(pg["healthcheck"]["test"]).endswith("pg_isready -U olano -d olano")
    assert _volume_source(pg) == ("volume", "olano_pgdata")
    assert cfg["volumes"]["olano_pgdata"]["name"] == f"{PROJECT}_olano_pgdata"
    runtime = cfg["services"]["olano"]["environment"]
    assert runtime["LIONCLAW_CHECKPOINT_DB_URL"] == "postgresql://olano:s3cret@postgres:5432/olano"
    assert runtime["LIONCLAW_STORE_DB_URL"] == "postgresql://olano:s3cret@postgres:5432/olano"


def test_sealed_box_bind_path_wins_over_the_volume_stem(tmp_path):
    """A managed box keeps its data dir on the sealed volume whatever the name says."""
    cfg = _render(
        tmp_path,
        "deploy/docker-compose.allinone.yml",
        ["OLANO_PG_NAME=olano", "LIONCLAW_PGDATA=/mnt/olano-sealed/pgdata"],
    )
    assert _volume_source(_postgres(cfg)) == ("bind", "/mnt/olano-sealed/pgdata")


def test_allinone_container_names_are_olano(tmp_path):
    cfg = _render(tmp_path, "deploy/docker-compose.allinone.yml", [])
    names = {name: svc.get("container_name") for name, svc in cfg["services"].items()}
    assert names == {"olano": "olano", "web": "olano_web", "postgres": "olano_postgres"}


def test_prod_container_names_are_olano(tmp_path):
    cfg = _render(tmp_path, "deploy/docker-compose.prod.yml", [])
    names = {name: svc.get("container_name") for name, svc in cfg["services"].items()}
    assert names == {"olano": "olano", "caddy": "olano_caddy", "postgres": "olano_postgres"}


def test_dev_compose_follows_the_same_pin(tmp_path):
    for env_lines, name in (([], "lionclaw"), (["OLANO_PG_NAME=olano"], "olano")):
        cfg = _render(tmp_path / name, "docker-compose.yml", env_lines)
        pg = _postgres(cfg)
        assert pg["container_name"] == "olano_postgres"
        assert pg["environment"]["POSTGRES_USER"] == name
        assert pg["environment"]["POSTGRES_DB"] == name
        assert _volume_source(pg) == ("volume", f"{name}_pgdata")
        assert cfg["volumes"][f"{name}_pgdata"]["name"] == f"{PROJECT}_{name}_pgdata"
