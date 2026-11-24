"""Compatibility wrapper for the old markdown-vault tools module.

The notes tools now live in :mod:`olano.memory.notes_tools` and use
``workspace/notes``. The encrypted credentials vault remains separate under
the ``secret_*`` tools and ``olano vault`` CLI.
"""

from olano.memory.notes_tools import *  # noqa: F403
from olano.memory.notes_tools import set_notes_path as set_vault_path  # noqa: F401
