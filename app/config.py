"""Runtime configuration read from environment variables.

Secret values (API keys) are only held in memory and are never logged or returned.
"""
from __future__ import annotations

import os
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


def _number(name: str, default: float, errors: list[str]) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{name} must be a number")
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


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

    return ProviderConfig(
        provider=provider,
        model=model,
        api_key=_get(f"{prefix}_API_KEY"),
        base_url=_get(f"{prefix}_BASE_URL"),
        temperature=temperature,
        effort=_get(f"{prefix}_EFFORT"),
    )


def load_settings() -> Settings:
    """Read settings from the environment (and a local .env file, if present)."""
    if load_dotenv is not None:
        load_dotenv(override=False)

    errors: list[str] = []
    primary = _provider("LLM", errors)
    backup = _provider("LLM_BACKUP", errors)
    deadline = _number("REQUEST_DEADLINE_SECONDS", 25.0, errors)
    if not 5.0 <= deadline <= 29.0:
        errors.append("REQUEST_DEADLINE_SECONDS must be between 5 and 29")
        deadline = 25.0

    return Settings(
        primary=primary,
        backup=backup,
        config_errors=tuple(errors),
        request_deadline_seconds=deadline,
        llm_timeout_seconds=max(1.0, _number("LLM_TIMEOUT_SECONDS", 10.0, errors)),
        llm_max_concurrency=max(1, int(_number("LLM_MAX_CONCURRENCY", 8, errors))),
        cache_size=max(0, int(_number("LLM_CACHE_SIZE", 512, errors))),
        warmup=_bool("LLM_WARMUP", True),
        log_level=(_get("LOG_LEVEL") or "INFO").upper(),
    )
