"""Tests for the per-message 'active human' identity block."""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.home import load_home_config, save_home_config
from olano.config.workspace import Workspace
from olano.core.system_middleware import (
    SESSION_START_TOKEN,
    _render_active_human_block,
    load_builtin_middleware,
)
from olano.security.request_context import RequestContext, push_request_context

# --------------------------------------------------------------- render helper


def test_block_rendered_for_authenticated_human():
    ctx = RequestContext(
        user_id="u_alice",
        authenticated=True,
        is_admin=True,
        metadata={"name": "Alice", "email": "alice@x.com"},
    )
    block = _render_active_human_block(ctx)
    assert "[active-human]" in block and "[/active-human]" in block
    assert "Alice" in block
    assert "alice@x.com" in block
    assert "id=u_alice" in block
    assert "role=admin" in block


def test_block_role_user_for_non_admin():
    ctx = RequestContext(user_id="u_bob", authenticated=True, metadata={"name": "Bob"})
    block = _render_active_human_block(ctx)
    assert "role=user" in block
    assert "Bob" in block


def test_block_falls_back_to_user_id_when_no_name():
    ctx = RequestContext(user_id="u_nmeta", authenticated=True, metadata={})
    block = _render_active_human_block(ctx)
    # With no name/email metadata, the user_id is used as the display name.
    assert "You are speaking with: u_nmeta (id=u_nmeta, role=user)" in block


def test_block_empty_for_owner_and_anonymous():
    assert _render_active_human_block(RequestContext()) == ""  # default owner
    assert _render_active_human_block(RequestContext(user_id="anonymous")) == ""
    assert (
        _render_active_human_block(
            RequestContext(user_id="u_x", authenticated=False, metadata={"name": "X"})
        )
        == ""
    )


# --------------------------------------------------------------- gated hook


def _identity_hook(tmp_workspace: Path, base_config):
    hooks = load_builtin_middleware(Workspace(tmp_workspace), base_config)
    assert len(hooks.on_input) == 2  # onboarding + identity
    return hooks.on_input[-1]


@pytest.mark.asyncio
async def test_identity_hook_noop_when_multi_user_disabled(tmp_workspace: Path, base_config):
    hook = _identity_hook(tmp_workspace, base_config)  # default: multi_user off
    with push_request_context(user_id="u_a", authenticated=True, metadata={"name": "Ann"}):
        out = await hook("hello", base_config, {})
    assert out == "hello"


@pytest.mark.asyncio
async def test_identity_hook_prepends_when_multi_user_enabled(tmp_workspace: Path, base_config):
    cfg = load_home_config()
    cfg.multi_user_enabled = True
    save_home_config(cfg)

    hook = _identity_hook(tmp_workspace, base_config)
    with push_request_context(user_id="u_a", authenticated=True, metadata={"name": "Ann"}):
        out = await hook("hello", base_config, {})
    assert "[active-human]" in out
    assert "Ann" in out
    assert out.endswith("hello")


@pytest.mark.asyncio
async def test_identity_hook_leaves_sentinels_untouched(tmp_workspace: Path, base_config):
    cfg = load_home_config()
    cfg.multi_user_enabled = True
    save_home_config(cfg)

    hook = _identity_hook(tmp_workspace, base_config)
    with push_request_context(user_id="u_a", authenticated=True, metadata={"name": "Ann"}):
        out = await hook(SESSION_START_TOKEN, base_config, {})
    assert out == SESSION_START_TOKEN
