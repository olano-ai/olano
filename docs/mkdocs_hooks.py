"""MkDocs hooks for the user guide site (mkdocs.yml `hooks:`).

Mermaid diagrams are written as ```mermaid fences in the pages. MkDocs Material
draws them when pymdownx.superfences hands those fences through as
`<pre class="mermaid">` blocks, which needs a Python callable in the
extension's config. In mkdocs.yml that is a `!!python/name:` tag, and that tag
would make the file unreadable for `yaml.safe_load`, which
scripts/gen_user_guide.py and tests/test_user_guide_generated.py use to read
the nav. So the custom fence is added here, at config time, and mkdocs.yml
stays plain YAML.
"""

from __future__ import annotations

from typing import Any

from pymdownx.superfences import fence_code_format


def on_config(config: Any) -> Any:
    fences = config.mdx_configs.setdefault("pymdownx.superfences", {})
    custom = fences.setdefault("custom_fences", [])
    if not any(f.get("name") == "mermaid" for f in custom):
        custom.append({"name": "mermaid", "class": "mermaid", "format": fence_code_format})
    return config
