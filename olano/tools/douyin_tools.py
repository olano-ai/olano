"""Douyin creator and community-management toolkit (approved APIs only)."""

from olano.tools._social_platform_factory import make_social_tools

_TOOLS = make_social_tools("douyin")


def get_tools(names: list[str] | None = None):
    return _TOOLS if names is None else [tool for tool in _TOOLS if tool.name in names]


def get_tool(name: str):
    return next((tool for tool in _TOOLS if tool.name == name), None)
