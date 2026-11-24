"""disabled_tools enforcement at the model-bind boundary.

The builder's own tool merge can't reach the tools deepagents attaches inside
``create_deep_agent`` (``task``, the backend filesystem/shell primitives), so
``core/disabled_tools_middleware`` filters every model request's bound tool
list by name — that filter is what makes the dashboard's internal-tools
checkboxes real for those rows.
"""

from __future__ import annotations

from types import SimpleNamespace

from olano.core.disabled_tools_middleware import (
    DisabledToolsMiddleware,
    _tool_name,
    build_disabled_tools_middleware,
)


class _FakeRequest:
    """Duck-typed stand-in for langchain's ModelRequest: `.tools` + `.override()`."""

    def __init__(self, tools):
        self.tools = tools

    def override(self, *, tools):
        return _FakeRequest(tools)


def _tool(name: str):
    return SimpleNamespace(name=name)


def test_empty_disabled_set_builds_no_middleware():
    assert build_disabled_tools_middleware([]) == []
    assert build_disabled_tools_middleware({""}) == []


def test_disabled_names_are_dropped_from_the_model_bind():
    mw = DisabledToolsMiddleware({"read_file", "task"})
    request = _FakeRequest([_tool("read_file"), _tool("web_search"), _tool("task")])

    seen: dict[str, list[str]] = {}

    def handler(req):
        seen["tools"] = [t.name for t in req.tools]
        return "response"

    assert mw.wrap_model_call(request, handler) == "response"
    assert seen["tools"] == ["web_search"]


def test_untouched_request_is_passed_through_unchanged():
    mw = DisabledToolsMiddleware({"execute"})
    request = _FakeRequest([_tool("web_search")])
    captured = []
    mw.wrap_model_call(request, lambda req: captured.append(req))
    # No name matched → the original request object, not an override copy.
    assert captured[0] is request


async def test_async_path_filters_too():
    mw = DisabledToolsMiddleware({"ls"})
    request = _FakeRequest([_tool("ls"), _tool("grep")])

    async def handler(req):
        return [t.name for t in req.tools]

    assert await mw.awrap_model_call(request, handler) == ["grep"]


def test_tool_name_handles_dict_schemas():
    assert _tool_name({"name": "web_search"}) == "web_search"
    assert _tool_name({"type": "function", "function": {"name": "execute"}}) == "execute"
    assert _tool_name(SimpleNamespace(name="task")) == "task"
    assert _tool_name(object()) == ""


def test_builder_wires_the_middleware_only_when_something_is_disabled(base_config, monkeypatch):
    """`_prepare_agent_build_rest` includes the filter middleware iff
    disabled_tools is non-empty (zero overhead for the common case)."""
    import olano.core.builder as builder

    def middleware_names(cfg):
        prepared = builder._prepare_agent_build(cfg)
        return [type(m).__name__ for m in prepared["middleware"]]

    monkeypatch.setattr(builder, "_build_main_model", lambda *a, **k: object())

    base_config.disabled_tools = []
    assert "DisabledToolsMiddleware" not in middleware_names(base_config)

    base_config.disabled_tools = ["read_file"]
    assert "DisabledToolsMiddleware" in middleware_names(base_config)
