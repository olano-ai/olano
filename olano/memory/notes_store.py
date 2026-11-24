"""Canonical NotesStore imports.

The implementation currently lives in ``knowledge_vault`` for compatibility
with older imports, but new code should import ``NotesStore`` and ``NotesEntry``
from this module.
"""

from olano.memory.knowledge_vault import NotesEntry, NotesStore

__all__ = ["NotesEntry", "NotesStore"]
