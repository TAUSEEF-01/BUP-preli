"""Deadline-aware orchestration: LLM interpretation -> guardrails -> constraints -> LP -> replay."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.directives import compile_limits
from app.llm.interpreter import Interpreter
from app.llm.prompt import INFEASIBLE_FEEDBACK
from app.optimizer import InfeasibleError, solve
from app.response_builder import build_response
from app.schemas import Directive, Scenario
from app.validator import replay

logger = logging.getLogger("gridwise.pipeline")

# Inward margin for the one numerical re-solve after a failed replay.
RETRY_MARGIN = 1e-4


class PipelineError(Exception):
    """A valid schedule could not be produced; the API answers with a controlled 500."""


@dataclass
class PipelineResult:
    response: dict[str, Any]
    source: str
    cache_hit: bool
    timings_ms: dict[str, float] = field(default_factory=dict)


async def _solve_before_deadline(
    scenario: Scenario, directives: list[Directive], deadline: float
) -> dict[str, Any]:
    """Run the blocking solver/replay without allowing the HTTP path to exceed its deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PipelineError("request deadline exhausted before optimization")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(solve_and_build, scenario, directives), timeout=remaining
        )
    except TimeoutError:
        # asyncio cannot stop a SciPy call already running in its worker thread, but the request
        # is released on time and the bounded 24-hour LP will finish independently.
        raise PipelineError("optimization exceeded the request deadline") from None


def solve_and_build(scenario: Scenario, directives: list[Directive]) -> dict[str, Any]:
    """Solve, serialize, and replay. Raises InfeasibleError or PipelineError."""
    limits = compile_limits(scenario, directives)
    for margin in (0.0, RETRY_MARGIN):
        solution = solve(scenario, limits, margin=margin)
        response = build_response(scenario, directives, limits, solution)
        violations = replay(scenario, directives, response)
        if not violations:
            return response
        logger.warning("replay failed (margin=%s): %s", margin, violations[:3])
    raise PipelineError("schedule failed independent replay")


async def run_pipeline(scenario: Scenario, interpreter: Interpreter, settings: Settings) -> PipelineResult:
    started = time.monotonic()
    deadline = started + settings.request_deadline_seconds
    timings: dict[str, float] = {}

    interpretation = await interpreter.interpret(scenario, deadline)
    timings["interpret"] = (time.monotonic() - started) * 1000

    solve_started = time.monotonic()
    try:
        response = await _solve_before_deadline(scenario, interpretation.directives, deadline)
    except InfeasibleError:
        # Organizer scenarios are feasible under the true directives, so this points at a
        # misreading. Never relax a directive; try one independent re-interpretation instead.
        interpreter.evict(scenario)
        logger.warning("compiled directives are infeasible; requesting one re-interpretation")
        retry = await interpreter.interpret(
            scenario, deadline, use_cache=False, feedback=INFEASIBLE_FEEDBACK, prefer_backup=True
        )
        same = [d.semantic_key() for d in retry.directives] == [
            d.semantic_key() for d in interpretation.directives
        ]
        if same:
            interpreter.evict(scenario)
            raise PipelineError("directives are infeasible") from None
        try:
            response = await _solve_before_deadline(scenario, retry.directives, deadline)
        except InfeasibleError:
            interpreter.evict(scenario)
            raise PipelineError("directives are infeasible after re-interpretation") from None
        interpretation = retry
    timings["optimize"] = (time.monotonic() - solve_started) * 1000
    timings["total"] = (time.monotonic() - started) * 1000
    return PipelineResult(response, interpretation.source, interpretation.cache_hit, timings)
