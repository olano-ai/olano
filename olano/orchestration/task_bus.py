"""Task bus — structured task routing with IDs, priorities, and status tracking."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: str
    title: str
    description: str
    assigned_to: str  # Agent name
    requested_by: str  # Agent or user that created the task
    priority: int = 50  # 0–100
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "assigned_to": self.assigned_to,
            "requested_by": self.requested_by,
            "priority": self.priority,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class TaskBus:
    """Structured task routing between agents."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._tasks: dict[str, Task] = {}
        self._state_dir = state_dir
        self._queues: dict[str, asyncio.Queue] = {}

    def register_agent(self, agent_name: str) -> None:
        if agent_name not in self._queues:
            self._queues[agent_name] = asyncio.Queue()

    async def submit(
        self,
        title: str,
        description: str,
        assigned_to: str,
        requested_by: str,
        priority: int = 50,
        metadata: dict | None = None,
    ) -> Task:
        import uuid

        task = Task(
            id=str(uuid.uuid4())[:12],
            title=title,
            description=description,
            assigned_to=assigned_to,
            requested_by=requested_by,
            priority=priority,
            metadata=metadata or {},
        )
        self._tasks[task.id] = task

        q = self._queues.get(assigned_to)
        if q:
            await q.put(task)
        else:
            logger.warning("TaskBus: no queue for agent '%s'", assigned_to)

        logger.info("TaskBus: submitted task %s → %s: %s", task.id, assigned_to, title)
        return task

    async def next_task(self, agent_name: str, timeout: float = 0.1) -> Task | None:
        q = self._queues.get(agent_name)
        if not q:
            return None
        try:
            return await asyncio.wait_for(q.get(), timeout=timeout)
        except TimeoutError:
            return None

    def update_status(
        self, task_id: str, status: TaskStatus, result: str = "", error: str = ""
    ) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.status = status
        task.result = result
        task.error = error
        task.updated_at = datetime.utcnow()
        return True

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_tasks(
        self,
        agent: str | None = None,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        tasks = list(self._tasks.values())
        if agent:
            tasks = [t for t in tasks if t.assigned_to == agent]
        if status:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: t.priority, reverse=True)
