"""Automatic revision tracking of the olano home (Phase 1).

Allowlisted config + markdown files are committed to a **bare** git repo at
``<home>/revisions.git`` by a debounced background worker; commits are
authored by the agent whose turn changed them. The user never runs git.

Design invariants (section 3 of the revision-tracking design record):

* **Allowlist, never denylist** — a file is versioned only because a pattern
  in ``engine.ALLOWLIST`` names it. New files the codebase grows are ignored
  until someone consciously adds them here (fail closed).
* **No ``.git`` at the home root** — commits are built from a snapshot copy,
  so git never reads the live home and ``git add -A`` against it is
  structurally impossible.
* **Secrets never enter history** — ``agent.json5`` ``env`` values are
  redacted at snapshot time; databases, tokens, the vault, and ``.env`` are
  simply absent from the allowlist.
* The repo is plain git (Contract B): Phase 2 adds a push remote, nothing
  else.
"""

from olano.revisions.engine import RevisionsEngine, snapshot_home
from olano.revisions.worker import get_revisions_worker, notify_turn

__all__ = ["RevisionsEngine", "get_revisions_worker", "notify_turn", "snapshot_home"]
