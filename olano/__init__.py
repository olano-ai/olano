"""Olano — multi-agent AI runtime powered by deepagents.

Copyright (c) 2026 Olano AI. Released under the MIT License; see the LICENSE
file at the repository root.
"""

from olano.env_aliases import apply_env_aliases as _apply_env_aliases

__version__ = "1.0.1"
__copyright__ = "Copyright (c) 2026 Olano AI"

# ``OLANO_*`` and ``LIONCLAW_*`` are two spellings of one variable. Mirror them
# here, before any reader runs, so the CLI, the runtime and the test suite all
# accept either. Stdlib-only; one pass over the environment.
_apply_env_aliases()
