import asyncio
import json
import time

import pytest

from app.config import Settings
from app.llm.interpreter import InterpretationError, InterpretationResult, Interpreter
from app.pipeline import PipelineError, run_pipeline
from app.schemas import BatteryInput, Directive, HourInput, Scenario

GOOD_NO_OP = json.dumps({
    "interpretations": [{
        "note_index": 0,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Unrelated note.",
    }]
})


def scenario() -> Scenario:
    hours = tuple(HourInput(hour, 1.0, 0.0, 1.0) for hour in range(24))
    return Scenario(
        "deadline-test",
        ("The cafeteria menu changed.",),
        hours,
        BatteryInput(0.0, 0.0, 0.0, 0.0, 0.0),
    )


class QueueingProvider:
    label = "queue:test"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system, messages, schema, timeout):
        self.calls += 1
        await asyncio.sleep(0.25 if self.calls == 1 else 0.1)
        return GOOD_NO_OP

    async def aclose(self):
        pass


def test_llm_queue_wait_is_inside_absolute_deadline(monkeypatch):
    # Use small constants so the regression test completes quickly.
    monkeypatch.setattr("app.llm.interpreter.RESERVED_SECONDS", 0.02)
    monkeypatch.setattr("app.llm.interpreter.MIN_ATTEMPT_SECONDS", 0.01)
    settings = Settings(llm_timeout_seconds=1.0, llm_max_concurrency=1, warmup=False)
    provider = QueueingProvider()
    interpreter = Interpreter(settings, [provider])

    async def exercise():
        first = asyncio.create_task(
            interpreter.interpret(scenario(), time.monotonic() + 1.0, use_cache=False)
        )
        await asyncio.sleep(0.01)
        started = time.monotonic()
        with pytest.raises(InterpretationError):
            await interpreter.interpret(
                scenario(), time.monotonic() + 0.12, use_cache=False
            )
        elapsed = time.monotonic() - started
        await first
        return elapsed

    elapsed = asyncio.run(exercise())
    assert elapsed < 0.2
    assert provider.calls == 1  # the expired queued request never starts a provider call


class ImmediateInterpreter:
    async def interpret(self, scenario, deadline, **kwargs):
        directive = Directive(0, False, "no_op", None, "Unrelated note.")
        return InterpretationResult([directive], "test")


def test_optimizer_is_bounded_by_request_deadline(monkeypatch):
    def slow_solve(*args, **kwargs):
        time.sleep(0.25)
        return {}

    monkeypatch.setattr("app.pipeline.solve_and_build", slow_solve)
    settings = Settings(request_deadline_seconds=0.05, warmup=False)

    async def exercise():
        started = time.monotonic()
        with pytest.raises(PipelineError, match="deadline"):
            await run_pipeline(scenario(), ImmediateInterpreter(), settings)
        return time.monotonic() - started

    # asyncio.run waits for already-started executor jobs during loop shutdown, so measure the
    # request coroutine itself, as a persistent ASGI server does.
    elapsed = asyncio.run(exercise())
    assert elapsed < 0.2
