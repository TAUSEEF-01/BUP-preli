"""Runtime configuration read from environment variables.

Secret values (API keys) are only held in memory and are never logged or returned.
"""
from __future__ import annotations

import os
import logging
import math
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    load_dotenv = None

SUPPORTED_PROVIDERS = ("anthropic", "openai_compat")


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    temperature: float | None
    effort: str | None
    schema_mode: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Settings:
    primary: ProviderConfig | None = None
    backup: ProviderConfig | None = None
    config_errors: tuple[str, ...] = ()
    request_deadline_seconds: float = 25.0
    llm_timeout_seconds: float = 10.0
    llm_max_concurrency: int = 8
    cache_size: int = 512
    warmup: bool = True
    log_level: str = "INFO"
    max_body_bytes: int = 1_000_000


def _get(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _number(
    name: str, default: float, errors: list[str], *, minimum: float | None = None,
    maximum: float | None = None
) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name} must be a number")
        return default
    if not math.isfinite(value):
        errors.append(f"{name} must be finite")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"{name} must be at least {minimum:g}")
        return default
    if maximum is not None and value > maximum:
        errors.append(f"{name} must be at most {maximum:g}")
        return default
    return value


def _integer(
    name: str, default: int, errors: list[str], *, minimum: int, maximum: int
) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer")
        return default
    if str(value) != raw and raw not in (f"+{value}", f"-{abs(value)}"):
        errors.append(f"{name} must be an integer")
        return default
    if not minimum <= value <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")
        return default
    return value


def _bool(name: str, default: bool, errors: list[str]) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    normalized = raw.lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    errors.append(f"{name} must be a boolean (true/false)")
    return default


def _provider(prefix: str, errors: list[str]) -> ProviderConfig | None:
    provider = _get(f"{prefix}_PROVIDER")
    model = _get(f"{prefix}_MODEL")
    if provider is None and model is None:
        return None
    if provider is None or model is None:
        errors.append(f"{prefix}_PROVIDER and {prefix}_MODEL must be set together")
        return None
    provider = provider.lower()
    if provider not in SUPPORTED_PROVIDERS:
        errors.append(f"{prefix}_PROVIDER must be one of {', '.join(SUPPORTED_PROVIDERS)}")
        return None

    # Newer Claude models reject sampling parameters, so Anthropic omits temperature
    # unless it is set explicitly. OpenAI-compatible APIs default to 0 for determinism.
    temperature_raw = _get(f"{prefix}_TEMPERATURE")
    if temperature_raw is None:
        temperature = 0.0 if provider == "openai_compat" else None
    elif temperature_raw.lower() == "none":
        temperature = None
    else:
        try:
            temperature = float(temperature_raw)
        except ValueError:
            errors.append(f"{prefix}_TEMPERATURE must be a number or 'none'")
            temperature = None
        else:
            if not math.isfinite(temperature):
                errors.append(f"{prefix}_TEMPERATURE must be finite or 'none'")
                temperature = None

    effort = _get(f"{prefix}_EFFORT")
    if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
        errors.append(f"{prefix}_EFFORT must be one of low, medium, high, xhigh, max")
        effort = None

    schema_mode = (_get(f"{prefix}_SCHEMA_MODE") or "json_schema").lower()
    if schema_mode not in ("json_schema", "json_object"):
        errors.append(f"{prefix}_SCHEMA_MODE must be json_schema or json_object")
        schema_mode = "json_schema"

    return ProviderConfig(
        provider=provider,
        model=model,
        api_key=_get(f"{prefix}_API_KEY"),
        base_url=_get(f"{prefix}_BASE_URL"),
        temperature=temperature,
        effort=effort,
        schema_mode=schema_mode,
    )


def load_settings() -> Settings:
    """Read settings from the environment (and a local .env file, if present)."""
    if load_dotenv is not None:
        load_dotenv(override=False)

    errors: list[str] = []
    primary = _provider("LLM", errors)
    backup = _provider("LLM_BACKUP", errors)
    deadline = _number(
        "REQUEST_DEADLINE_SECONDS", 25.0, errors, minimum=5.0, maximum=29.0
    )
    log_level = (_get("LOG_LEVEL") or "INFO").upper()
    if log_level not in logging.getLevelNamesMapping():
        errors.append("LOG_LEVEL must be a standard Python logging level")
        log_level = "INFO"
    timeout = _number(
        "LLM_TIMEOUT_SECONDS", 10.0, errors, minimum=1.0, maximum=29.0
    )
    concurrency = _integer(
        "LLM_MAX_CONCURRENCY", 8, errors, minimum=1, maximum=100
    )
    cache_size = _integer("LLM_CACHE_SIZE", 512, errors, minimum=0, maximum=100_000)
    warmup = _bool("LLM_WARMUP", True, errors)

    return Settings(
        primary=primary,
        backup=backup,
        config_errors=tuple(errors),
        request_deadline_seconds=deadline,
        llm_timeout_seconds=timeout,
        llm_max_concurrency=concurrency,
        cache_size=cache_size,
        warmup=warmup,
        log_level=log_level,
    )
