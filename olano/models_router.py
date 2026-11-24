"""Model Router — selects local vs cloud model based on task type and budget.

Routing rules (in priority order):
1. Security-sensitive tasks → always cloud (never local for safety-critical decisions)
2. Over daily budget → prefer local if available
3. Simple tasks (classify, summarize short text) → local if available
4. Complex reasoning, code gen, multi-step → cloud
5. Privacy-sensitive content → local preferred (if configured)
6. Default → use agent's configured model

Usage::

    router = ModelRouter(config)
    model_id = router.route(task_type="code_generation", text=user_message)
    # → "anthropic:claude-sonnet-4-6"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

# Task type → routing preference
_TASK_ROUTING: dict[str, str] = {
    # Always cloud
    "security_decision": "cloud",
    "hitl_approval": "cloud",
    "code_generation": "cloud",
    "multi_step_reasoning": "cloud",
    "agent_orchestration": "cloud",
    # Prefer local
    "summarization": "local",
    "classification": "local",
    "simple_qa": "local",
    "pii_redaction": "local",
    "health_data": "local",  # Privacy-sensitive
    # Neutral (use configured)
    "chat": "configured",
    "translation": "configured",
}

# Keywords that suggest a task type
_TASK_KEYWORDS: dict[str, list[str]] = {
    "code_generation": ["write code", "implement", "function", "class", "debug", "refactor", "```"],
    "summarization": ["summarize", "tldr", "brief", "in a few words", "key points"],
    "classification": ["classify", "categorize", "label", "sort", "which category"],
    "simple_qa": ["what is", "who is", "when is", "where is", "how many"],
    "health_data": ["health", "fitness", "sleep", "heart rate", "steps", "calories", "weight"],
    "security_decision": ["approve", "allow this", "is this safe", "trust level"],
    "multi_step_reasoning": ["analyze", "compare", "evaluate", "plan", "strategy", "break down"],
}


@dataclass
class RoutingDecision:
    model_id: str
    reason: str
    task_type: str = "chat"
    is_local: bool = False


class ModelRouter:
    """Routes model selection based on task type, budget, and privacy requirements."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._local_model: str = ""
        self._cloud_model: str = config.model
        self._detect_local_model()

    def _detect_local_model(self) -> None:
        """Find an available local model (Ollama preferred)."""
        from olano.models import is_provider_available, list_ollama_models

        if not is_provider_available("ollama"):
            return
        try:
            models = list_ollama_models()
            if models:
                # Prefer llama3 variants, then phi, then anything
                preferred = [m for m in models if "llama3" in m.lower()]
                self._local_model = preferred[0] if preferred else f"ollama:{models[0]}"
                if self._local_model and not self._local_model.startswith("ollama:"):
                    self._local_model = f"ollama:{self._local_model}"
        except Exception:
            pass

    def infer_task_type(self, text: str) -> str:
        """Infer task type from message text."""
        text_lower = text.lower()
        # Check keywords in priority order
        for task_type, keywords in _TASK_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return task_type
        return "chat"

    def route(
        self,
        task_type: str | None = None,
        text: str = "",
        force_local: bool = False,
        force_cloud: bool = False,
    ) -> RoutingDecision:
        """Select the best model for this task.

        Args:
            task_type: Explicit task type (from _TASK_ROUTING keys). If None, inferred from text.
            text: Message text to infer task type from.
            force_local: Override to always use local.
            force_cloud: Override to always use cloud.
        """
        if force_cloud:
            return RoutingDecision(
                model_id=self._cloud_model,
                reason="force_cloud override",
                is_local=False,
            )

        if force_local and self._local_model:
            return RoutingDecision(
                model_id=self._local_model,
                reason="force_local override",
                is_local=True,
            )

        if not task_type and text:
            task_type = self.infer_task_type(text)

        task_type = task_type or "chat"
        preference = _TASK_ROUTING.get(task_type, "configured")

        # Check budget — over limit → prefer local
        if self._is_over_budget():
            if self._local_model and preference != "cloud":
                return RoutingDecision(
                    model_id=self._local_model,
                    reason="budget_limit — routing to local",
                    task_type=task_type,
                    is_local=True,
                )
            logger.warning("ModelRouter: over budget but no local model available")

        # Privacy-sensitive data → prefer local
        if preference == "local" and self._local_model:
            return RoutingDecision(
                model_id=self._local_model,
                reason=f"task_type={task_type} prefers local",
                task_type=task_type,
                is_local=True,
            )

        # Cloud-preferred tasks or no local model available
        return RoutingDecision(
            model_id=self._cloud_model,
            reason=f"task_type={task_type} routed to cloud",
            task_type=task_type,
            is_local=False,
        )

    def _is_over_budget(self) -> bool:
        """Check if daily budget is exceeded."""
        if not self.config.budget.daily_usd:
            return False
        try:
            from olano.config.workspace import get_workspace
            from olano.pulse.cost_tracker import CostTracker

            workspace = get_workspace(self.config.workspace)
            tracker = CostTracker(self.config.name, workspace.state_dir, self.config.budget)
            return tracker.daily_usd() >= self.config.budget.daily_usd
        except Exception:
            return False

    def route_for_subtask(self, subtask: str) -> RoutingDecision:
        """Route for a specific named subtask (higher level than task_type)."""
        subtask_map: dict[str, str] = {
            "memory_distillation": "summarization",
            "skill_audit": "classification",
            "cartography": "classification",
            "pulse_generation": "summarization",
            "code_review": "code_generation",
            "debugging": "code_generation",
            "research": "multi_step_reasoning",
            "planning": "multi_step_reasoning",
        }
        task_type = subtask_map.get(subtask, "chat")
        return self.route(task_type=task_type)

    def summary(self) -> str:
        """Return a human-readable summary of routing configuration."""
        return (
            f"Cloud model: {self._cloud_model}\n"
            f"Local model: {self._local_model or 'none (Ollama not available)'}\n"
            f"Budget limit: {self.config.budget.daily_usd or 'unlimited'}/day\n"
        )
