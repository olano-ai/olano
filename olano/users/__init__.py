"""Local human accounts, sessions, and per-agent access control.

Backs the multi-tenant control layer: an owner/admin who configures the
platform and creates human users, with each agent owned by one human and
chat-accessible to a flat allow-list of others.
"""

from __future__ import annotations

from olano.users.db import DuplicateEmailError, User, UsersDB
from olano.users.store import UserStore, get_user_store, reset_user_store

__all__ = [
    "DuplicateEmailError",
    "User",
    "UserStore",
    "UsersDB",
    "get_user_store",
    "reset_user_store",
]
