"""What a shell result tells the agent, shaped so it does not cost extra calls.

Seen live on a managed box: changing one field in two campaigns took the agent
23 tool calls, about nine of them caused by how command output was presented:

- a 31-line ``--help`` (1.3 KB) went to a log and its preview dropped the three
  lines naming the subcommands, so the agent asked for help again;
- the log path read ``/workspace/.olano/...``, a container alias the agent had
  never seen elsewhere, so it "corrected" it to a host path that did not exist
  and the read was refused;
- a failing command whose preview held all of its output still said "The
  preview below is partial. Read the full log", and the agent read it;
- an ``rg`` result clipped to 240 characters a line gave the agent a guessed
  ``old_string``; the edit failed and it searched twice more.

Each is pinned here, on the output shapes from that turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.security import shell_output
from olano.security.shell_output import (
    COMPLETE_HINT,
    FAILURE_HINT,
    SEARCH_HINT,
    SUCCESS_HINT,
    is_search_command,
    summarize_shell_output,
)


def _summarize(tmp_path: Path, *, command: str, stdout: str = "", stderr: str = "", rc: int = 0):
    return summarize_shell_output(
        tool_name="execute",
        tool_call_id="call_462c52d",
        workspace_root=tmp_path,
        command=command,
        runtime_command=command,
        timeout=30,
        stdout=stdout,
        stderr=stderr,
        returncode=rc,
    )


def _crm_help() -> str:
    """argparse help shaped like the live one: 31 lines, about 1.3 KB."""
    subcommands = [
        "init",
        "worklist",
        "repair-contact-times",
        "backup",
        "search",
        "company",
        "customer",
        "campaign",
        "outreach",
        "social",
    ]
    lines = [
        "usage: olano-crm [-h] [--db DB]",
        "                 {" + ",".join(subcommands) + "}",
        "                 ...",
        "",
        "Local SQLite marketing CRM",
        "",
        "positional arguments:",
        "  {" + ",".join(subcommands) + "}",
    ]
    lines += [f"    {name:<22}{name.replace('-', ' ')} commands" for name in subcommands]
    lines += ["", "options:", "  -h, --help            show this help message and exit"]
    lines += ["  --db DB               path to the SQLite database"] * 10
    return "\n".join(lines[:31])


def _rg_matches(count: int = 22, width: int = 780) -> str:
    """``rg -n`` output: long markdown lines, as in a campaign brief."""
    rows = []
    for n in range(count):
        text = f"Daily campaign target: 30 distinct new people, row {n}. " + "x" * width
        rows.append(f"/home/olano/ws/campaigns/employee-benefits/campaign.md:{60 + n}:{text}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# 1. Ordinary output stays inline
# ---------------------------------------------------------------------------


def test_a_cli_help_page_is_returned_whole(tmp_path: Path) -> None:
    help_text = _crm_help()
    assert len(help_text.splitlines()) == 31 and len(help_text) > 1_200  # was offloaded

    summary = _summarize(tmp_path, command="python3 crm.py --help", stdout=help_text)

    assert not summary.large_output and summary.artifact is None
    assert summary.content == help_text
    assert "repair-contact-times" in summary.content
    assert not (tmp_path / ".olano").exists()  # no log written


def test_a_json_record_is_returned_whole(tmp_path: Path) -> None:
    record = json.dumps(
        {"id": 4, "name": "Employee Benefits", "goal": "g" * 400, "notes": "n" * 1500},
        indent=2,
    )
    summary = _summarize(tmp_path, command="python3 crm.py campaign show 4", stdout=record)
    assert summary.artifact is None and summary.content == record


def test_output_over_the_limits_still_goes_to_a_log(tmp_path: Path) -> None:
    by_chars = "y" * (shell_output.INLINE_MAX_CHARS + 1)
    by_lines = "\n".join("z" for _ in range(shell_output.INLINE_MAX_LINES + 1))
    for stdout in (by_chars, by_lines):
        summary = _summarize(tmp_path, command="cat big.txt", stdout=stdout)
        assert summary.large_output and summary.artifact is not None


# ---------------------------------------------------------------------------
# 2. The log path is the host path
# ---------------------------------------------------------------------------


def test_the_log_path_is_the_host_path(tmp_path: Path) -> None:
    summary = _summarize(tmp_path, command="rg -n target campaigns", stdout=_rg_matches())

    assert summary.artifact is not None
    log_path = Path(summary.artifact["log_path"])
    shown = summary.artifact["workspace_log_path"]
    assert shown == str(log_path.absolute())
    assert shown.startswith(str(tmp_path.absolute() / ".olano" / "artifacts" / "shell") + "/")
    assert f"Full log: {shown}" in summary.content
    assert "/workspace/" not in summary.content
    # The path names the file that was written, so read_file on it succeeds.
    assert log_path.is_file()


# ---------------------------------------------------------------------------
# 3. "Read the log" only when something was left out
# ---------------------------------------------------------------------------


@pytest.fixture
def old_inline_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The previous limits, so a log is written for output a preview holds whole.

    With the current limits a stream short enough to preview whole is short
    enough to inline, so this is the way to reach the branch; it is still a
    contract worth pinning, because the limits are tuning and will move.
    """
    monkeypatch.setattr(shell_output, "INLINE_MAX_CHARS", 1_200)
    monkeypatch.setattr(shell_output, "INLINE_MAX_LINES", 24)


@pytest.mark.usefixtures("old_inline_limits")
def test_a_failure_shown_whole_does_not_send_the_agent_to_the_log(tmp_path: Path) -> None:
    """The live shape: 19 lines of stdout, one line of stderr, exit 1."""
    stdout = "Updated\nUpdated\n" + json.dumps({"id": 4, "goal": "g" * 900}, indent=2)
    stdout += "\n" + "\n".join(f'  "k{n}": "v"' for n in range(14))
    stderr = "Error: campaign 3: daily_outreach_target is locked while a run is active " + "e" * 150
    assert len(stdout) + len(stderr) > 1_200

    summary = _summarize(
        tmp_path,
        command="crm.py campaign update 4 && crm.py campaign show 3",
        stdout=stdout,
        stderr=stderr,
        rc=1,
    )

    assert summary.large_output
    assert summary.content.startswith("Command failed with exit code 1.")
    assert COMPLETE_HINT in summary.content
    assert FAILURE_HINT not in summary.content and SUCCESS_HINT not in summary.content
    assert stderr in summary.content and stdout.strip() in summary.content
    assert "omitted" not in summary.content and "…" not in summary.content


@pytest.mark.usefixtures("old_inline_limits")
def test_a_success_shown_whole_says_so(tmp_path: Path) -> None:
    """The live `campaign update --help`: 26 lines, offloaded at 24, preview whole."""
    help_text = "\n".join(_crm_help().splitlines()[:26])
    assert len(help_text.splitlines()) > 24
    summary = _summarize(tmp_path, command="python3 crm.py --help", stdout=help_text)
    assert summary.large_output
    assert COMPLETE_HINT in summary.content and SUCCESS_HINT not in summary.content
    assert help_text in summary.content


def test_a_cut_preview_still_points_at_the_log(tmp_path: Path) -> None:
    stdout = "\n".join(f"line {i}" for i in range(400))
    summary = _summarize(tmp_path, command="cat big.txt", stdout=stdout)
    assert SUCCESS_HINT in summary.content and COMPLETE_HINT not in summary.content

    failed = _summarize(tmp_path, command="cat big.txt", stdout=stdout, rc=1)
    assert FAILURE_HINT in failed.content and COMPLETE_HINT not in failed.content


@pytest.mark.usefixtures("old_inline_limits")
def test_whole_previews_cut_by_the_overall_cap_are_not_called_complete(tmp_path: Path) -> None:
    """Each stream fits its preview, but together they pass the summary cap."""
    stdout = "o" * shell_output.PREVIEW_MAX_CHARS
    stderr = "r" * shell_output.PREVIEW_MAX_CHARS
    summary = _summarize(tmp_path, command="make", stdout=stdout, stderr=stderr)
    assert len(summary.content) <= shell_output.SUMMARY_MAX_CHARS
    assert COMPLETE_HINT not in summary.content
    assert SUCCESS_HINT in summary.content


def test_a_caller_hint_still_wins(tmp_path: Path) -> None:
    stdout = "\n".join(f"line {i}" for i in range(400))
    summary = summarize_shell_output(
        tool_name="execute",
        tool_call_id="c",
        workspace_root=tmp_path,
        command="rg x",
        runtime_command="rg x",
        timeout=5,
        stdout=stdout,
        stderr="",
        returncode=0,
        hint="Custom hint.",
    )
    assert "Custom hint." in summary.content
    assert SEARCH_HINT not in summary.content and SUCCESS_HINT not in summary.content


# ---------------------------------------------------------------------------
# 4. A shortened search says where the exact text is
# ---------------------------------------------------------------------------


def test_a_clipped_search_result_says_how_to_get_whole_lines(tmp_path: Path) -> None:
    """The live shape: 22 matches, 17 KB, every line longer than the clip."""
    command = (
        'rg -n "30 distinct new people|up to 30 initial|Daily campaign target" '
        "campaigns/employee-benefits campaigns/wellness-events"
    )
    summary = _summarize(tmp_path, command=command, stdout=_rg_matches())

    assert summary.large_output
    assert SUCCESS_HINT in summary.content and SEARCH_HINT in summary.content
    assert "…" in summary.content  # the lines really were clipped


def test_a_whole_search_result_needs_no_search_hint(tmp_path: Path) -> None:
    short = _rg_matches(count=5, width=40)
    summary = _summarize(tmp_path, command="rg -n target campaigns", stdout=short)
    assert summary.artifact is None and summary.content == short


def test_a_non_search_command_gets_no_search_hint(tmp_path: Path) -> None:
    summary = _summarize(tmp_path, command="cat campaign.md", stdout=_rg_matches())
    assert SUCCESS_HINT in summary.content and SEARCH_HINT not in summary.content


def test_a_failed_search_gets_both_hints(tmp_path: Path) -> None:
    summary = _summarize(tmp_path, command="grep -rn target .", stdout=_rg_matches(), rc=2)
    assert FAILURE_HINT in summary.content and SEARCH_HINT in summary.content


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("rg -n target campaigns", True),
        ('rg -n -e "--new-target" -e new_target skills', True),
        ("grep -rn foo .", True),
        ("egrep 'a|b' f.txt", True),
        ("/usr/bin/rg foo", True),
        ("cd campaigns && rg foo", True),
        ("cat f.txt | grep foo", True),
        ("LC_ALL=C grep -n foo f.txt", True),
        ("timeout 20 rg foo", True),
        ("sudo -n grep foo /etc/hosts", True),
        ("git grep -n foo", True),
        ("git -C repo grep foo", True),
        ("ag foo", True),
        ("cat campaign.md", False),
        ("python3 crm.py campaign show 4", False),
        ("echo grep", False),
        ("git log --grep foo", False),
        ("python3 -c 'import re; re.search(\"rg\", s)'", False),
        ("rg 'unbalanced", False),
        ("", False),
    ],
)
def test_is_search_command(command: str, expected: bool) -> None:
    assert is_search_command(command) is expected
