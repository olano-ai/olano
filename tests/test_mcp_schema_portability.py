"""MCP tool schemas are made portable before they reach a provider."""

from types import SimpleNamespace

from olano.core.mcp_setup import sanitize_mcp_tool_schemas

# Lusha's shape: OpenAI refuses the whole request with
# "invalid_json_schema: regex lookaround is not supported".
LUSHA_EMAIL = r"^(?!.*\.\.)[A-Za-z0-9._%+-]+@(?=[A-Za-z0-9-]+\.)[A-Za-z0-9.-]+$"


def _tool(schema):
    return SimpleNamespace(name="lusha_enrich", args_schema=schema)


def test_lookaround_pattern_is_dropped_and_the_rest_is_kept():
    tool = _tool(
        {
            "type": "object",
            "properties": {
                "email": {"type": "string", "pattern": LUSHA_EMAIL, "description": "Email"},
                "domain": {"type": "string", "pattern": r"^[a-z0-9.-]+$"},
            },
            "required": ["email"],
        }
    )
    sanitize_mcp_tool_schemas([tool])
    props = tool.args_schema["properties"]
    assert "pattern" not in props["email"]
    assert props["email"]["description"] == "Email"
    assert props["domain"]["pattern"] == r"^[a-z0-9.-]+$"
    assert tool.args_schema["required"] == ["email"]


def test_nested_defs_and_arrays_are_walked():
    tool = _tool(
        {
            "$defs": {"Contact": {"properties": {"e": {"pattern": r"(?<=x)y"}}}},
            "properties": {"list": {"items": [{"pattern": r"(a)\1"}, {"pattern": r"(?>ab)"}]}},
        }
    )
    sanitize_mcp_tool_schemas([tool])
    assert "pattern" not in tool.args_schema["$defs"]["Contact"]["properties"]["e"]
    assert tool.args_schema["properties"]["list"]["items"] == [{}, {}]


def test_instance_data_and_property_named_pattern_are_untouched():
    schema = {
        "properties": {
            "pattern": {"type": "string"},
            "mode": {"enum": [{"pattern": "(?=x)"}], "default": {"pattern": "(?!y)"}},
        }
    }
    tool = _tool(schema)
    sanitize_mcp_tool_schemas([tool])
    assert tool.args_schema is schema  # nothing dropped: the original is kept
    assert tool.args_schema["properties"]["pattern"] == {"type": "string"}


def test_non_dict_schema_is_left_alone():
    sentinel = object()
    tool = _tool(sentinel)
    sanitize_mcp_tool_schemas([tool])
    assert tool.args_schema is sentinel
