"""TikTok creator and community-management toolkit.

Configure an API product/partner gateway with ``TIKTOK_API_BASE_URL``. Access
to comments and Direct Messages is not granted to every TikTok developer app;
tools return the upstream permission error rather than scraping or automating a
consumer login.
"""

from olano.tools._social_platform_factory import make_social_tools

_TOOLS = make_social_tools("tiktok")


def get_tools(names: list[str] | None = None):
    return _TOOLS if names is None else [tool for tool in _TOOLS if tool.name in names]


def get_tool(name: str):
    return next((tool for tool in _TOOLS if tool.name == name), None)
