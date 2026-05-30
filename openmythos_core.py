#!/usr/bin/env python3
"""OpenMythos compatibility facade v3.0

Lightweight runtime shim for local ecommerce scripts.
Exposes the old import surface:

    from openmythos_core import RecurrentDepthTransformer, AdaptiveExecutor, MixtureOfExperts

Internally reuses mythos_router v3 (极速版).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

_SKILL_DIR = Path.home() / ".hermes" / "skills" / "mythos-agent-orchestration"
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from mythos_router import LoopedExecutor, MythosRouter, route_task, AsyncLoopedExecutor  # type: ignore


class RecurrentDepthTransformer:
    """Loop-style execution wrapper (v3: async support)."""

    def __init__(self, max_loops: int = 3, quality_threshold: float = 0.95, task_id: str = "openmythos_rdt"):
        self.executor = LoopedExecutor(
            max_loops=max_loops,
            quality_threshold=quality_threshold,
            task_id=task_id,
        )

    def run(self, task_fn: Callable, task_input: Any) -> Dict[str, Any]:
        return self.executor.run(task_fn, task_input)

    __call__ = run


class AdaptiveExecutor:
    """Facade around LoopedExecutor with async support."""

    def __init__(self, max_loops: int = 3, quality_threshold: float = 0.95, task_id: str = "openmythos_adaptive"):
        self.max_loops = max_loops
        self.quality_threshold = quality_threshold
        self.task_id = task_id

    def execute(self, task_fn: Callable, task_input: Any) -> Dict[str, Any]:
        executor = LoopedExecutor(
            max_loops=self.max_loops,
            quality_threshold=self.quality_threshold,
            task_id=self.task_id,
        )
        return executor.run(task_fn, task_input)

    async def execute_async(self, task_fn: Callable, task_input: Any) -> Dict[str, Any]:
        executor = AsyncLoopedExecutor(
            max_loops=self.max_loops,
            quality_threshold=self.quality_threshold,
            task_id=self.task_id,
        )
        return await executor.run(task_fn, task_input)

    def run(self, task_fn: Callable, task_input: Any) -> Dict[str, Any]:
        return self.execute(task_fn, task_input)


class MixtureOfExperts:
    """Task routing facade (v3: cached routing)."""

    def __init__(self, overload_threshold: int = 3):
        self.router = MythosRouter(overload_threshold=overload_threshold)

    def route(self, task: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.router.route(task, context=context)

    def batch_route(self, tasks: Iterable[str], base_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return [self.route(task, dict(base_context or {})) for task in tasks]


def demo_task(task_input: Any, loop_idx: int, state: Dict[str, Any]) -> Dict[str, Any]:
    quality = min(1.0, 0.55 + loop_idx * 0.2)
    verified = quality >= 0.95
    return {
        "quality": quality,
        "errors": 0,
        "verified": verified,
        "output": {"task_input": task_input, "loop": loop_idx},
        "state_update": {"last_loop": loop_idx},
    }


__all__ = [
    "AdaptiveExecutor",
    "AsyncLoopedExecutor",
    "MixtureOfExperts",
    "RecurrentDepthTransformer",
    "demo_task",
    "route_task",
]
