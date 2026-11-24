#!/usr/bin/env python3
"""Runs the ACP tutorial for real and keeps what the terminal printed (assets/acp-session.json).

    uv run python demo/assets/acp-capture.py

In a throwaway home it creates one agent (sage, at the made-up Copperline Labs) with two notes in its
workspace, then runs the four commands the video shows: `olano acp --help`, `cat` of a Zed settings
file with the custom agent entry, the small ACP client (assets/acp-client.py, built on the official
`acp` SDK: it starts `olano acp --agent sage` over stdio, as an editor does, and sends two prompts),
and `cat` of the note the agent edited. The agent's model is the offline `fake:` provider with a
scripted answer, as in every demo: no model is called. Everything else is the real thing: the real
`olano acp` server, a real ACP session, real tool calls on the agent's real workspace.
flow-acp.mjs replays the output; the throwaway home is shown as /home/you.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROMPTS = ["How do we store money values?", "Add that to the review checklist."]
SCRIPT = [
    {"tool": "read_file", "args": {"file_path": "notes/conventions.md"}},
    {
        "text": "As integer cents, never floats. The currency code has its own column, "
        "and amounts are rounded only when they are formatted for display."
    },
    {
        "tool": "edit_file",
        "args": {
            "file_path": "notes/checklist.md",
            "old_string": "- [ ] No secrets in the diff",
            "new_string": "- [ ] No secrets in the diff\n- [ ] Money is stored as integer cents, never floats",
        },
    },
    {"text": "Added to notes/checklist.md."},
]
ZED = """\
{
  "agent_servers": {
    "Olano": {
      "type": "custom",
      "command": "olano",
      "args": ["acp", "--agent", "sage"],
      "env": {}
    }
  }
}
"""
CONVENTIONS = """\
# Engineering conventions

## Money
- Store amounts as integer cents, never floats.
- Keep the currency code in its own column.
- Round only when formatting for display.

## Dates
- Store UTC; convert at the edge.
"""
CHECKLIST = "# Review checklist\n\n- [ ] Tests cover the change\n- [ ] No secrets in the diff\n"


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="olano-acp-demo-"))
    home, project = root / ".olano", root / "copperline"
    notes = home / "agents" / "sage" / "workspace" / "notes"
    notes.mkdir(parents=True)
    project.mkdir()
    (notes / "conventions.md").write_text(CONVENTIONS, encoding="utf-8")
    (notes / "checklist.md").write_text(CHECKLIST, encoding="utf-8")
    (home / "agents" / "sage" / "agent.json5").write_text(
        json.dumps(
            {
                "name": "sage",
                "model": "fake:fast",
                "description": "Keeps the engineering notes for Copperline Labs.",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    zed = root / ".config" / "zed" / "settings.json"
    zed.parent.mkdir(parents=True)
    zed.write_text(ZED, encoding="utf-8")
    (root / "script.json").write_text(json.dumps(SCRIPT), encoding="utf-8")
    env = {
        **os.environ,
        "LIONCLAW_HOME": str(home),
        "LIONCLAW_CHECKPOINT_BACKEND": "sqlite",
        "LIONCLAW_FAKE_MODEL_SCRIPT": f"@{root / 'script.json'}",
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "COLUMNS": "80",
    }

    def run(*cmd: str) -> list[str]:
        out = subprocess.run(cmd, env=env, cwd=project, capture_output=True, text=True, check=True)
        return out.stdout.replace(str(root), "/home/you").rstrip("\n").split("\n")

    session: dict[str, object] = {
        "help": run("olano", "acp", "--help"),
        "settings": run("cat", str(zed)),
    }

    # The client, driven the way a person at the keyboard would: wait for its "> ", type a prompt.
    client = subprocess.Popen(
        [sys.executable, str(HERE / "acp-client.py"), "sage"],
        env=env,
        cwd=project,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert client.stdin and client.stdout

    def until_prompt() -> list[str]:
        buf = ""
        while not buf.endswith("\n> "):
            ch = client.stdout.read(1)
            if not ch:
                raise RuntimeError(
                    "the client ended early:\n"
                    + buf
                    + (client.stderr.read() if client.stderr else "")
                )
            buf += ch
        return buf[: -len("\n> ")].replace(str(root), "/home/you").strip("\n").split("\n")

    turns = [{"prompt": None, "out": until_prompt()}]
    for prompt in PROMPTS:
        client.stdin.write(prompt + "\n")
        client.stdin.flush()
        turns.append({"prompt": prompt, "out": until_prompt()})
    client.stdin.write("\n")
    client.stdin.flush()
    client.wait(timeout=30)
    session["client"] = turns
    session["stderr"] = (client.stderr.read() if client.stderr else "").replace(
        str(root), "/home/you"
    )
    session["checklist"] = run("cat", str(notes / "checklist.md"))
    (HERE / "acp-session.json").write_text(
        json.dumps(session, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    shutil.rmtree(root, ignore_errors=True)
    print(json.dumps(session, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
