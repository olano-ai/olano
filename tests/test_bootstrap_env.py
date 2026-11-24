"""Tests for the machine-level bootstrap env loader (config/bootstrap_env.py)."""

from __future__ import annotations

import os

from olano.config.bootstrap_env import (
    bootstrap_env_path,
    load_bootstrap_env,
    parse_env_file,
)


def test_parse_env_file_tolerant_format():
    text = """
# deployment wiring written by cloud-init
LIONCLAW_MCP_RELAY_URL=https://auth.olano.ai
export LIONCLAW_MCP_RELAY_SECRET="s3cr3t=with=equals"
LIONCLAW_INSTANCE_ID='dep_abc'

NOT A KEY LINE
=novalue
BAD-KEY=x
EMPTY=
"""
    pairs = parse_env_file(text)
    assert pairs["LIONCLAW_MCP_RELAY_URL"] == "https://auth.olano.ai"
    assert pairs["LIONCLAW_MCP_RELAY_SECRET"] == "s3cr3t=with=equals"
    assert pairs["LIONCLAW_INSTANCE_ID"] == "dep_abc"
    assert "BAD-KEY" not in pairs
    assert pairs.get("EMPTY") == ""


def test_loader_fills_only_unset_vars(tmp_path, monkeypatch):
    f = tmp_path / "olano.env"
    f.write_text(
        "LIONCLAW_TEST_BOOT_A=from-file\nLIONCLAW_TEST_BOOT_B=from-file\nLIONCLAW_TEST_EMPTY=\n"
    )
    monkeypatch.setenv("LIONCLAW_BOOTSTRAP_ENV", str(f))
    monkeypatch.setenv("LIONCLAW_TEST_BOOT_B", "from-process")
    monkeypatch.delenv("LIONCLAW_TEST_BOOT_A", raising=False)
    monkeypatch.delenv("LIONCLAW_TEST_EMPTY", raising=False)

    applied = load_bootstrap_env()

    assert os.environ["LIONCLAW_TEST_BOOT_A"] == "from-file"  # filled
    assert os.environ["LIONCLAW_TEST_BOOT_B"] == "from-process"  # process wins
    assert "LIONCLAW_TEST_EMPTY" not in os.environ  # empty values never applied
    assert applied == ["LIONCLAW_TEST_BOOT_A"]


def test_loader_missing_file_is_noop(monkeypatch):
    monkeypatch.setenv("LIONCLAW_BOOTSTRAP_ENV", "/nonexistent/olano.env")
    assert load_bootstrap_env() == []
    assert bootstrap_env_path() is None


def test_default_path_used_when_no_override(tmp_path, monkeypatch):
    # With no override and no /opt|/etc file present, path resolution is None.
    monkeypatch.delenv("LIONCLAW_BOOTSTRAP_ENV", raising=False)
    p = bootstrap_env_path()
    assert p is None or str(p) in ("/opt/olano/olano.env", "/etc/olano/olano.env")


def test_idempotent(monkeypatch, tmp_path):
    f = tmp_path / "olano.env"
    f.write_text("LIONCLAW_TEST_BOOT_C=v1\n")
    monkeypatch.setenv("LIONCLAW_BOOTSTRAP_ENV", str(f))
    monkeypatch.delenv("LIONCLAW_TEST_BOOT_C", raising=False)
    assert load_bootstrap_env() == ["LIONCLAW_TEST_BOOT_C"]
    # Second call: var already set, nothing re-applied.
    assert load_bootstrap_env() == []
