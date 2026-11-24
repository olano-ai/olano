"""Agent-to-agent issue tracking (Phase 1: native, in-process).

Durable, assignable work items with an audit trail — the piece the
coordination stack (``talk_to_agent``, heartbeat, scheduler) doesn't cover.
Storage is a small SQLite database at ``<home>/issues.db``; the agent-facing
surface is the six tools in ``olano/tools/issue_tools.py``, and the
dashboard reads through ``/api/issues*`` only.

The six-tool surface and this store's method shape are the stable contracts
for the Phase 2 backend swap (Gitea); see section 6 of the issue-tracking
design record.
"""

from olano.issues.store import IssueStore, get_issue_store

__all__ = ["IssueStore", "get_issue_store"]
