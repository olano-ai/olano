"""The dashboard's peer-tool list must match the runner's.

`runner._PEER_TOOL_NAMES` decides which calls get announced to the stream early
(before the whole argument object has been written) and whose output gets
rerouted as peer text instead of the caller's own words. The simple shell's
`PEER_TOOLS` reads that same announcement to draw "<peer> is thinking…" while a
member waits on another bot.

They are two spellings of one fact, in two languages, and drift is silent: add a
peer tool on the Python side and the indicator simply never appears for it —
nothing errors, the group just goes quiet again for the length of the call.
Pure-text parsing on purpose: no node toolchain in the unit suite.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_RUNNER = _ROOT / "olano" / "core" / "runner.py"
_DETAIL = _ROOT / "olano" / "dashboard_2.0" / "src" / "views" / "simple" / "detail.tsx"


def _names(source: str, pattern: str) -> set[str]:
    match = re.search(pattern, source, re.MULTILINE | re.DOTALL)
    assert match, f"could not find the tool-name set: {pattern}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_dashboard_peer_tools_match_the_runner() -> None:
    runner = _names(_RUNNER.read_text(), r"^_PEER_TOOL_NAMES = \{(.*?)\}")
    dashboard = _names(_DETAIL.read_text(), r"^const PEER_TOOLS = new Set\(\[(.*?)\]\)")
    assert runner, "the runner's peer-tool set parsed as empty"
    assert runner == dashboard, (
        "olano/core/runner.py and the simple shell's detail.tsx disagree about "
        f"which tools reach a peer: runner={sorted(runner)} dashboard={sorted(dashboard)}. "
        "A tool announced by one and unknown to the other loses its "
        "'<peer> is thinking…' line in group chats."
    )
