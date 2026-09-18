"""LLM provider adapters.

- AnthropicProvider uses the official `anthropic` SDK with native structured output.
- OpenAICompatProvider speaks the widely shared /chat/completions REST contract (OpenAI, Groq,
  Gemini's OpenAI-compatible endpoint, OpenRouter, local servers) in JSON mode.

Failures surface as ProviderError with a short category only; provider payloads, prompts and
credentials are never included.
"""
from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.config import ProviderConfig

ANTHROPIC_MAX_TOKENS = 8192


class ProviderError(Exception):
    """Sanitized provider failure. The message is a short category such as 'timeout'."""


class LLMProvider(Protocol):
    label: str

    async def complete(self, system: str, messages: list[dict[str, str]], schema: dict[str, Any],
                       timeout: float) -> str: ...

    async def aclose(self) -> None: ...


class AnthropicProvider:
    def __init__(self, config: ProviderConfig):
        import anthropic

        self._anthropic = anthropic
        self._config = config
        self.label = config.label
        kwargs: dict[str, Any] = {"max_retries": 0}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def complete(self, system: str, messages: list[dict[str, str]], schema: dict[str, Any],
                       timeout: float) -> str:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self._config.effort:
            output_config["effort"] = self._config.effort
        params: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": system,
            "messages": messages,
            "output_config": output_config,
        }
        if self._config.temperature is not None:
            params["temperature"] = self._config.temperature

        anthropic = self._anthropic
        try:
            response = await self._client.with_options(timeout=timeout).messages.create(**params)
        except anthropic.APITimeoutError:
            raise ProviderError("timeout") from None
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"http_{exc.status_code}") from None
        except anthropic.APIConnectionError:
            raise ProviderError("connection") from None
        except anthropic.AnthropicError:
            raise ProviderError("client_error") from None

        if response.stop_reason in ("refusal", "max_tokens"):
            raise ProviderError(f"stop_{response.stop_reason}")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise ProviderError("empty_response")
        return text

    async def aclose(self) -> None:
        await self._client.close()


class OpenAICompatProvider:
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(self, config: ProviderConfig):
        self._config = config
        self.label = config.label
        self._url = (config.base_url or self.DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.AsyncClient(headers=headers)

    async def complete(self, system: str, messages: list[dict[str, str]], schema: dict[str, Any],
                       timeout: float) -> str:
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if self._config.schema_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "gridwise_directives",
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            # Compatibility mode for models that implement JSON mode but not JSON Schema.
            # Deterministic guardrails still validate the complete returned object.
            body["response_format"] = {"type": "json_object"}
        if self._config.temperature is not None:
            body["temperature"] = self._config.temperature
        try:
            response = await self._client.post(self._url, json=body, timeout=timeout)
        except httpx.TimeoutException:
            raise ProviderError("timeout") from None
        except httpx.HTTPError:
            raise ProviderError("connection") from None
        if response.status_code != 200:
            raise ProviderError(f"http_{response.status_code}")
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("malformed_response") from None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("empty_response")
        return text

    async def aclose(self) -> None:
        await self._client.aclose()


def create_provider(config: ProviderConfig) -> LLMProvider:
    if config.provider == "anthropic":
        return AnthropicProvider(config)
    if config.provider == "openai_compat":
        return OpenAICompatProvider(config)
    raise ValueError(f"unsupported provider {config.provider}")
