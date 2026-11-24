"""The shared onboarding bridge-spawn path (WhatsAppAdapter.spawn_pairing_bridge).

The channel-connect QR flow used to spawn the bridge with a bare Popen to
DEVNULL and no npm-install — so on a fresh box the bridge exited immediately
and the page showed only "cannot connect to host …:<port>". This path now
mirrors the adapter's real connect(): install deps, log output, and fail fast
with a user-safe message instead of a silent hang.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from olano.platforms.whatsapp import WhatsAppAdapter


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))


def test_spawn_refuses_when_node_is_missing(monkeypatch):
    import olano.platforms.whatsapp as wa

    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: False)
    with pytest.raises(ValueError, match="Node.js is not installed"):
        WhatsAppAdapter.spawn_pairing_bridge("marcus", {})


def test_spawn_reports_a_bridge_that_dies_on_startup(monkeypatch, tmp_path):
    import olano.platforms.whatsapp as wa

    # Requirements OK; pretend the bundled bridge + its node_modules exist so we
    # skip npm install and go straight to the Popen.
    script = tmp_path / "whatsapp-bridge" / "index.js"
    (script.parent / "node_modules").mkdir(parents=True)
    script.write_text("// bridge")
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: True)
    monkeypatch.setattr(
        WhatsAppAdapter, "_default_bridge_script", staticmethod(lambda: str(script))
    )
    monkeypatch.setattr(wa, "_kill_port_process", lambda port: None)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    dead = MagicMock()
    dead.poll.return_value = 1  # exited immediately
    dead.returncode = 1
    monkeypatch.setattr(wa.subprocess, "Popen", lambda *a, **k: dead)

    with pytest.raises(ValueError, match="exited on startup"):
        WhatsAppAdapter.spawn_pairing_bridge("marcus", {})


def test_spawn_installs_deps_when_node_modules_absent(monkeypatch, tmp_path):
    import olano.platforms.whatsapp as wa

    script = tmp_path / "whatsapp-bridge" / "index.js"
    script.parent.mkdir(parents=True)  # no node_modules → triggers npm install
    script.write_text("// bridge")
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: True)
    monkeypatch.setattr(
        WhatsAppAdapter, "_default_bridge_script", staticmethod(lambda: str(script))
    )
    monkeypatch.setattr(wa, "_kill_port_process", lambda port: None)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)

    installs: list = []

    def fake_run(cmd, **kw):
        installs.append(cmd)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(wa.subprocess, "run", fake_run)
    alive = MagicMock()
    alive.poll.return_value = None  # still running
    monkeypatch.setattr(wa.subprocess, "Popen", lambda *a, **k: alive)

    port, proc, log = WhatsAppAdapter.spawn_pairing_bridge("marcus", {})
    assert installs and installs[0][:2] == ["npm", "install"]
    assert proc is alive
    assert isinstance(port, int)
    assert isinstance(log, Path)


def test_spawn_surfaces_npm_failure(monkeypatch, tmp_path):
    import olano.platforms.whatsapp as wa

    script = tmp_path / "whatsapp-bridge" / "index.js"
    script.parent.mkdir(parents=True)
    script.write_text("// bridge")
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: True)
    monkeypatch.setattr(
        WhatsAppAdapter, "_default_bridge_script", staticmethod(lambda: str(script))
    )
    monkeypatch.setattr(
        wa.subprocess, "run", lambda cmd, **kw: MagicMock(returncode=1, stderr="boom")
    )
    with pytest.raises(ValueError, match="dependencies failed to install"):
        WhatsAppAdapter.spawn_pairing_bridge("marcus", {})
