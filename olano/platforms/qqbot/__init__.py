"""
QQBot platform package.

Re-exports the main adapter symbols from ``adapter.py`` (the original
``qqbot.py``) so that **all existing import paths remain unchanged**::

    from olano.platforms.qqbot import QQAdapter  # works
    from olano.platforms.qqbot import check_qq_requirements  # works

New modules:
    - ``constants`` — shared constants (API URLs, timeouts, message types)
    - ``utils`` — User-Agent builder, config helpers
    - ``crypto`` — AES-256-GCM key generation and decryption
    - ``onboard`` — QR-code scan-to-configure flow
"""

# -- Adapter (original qqbot.py) ------------------------------------------
from .adapter import (
    QQAdapter,
    QQCloseError,
    _coerce_list,
    _ssrf_redirect_guard,
    check_qq_requirements,
)
from .crypto import decrypt_secret, generate_bind_key

# -- Onboard (QR-code scan-to-configure) -----------------------------------
from .onboard import (
    BindStatus,
    build_connect_url,
    create_bind_task,
    poll_bind_result,
)

# -- Utils -----------------------------------------------------------------
from .utils import build_user_agent, coerce_list, get_api_headers

__all__ = [
    # onboard
    "BindStatus",
    # adapter
    "QQAdapter",
    "QQCloseError",
    "_coerce_list",
    "_ssrf_redirect_guard",
    "build_connect_url",
    # utils
    "build_user_agent",
    "check_qq_requirements",
    "coerce_list",
    "create_bind_task",
    # crypto
    "decrypt_secret",
    "generate_bind_key",
    "get_api_headers",
    "poll_bind_result",
]
