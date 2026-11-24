"""RedNote/Xiaohongshu creator and community-management toolkit.

Xiaohongshu capabilities depend heavily on the approved application/partner
product. This adapter intentionally does not use cookie automation or scraping.
"""

from olano.tools._social_platform_factory import make_social_tools

_TOOLS = make_social_tools("rednote")


def get_tools(names: list[str] | None = None):
    return _TOOLS if names is None else [tool for tool in _TOOLS if tool.name in names]


def get_tool(name: str):
    return next((tool for tool in _TOOLS if tool.name == name), None)
