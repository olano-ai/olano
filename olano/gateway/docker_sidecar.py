"""Docker Compose sidecar override for gateway in docker-install mode."""

from __future__ import annotations

from pathlib import Path

_COMPOSE_FILENAME = "docker-compose.gateway.yml"


def _build_compose_override(home: Path, binary: str) -> str:
    return f"""services:
  olano-gateway:
    image: olano:latest
    restart: always
    command: ["{binary}", "serve"]
    environment:
      OLANO_HOME: {home}
      LIONCLAW_HOME: {home}
    volumes:
      - {home}:{home}:rw
"""


def install_sidecar(home: Path, *, output_dir: Path | None = None) -> Path:
    from olano.gateway import _lionclaw_binary

    binary = _lionclaw_binary()
    out_dir = output_dir or home
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / _COMPOSE_FILENAME
    path.write_text(_build_compose_override(home, binary), encoding="utf-8")
    return path
