"""Operator-note interpretation: LLM call, guardrails, one repair, optional backup model, cache.

Failure policy (plan §5.3): an invalid model result is rejected as a whole; the primary model
gets at most one repair call; an optional backup model gets one attempt with the original
task. If no complete valid interpretation is obtained, InterpretationError is raised and the
API returns a controlled 500. There is no rule-based fallback and no silent no_op.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.guardrails import GuardrailError, validate_interpretations
from app.llm.prompt import (
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_repair_message,
    build_user_message,
)
from app.llm.providers import LLMProvider, ProviderError, create_provider
from app.schemas import Directive, Scenario

logger = logging.getLogger("gridwise.llm")

# Time kept back for optimization, replay and serialization after the last LLM call.
RESERVED_SECONDS = 1.5
# Do not start an LLM attempt with less time than this.
MIN_ATTEMPT_SECONDS = 2.0


class InterpretationError(Exception):
    """No complete, valid LLM interpretation could be obtained within the deadline."""


@dataclass
class InterpretationResult:
    directives: list[Directive]
    source: str
    cache_hit: bool = False
    attempts: list[str] = field(default_factory=list)


def parse_json_text(text: str) -> Any:
    """Parse model text as JSON, tolerating code fences or prose around one JSON object."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except ValueError:
            pass
    raise GuardrailError(["The answer was not a valid JSON object."])


class Interpreter:
    def __init__(self, settings: Settings, providers: list[LLMProvider]):
        self._settings = settings
        self._providers = providers
        self._cache: OrderedDict[str, list[Directive]] = OrderedDict()
        self._semaphore = asyncio.Semaphore(settings.llm_max_concurrency)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Interpreter":
        providers: list[LLMProvider] = []
        for config in (settings.primary, settings.backup):
            if config is None:
                continue
            try:
                providers.append(create_provider(config))
            except Exception as exc:  # e.g. SDK rejects the configuration
                logger.error("could not create LLM provider %s: %s", config.label, type(exc).__name__)
        return cls(settings, providers)

    @property
    def configured(self) -> bool:
        return bool(self._providers)

    def _cache_key(self, scenario: Scenario) -> str:
        payload = {
            "prompt_version": PROMPT_VERSION,
            "providers": [p.label for p in self._providers],
            "notes": list(scenario.operator_notes),
            "capacity_kwh": scenario.battery.capacity_kwh,
            "minimum_energy_kwh": scenario.battery.minimum_energy_kwh,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def evict(self, scenario: Scenario) -> None:
        self._cache.pop(self._cache_key(scenario), None)

    def _remember(self, key: str, directives: list[Directive]) -> None:
        if self._settings.cache_size <= 0:
            return
        self._cache[key] = directives
        self._cache.move_to_end(key)
        while len(self._cache) > self._settings.cache_size:
            self._cache.popitem(last=False)

    async def _call(self, provider: LLMProvider, messages: list[dict[str, str]], deadline: float,
                    attempts: list[str]) -> str | None:
        remaining = deadline - time.monotonic() - RESERVED_SECONDS
        if remaining < MIN_ATTEMPT_SECONDS:
            attempts.append(f"{provider.label}:skipped_deadline")
            return None
        timeout = min(self._settings.llm_timeout_seconds, remaining)
        started = time.monotonic()
        try:
            async with self._semaphore:
                text = await asyncio.wait_for(
                    provider.complete(SYSTEM_PROMPT, messages, OUTPUT_SCHEMA, timeout), timeout + 0.5
                )
        except ProviderError as exc:
            attempts.append(f"{provider.label}:{exc}")
            return None
        except asyncio.TimeoutError:
            attempts.append(f"{provider.label}:timeout")
            return None
        except Exception as exc:  # never let a provider bug crash the request
            attempts.append(f"{provider.label}:{type(exc).__name__}")
            return None
        attempts.append(f"{provider.label}:ok:{(time.monotonic() - started) * 1000:.0f}ms")
        return text

    def _validate(self, text: str, scenario: Scenario) -> list[Directive]:
        data = parse_json_text(text)
        return validate_interpretations(data, len(scenario.operator_notes), scenario.battery.capacity_kwh)

    async def interpret(self, scenario: Scenario, deadline: float, *, use_cache: bool = True,
                        feedback: str | None = None, prefer_backup: bool = False) -> InterpretationResult:
        key = self._cache_key(scenario)
        if use_cache and key in self._cache:
            self._cache.move_to_end(key)
            return InterpretationResult(self._cache[key], source="cache", cache_hit=True)
        if not self._providers:
            raise InterpretationError("no LLM provider is configured")

        user_message = build_user_message(scenario)
        if feedback:
            user_message = f"{user_message}\n\n{feedback}"
        providers = list(self._providers)
        if prefer_backup and len(providers) > 1:
            providers.reverse()

        attempts: list[str] = []
        for position, provider in enumerate(providers):
            messages = [{"role": "user", "content": user_message}]
            text = await self._call(provider, messages, deadline, attempts)
            if text is None:
                continue  # provider failure: move on to the backup model
            try:
                directives = self._validate(text, scenario)
            except GuardrailError as err:
                attempts.append(f"{provider.label}:invalid({len(err.errors)})")
                if position != 0:
                    continue  # the backup model gets one attempt only
                repair = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": build_repair_message(err.errors, len(scenario.operator_notes))},
                ]
                repaired = await self._call(provider, repair, deadline, attempts)
                if repaired is None:
                    continue
                try:
                    directives = self._validate(repaired, scenario)
                except GuardrailError as repair_err:
                    attempts.append(f"{provider.label}:repair_invalid({len(repair_err.errors)})")
                    continue
            self._remember(key, directives)
            return InterpretationResult(directives, source=provider.label, attempts=attempts)

        logger.warning("interpretation failed after attempts: %s", attempts)
        raise InterpretationError("no valid interpretation: " + ", ".join(attempts))

    async def aclose(self) -> None:
        for provider in self._providers:
            try:
                await provider.aclose()
            except Exception:
                pass
