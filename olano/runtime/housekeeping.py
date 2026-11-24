"""Startup housekeeping: prune stale sessions, compress old ones, archive old memories."""

from __future__ import annotations

import datetime
import gzip
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HousekeepingReport:
    empty_sessions_deleted: int = 0
    sessions_compressed: int = 0
    memories_archived: int = 0
    degraded_log_rotated: bool = False
    rag_bytes: int = 0

    @property
    def actions_taken(self) -> bool:
        return bool(
            self.empty_sessions_deleted
            or self.sessions_compressed
            or self.memories_archived
            or self.degraded_log_rotated
        )

    def __str__(self) -> str:
        rag = f", rag={self.rag_bytes / 1_048_576:.1f} MB" if self.rag_bytes else ""
        return (
            f"✓ Housekeeping: {self.empty_sessions_deleted} empty sessions removed, "
            f"{self.sessions_compressed} old sessions compressed, "
            f"{self.memories_archived} memories archived{rag}."
        )


@dataclass
class HousekeepingConfig:
    enabled: bool = True
    empty_session_ttl_hours: int = 24
    session_compress_days: int = 180
    memory_archive_days: int = 365
    degraded_log_max_mb: float = 5.0


def _load_config(home: Path) -> HousekeepingConfig:
    config_path = home / "config" / "housekeeping.yaml"
    if not config_path.exists():
        return HousekeepingConfig()
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return HousekeepingConfig(
            **{k: v for k, v in raw.items() if k in HousekeepingConfig.__dataclass_fields__}
        )
    except Exception:
        return HousekeepingConfig()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _file_age_hours(path: Path) -> float:
    mtime = path.stat().st_mtime
    age = _utcnow().timestamp() - mtime
    return age / 3600


def _file_age_days(path: Path) -> float:
    return _file_age_hours(path) / 24


def _memory_file_age_days(path: Path) -> float | None:
    """Parse the date from a memories/YYYY-MM-DD.md filename."""
    try:
        stem = path.stem  # e.g. "2025-04-20"
        date = datetime.date.fromisoformat(stem)
        today = _utcnow().date()
        return (today - date).days
    except (ValueError, AttributeError):
        return None


def run_housekeeping(home: Path, config: HousekeepingConfig | None = None) -> HousekeepingReport:
    """
    Run all housekeeping rules. Returns a HousekeepingReport.

    Only prints output if actions_taken is True (caller is responsible for printing).
    """
    cfg = config or _load_config(home)
    if not cfg.enabled:
        return HousekeepingReport()

    report = HousekeepingReport()

    # ── Rule 1 & 2: Sessions ─────────────────────────────────────────────────
    sessions_root = home / "sessions"
    if sessions_root.exists():
        for agent_dir in sessions_root.iterdir():
            if not agent_dir.is_dir():
                continue
            for jsonl in agent_dir.glob("*.jsonl"):
                try:
                    size = jsonl.stat().st_size
                    age_hours = _file_age_hours(jsonl)
                    if size == 0 and age_hours >= cfg.empty_session_ttl_hours:
                        jsonl.unlink()
                        report.empty_sessions_deleted += 1
                    elif size > 0 and _file_age_days(jsonl) >= cfg.session_compress_days:
                        gz_path = jsonl.with_suffix(".jsonl.gz")
                        with jsonl.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)
                        jsonl.unlink()
                        report.sessions_compressed += 1
                except OSError:
                    pass

    # ── Rule 3: Memories ─────────────────────────────────────────────────────
    for agent_dir in home.glob("workspace/memories"):
        _prune_memories(agent_dir, cfg, report)
    # Also check direct memories/ under home (legacy)
    _prune_memories(home / "memories", cfg, report)
    # And agent-specific memories in agents/*/workspace/memories/
    for agent_ws in home.glob("agents/*/workspace/memories"):
        _prune_memories(agent_ws, cfg, report)

    # ── Rule 4: Degraded log ─────────────────────────────────────────────────
    degraded_log = home / "logs" / "degraded.log"
    if degraded_log.exists():
        size_mb = degraded_log.stat().st_size / 1_048_576
        if size_mb >= cfg.degraded_log_max_mb:
            ts = _utcnow().strftime("%Y%m%d-%H%M%S")
            rotated = degraded_log.parent / f"degraded.{ts}.log"
            shutil.move(str(degraded_log), str(rotated))
            degraded_log.touch()
            report.degraded_log_rotated = True

    # ── Rule 5: RAG size (surfaced only) ─────────────────────────────────────
    rag_dir = home / "rag"
    if rag_dir.exists():
        report.rag_bytes = sum(f.stat().st_size for f in rag_dir.rglob("*") if f.is_file())

    return report


def _prune_memories(
    memories_dir: Path, cfg: HousekeepingConfig, report: HousekeepingReport
) -> None:
    if not memories_dir.exists():
        return
    archive = memories_dir / "archive"
    for md in memories_dir.glob("????-??-??.md"):
        age_days = _memory_file_age_days(md)
        if age_days is not None and age_days >= cfg.memory_archive_days:
            year = md.stem[:4]
            dest_dir = archive / year
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(md), str(dest_dir / md.name))
            report.memories_archived += 1
