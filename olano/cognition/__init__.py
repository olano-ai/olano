"""Olano cognition — background reasoning workers.

This package turns the previously single-prompt "pulse" and "self-learning"
features into real reasoning agents (headless deepagents) that read files,
research the web, run tools, and produce concrete artifacts. A FAST/DEEP
:mod:`~olano.cognition.router` keeps routine work cheap while escalating
high-value work to a full :func:`~olano.cognition.worker.run_worker` run.
"""

from __future__ import annotations

from olano.cognition.worker import WorkerOutcome, run_worker

__all__ = ["WorkerOutcome", "run_worker"]
