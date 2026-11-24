"""Backward-compatible imports for built-in skill sync."""

from olano.skills.built_in_sync import (
    _LEGACY_SENTINEL,
    _PINNED,
    _SENTINEL,
    SyncReport,
    _builtin_source_dir,
    _dir_content_hash,
    _list_builtin_skills,
    sync_builtin_skills,
)

_bundle_source_dir = _builtin_source_dir
_list_bundled_skills = _list_builtin_skills
sync_bundled_skills = sync_builtin_skills

__all__ = [
    "_LEGACY_SENTINEL",
    "_PINNED",
    "_SENTINEL",
    "SyncReport",
    "_builtin_source_dir",
    "_bundle_source_dir",
    "_dir_content_hash",
    "_list_builtin_skills",
    "_list_bundled_skills",
    "sync_builtin_skills",
    "sync_bundled_skills",
]
