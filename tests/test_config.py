from unittest.mock import patch

import pytest

from app.config import load_settings


def settings_with(**values):
    environment = {key: str(value) for key, value in values.items()}
    with patch.dict("os.environ", environment, clear=True):
        return load_settings()


@pytest.mark.parametrize(("name", "value", "expected"), [
    ("LLM_MAX_CONCURRENCY", "nan", 8),
    ("LLM_MAX_CONCURRENCY", "1.5", 8),
    ("LLM_MAX_CONCURRENCY", "0", 8),
    ("LLM_CACHE_SIZE", "inf", 512),
    ("LLM_CACHE_SIZE", "-1", 512),
    ("LLM_TIMEOUT_SECONDS", "nan", 10.0),
    ("REQUEST_DEADLINE_SECONDS", "30", 25.0),
])
def test_invalid_numeric_settings_are_safe_errors(name, value, expected):
    settings = settings_with(**{name: value})
    field = {
        "LLM_MAX_CONCURRENCY": "llm_max_concurrency",
        "LLM_CACHE_SIZE": "cache_size",
        "LLM_TIMEOUT_SECONDS": "llm_timeout_seconds",
        "REQUEST_DEADLINE_SECONDS": "request_deadline_seconds",
    }[name]
    assert getattr(settings, field) == expected
    assert any(name in error for error in settings.config_errors)


def test_invalid_boolean_and_log_level_are_safe_errors():
    settings = settings_with(LLM_WARMUP="perhaps", LOG_LEVEL="verbose")
    assert settings.warmup is True
    assert settings.log_level == "INFO"
    assert len(settings.config_errors) == 2


def test_invalid_temperature_and_effort_are_safe_errors():
    settings = settings_with(
        LLM_PROVIDER="anthropic",
        LLM_MODEL="model",
        LLM_TEMPERATURE="nan",
        LLM_EFFORT="extreme",
    )
    assert settings.primary is not None
    assert settings.primary.temperature is None
    assert settings.primary.effort is None
    assert len(settings.config_errors) == 2


def test_valid_boundary_settings_are_preserved():
    settings = settings_with(
        REQUEST_DEADLINE_SECONDS="29",
        LLM_TIMEOUT_SECONDS="1",
        LLM_MAX_CONCURRENCY="100",
        LLM_CACHE_SIZE="0",
        LLM_WARMUP="off",
        LOG_LEVEL="warning",
    )
    assert settings.request_deadline_seconds == 29
    assert settings.llm_timeout_seconds == 1
    assert settings.llm_max_concurrency == 100
    assert settings.cache_size == 0
    assert settings.warmup is False
    assert settings.log_level == "WARNING"
    assert settings.config_errors == ()
