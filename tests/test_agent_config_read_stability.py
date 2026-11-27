"""Opening an agent must never rewrite its agent.json5.

`save_agent_config` (the model-based writer) used to write ``sandbox: null``
and the dashboard read path listed ``sandbox`` as a legacy key, so the next
page load stripped it and rewrote the file: Revisions recorded a change to an
agent nobody touched. ``sandbox`` is also a live block of resource ceilings,
which the same strip deleted from disk.
"""

from __future__ import annotations

import json

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.loader import save_agent_config


@pytest.fixture
def agents_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    from olano.config.home import get_olano_paths

    return get_olano_paths().agents_dir


def test_save_does_not_write_a_null_sandbox(agents_home):
    path = save_agent_config(AgentConfig(name="steady"), base=agents_home)
    assert "sandbox" not in json.loads(path.read_text(encoding="utf-8"))


def test_read_after_save_leaves_the_file_unchanged(agents_home):
    from olano.runtime import api

    path = save_agent_config(AgentConfig(name="steady"), base=agents_home)
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    api._read_agent_config("steady")
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_read_keeps_explicit_null_and_resource_block(agents_home):
    from olano.runtime import api

    for name, value in (("nulled", None), ("ceilings", {"memory": "4g", "cpus": 2})):
        d = agents_home / name
        d.mkdir(parents=True)
        path = d / "agent.json5"
        path.write_text(json.dumps({"name": name, "sandbox": value}) + "\n", encoding="utf-8")
        before = path.read_bytes()
        data = api._read_agent_config(name)
        assert data["sandbox"] == value
        assert path.read_bytes() == before


def test_read_still_migrates_the_old_scalar_spelling(agents_home):
    from olano.runtime import api

    d = agents_home / "ancient"
    d.mkdir(parents=True)
    path = d / "agent.json5"
    path.write_text(json.dumps({"name": "ancient", "sandbox": True}) + "\n", encoding="utf-8")
    data = api._read_agent_config("ancient")
    assert "sandbox" not in data
    assert "sandbox" not in json.loads(path.read_text(encoding="utf-8"))
