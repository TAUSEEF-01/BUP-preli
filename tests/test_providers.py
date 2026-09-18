import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.config import ProviderConfig
from app.llm.prompt import OUTPUT_SCHEMA
from app.llm.providers import AnthropicProvider, OpenAICompatProvider, ProviderError


def provider_config(provider="openai_compat", schema_mode="json_schema"):
    return ProviderConfig(
        provider=provider,
        model="test-model",
        api_key="test-key",
        base_url="https://provider.example/v1",
        temperature=0.0,
        effort=None,
        schema_mode=schema_mode,
    )


def run(coro):
    return asyncio.run(coro)


def test_openai_compat_sends_native_json_schema_and_parses_response():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    provider = OpenAICompatProvider(provider_config())
    run(provider._client.aclose())
    provider._client = httpx.AsyncClient(
        headers={"Authorization": "Bearer test-key"}, transport=httpx.MockTransport(handler)
    )
    text = run(provider.complete("system", [{"role": "user", "content": "input"}], OUTPUT_SCHEMA, 1))
    run(provider.aclose())

    assert text == '{"ok":true}'
    assert captured["request"].url == "https://provider.example/v1/chat/completions"
    assert captured["request"].headers["authorization"] == "Bearer test-key"
    response_format = captured["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == OUTPUT_SCHEMA


def test_openai_compat_json_object_mode_is_explicit():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OpenAICompatProvider(provider_config(schema_mode="json_object"))
    run(provider._client.aclose())
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    run(provider.complete("system", [], OUTPUT_SCHEMA, 1))
    run(provider.aclose())
    assert captured["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(("response", "category"), [
    (httpx.Response(429), "http_429"),
    (httpx.Response(200, json={"not_choices": []}), "malformed_response"),
    (httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}), "empty_response"),
])
def test_openai_compat_sanitizes_bad_responses(response, category):
    provider = OpenAICompatProvider(provider_config())
    run(provider._client.aclose())
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(ProviderError, match=category):
        run(provider.complete("system", [], OUTPUT_SCHEMA, 1))
    run(provider.aclose())


def test_openai_compat_sanitizes_transport_errors():
    def fail(request):
        raise httpx.ReadTimeout("secret provider detail", request=request)

    provider = OpenAICompatProvider(provider_config())
    run(provider._client.aclose())
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    with pytest.raises(ProviderError, match="timeout") as info:
        run(provider.complete("system", [], OUTPUT_SCHEMA, 1))
    assert "secret" not in str(info.value)
    run(provider.aclose())


class FakeAnthropicClient:
    def __init__(self):
        self.messages = self
        self.params = None
        self.timeout = None

    def with_options(self, timeout):
        self.timeout = timeout
        return self

    async def create(self, **params):
        self.params = params
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"ok":true}')],
        )

    async def close(self):
        pass


def test_anthropic_sends_native_json_schema():
    provider = AnthropicProvider(provider_config(provider="anthropic"))
    run(provider._client.close())
    fake = FakeAnthropicClient()
    provider._client = fake
    text = run(provider.complete("system", [{"role": "user", "content": "input"}], OUTPUT_SCHEMA, 2))
    assert text == '{"ok":true}'
    assert fake.timeout == 2
    assert fake.params["output_config"]["format"] == {
        "type": "json_schema", "schema": OUTPUT_SCHEMA
    }
