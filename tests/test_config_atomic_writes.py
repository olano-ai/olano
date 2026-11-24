"""Config files are never observable half-written — ``agent.json5``, ``config.yaml``.

``Path.write_text`` truncates the file before it writes the new bytes, so a
concurrent reader can read **zero bytes**. That is not theoretical: it turned up
as a red E2E run
(``test_agent_tools_internal.py::test_unchecking_runtime_primitive_persists_too``
→ ``JSONDecodeError: Expecting value: line 1 column 1``), where the test polled
agent.json5 while the dashboard's config write was mid-truncate.

The two files fail *differently*, which is why both are covered here:

* **agent.json5** raises. Loud, except in the readers that swallow it
  (``except Exception: old_raw = {}`` in the deployment-limit guard), where a
  truncated read counts the agent as empty and can mis-decide a cap.
* **config.yaml** does not raise at all. ``yaml.safe_load("")`` returns
  ``None`` and every reader coalesces that to ``{}`` (``… or {}``), so the home
  silently reports *no settings* and the caller runs on defaults. The
  read-modify-write writers then merge their one key into that empty dict and
  save it — turning a momentary bad read into permanent loss of every other key
  in the file. ``test_home_config_rmw_cannot_lose_keys`` pins that.

The reader loops below fail a few percent of reads against a plain
``write_text`` and cannot fail against the atomic writers, whose ``os.replace``
publishes the new file in one step.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import yaml

from olano.config.agent_config import AgentConfig
from olano.config.home import (
    HomeConfig,
    save_home_config,
    update_home_config,
    write_home_config_text,
)
from olano.config.loader import save_agent_config, write_agent_config_text

# Big enough that the write is not a single trivial syscall, which is what makes
# the truncate window observable at all.
_PAYLOAD = json.dumps(
    {
        "name": "racer",
        "model": "anthropic:claude-haiku-4-5-20251001",
        "tools": [f"tool_{i}" for i in range(500)],
    },
    indent=2,
)


class _Reader:
    """Hammers ``path`` in a thread, recording every read that didn't come back whole.

    "Didn't come back whole" is deliberately wider than "raised": ``yaml`` never
    raises on an empty document, it returns ``None``. A falsy parse result is
    exactly the silent failure config.yaml's readers suffer, so it counts as a
    bad read here too.
    """

    def __init__(self, path: Path, parse=json.loads) -> None:
        self.path = path
        self.parse = parse
        self.bad: list[str] = []
        self.reads = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                parsed = self.parse(self.path.read_text(encoding="utf-8"))
                if not parsed:
                    self.bad.append(f"parsed empty: {parsed!r}")
            except Exception as exc:
                self.bad.append(str(exc))
            self.reads += 1

    def __enter__(self) -> _Reader:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def test_write_agent_config_text_is_never_seen_empty(tmp_path: Path) -> None:
    path = tmp_path / "agent.json5"
    write_agent_config_text(path, _PAYLOAD)

    with _Reader(path) as reader:
        for _ in range(300):
            write_agent_config_text(path, _PAYLOAD)

    assert reader.reads > 0, "reader thread never ran — the test proves nothing"
    assert not reader.bad, (
        f"{len(reader.bad)}/{reader.reads} reads saw a partial file: {reader.bad[:3]}"
    )


def test_save_agent_config_is_never_seen_empty(tmp_path: Path) -> None:
    """The same guarantee through the public persistence path."""
    cfg = AgentConfig(name="racer", tools=[f"tool_{i}" for i in range(500)])
    path = save_agent_config(cfg, base=tmp_path, enforce_deployment_limits=False)

    with _Reader(path) as reader:
        for _ in range(150):
            save_agent_config(cfg, base=tmp_path, enforce_deployment_limits=False)

    assert reader.reads > 0
    assert not reader.bad, (
        f"{len(reader.bad)}/{reader.reads} reads saw a partial file: {reader.bad[:3]}"
    )


def test_write_agent_config_text_leaves_no_temp_siblings(tmp_path: Path) -> None:
    """The temp file is a sibling (same filesystem, so ``replace`` is atomic) and
    must not survive the write — a stray ``.agent.json5.*.tmp`` would be picked
    up by nothing, but it accumulates in every agent dir."""
    path = tmp_path / "agent.json5"
    for _ in range(5):
        write_agent_config_text(path, _PAYLOAD)

    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "racer"
    # Only .tmp siblings — the autouse `lionclaw_home` fixture also puts a dir
    # in tmp_path, so a full listing comparison would be checking that instead.
    assert not [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]


# ------------------------------------------------------------------ config.yaml

_HOME_YAML = yaml.dump(
    {
        "default_agent": "merli",
        "dashboard_port": 3200,
        "cognition": {"enabled": True, "intensity": "aggressive"},
        "disk_monitor": {"enabled": True, "threshold_percent": 85},
        "archetype_dirs": [f"/srv/archetypes/{i}" for i in range(300)],
    },
    sort_keys=False,
)


def test_write_home_config_text_is_never_seen_empty(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_home_config_text(path, _HOME_YAML)

    with _Reader(path, parse=yaml.safe_load) as reader:
        for _ in range(300):
            write_home_config_text(path, _HOME_YAML)

    assert reader.reads > 0, "reader thread never ran — the test proves nothing"
    assert not reader.bad, (
        f"{len(reader.bad)}/{reader.reads} reads saw a partial file: {reader.bad[:3]}"
    )


def test_save_home_config_is_never_seen_empty(tmp_path: Path) -> None:
    """The same guarantee through the public persistence path."""
    home = tmp_path / "home"
    cfg = HomeConfig(archetype_dirs=[f"/srv/archetypes/{i}" for i in range(300)])
    path = save_home_config(cfg, override=home)

    with _Reader(path, parse=yaml.safe_load) as reader:
        for _ in range(150):
            save_home_config(cfg, override=home)

    assert reader.reads > 0
    assert not reader.bad, (
        f"{len(reader.bad)}/{reader.reads} reads saw a partial file: {reader.bad[:3]}"
    )


def test_home_config_rmw_cannot_lose_keys(tmp_path: Path) -> None:
    """The consequence that makes config.yaml worse than agent.json5.

    ``update_home_config`` is a read-modify-write: it loads the raw YAML, merges
    its one key, and writes the whole document back. A truncated read parses as
    ``None`` → ``{}`` and never raises, so the merge would persist a document
    containing *only* the key being set — silently dropping ``cognition``,
    ``disk_monitor`` and everything else the dataclass doesn't own. With the
    write atomic, a concurrent reader-writer pair can't observe that state.
    """
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    write_home_config_text(path, _HOME_YAML)

    with _Reader(path, parse=yaml.safe_load) as reader:
        for port in range(3200, 3260):
            update_home_config({"dashboard_port": port}, override=home)

    assert not reader.bad, (
        f"{len(reader.bad)}/{reader.reads} reads saw a partial file: {reader.bad[:3]}"
    )
    # The keys update_home_config doesn't own must all have survived every merge.
    final = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert final["cognition"] == {"enabled": True, "intensity": "aggressive"}
    assert final["disk_monitor"] == {"enabled": True, "threshold_percent": 85}
    assert final["dashboard_port"] == 3259
