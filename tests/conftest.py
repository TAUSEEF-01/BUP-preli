"""Shared fixtures. Tests never call a real LLM: FakeProvider stands in for the model."""
from __future__ import annotations

import copy
import html
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.interpreter import Interpreter
from app.main import create_app
from app.request_validation import parse_scenario

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CASES_PATH = ROOT / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
NOTE_PATTERN = re.compile(r'<note index="(\d+)">(.*?)</note>', re.S)


def load_public_cases() -> list[dict]:
    return json.loads(PUBLIC_CASES_PATH.read_text(encoding="utf-8"))["cases"]


def notes_from_messages(messages: list[dict]) -> tuple[str, ...]:
    first_user = messages[0]["content"]
    return tuple(html.unescape(text) for _, text in NOTE_PATTERN.findall(first_user))


class FakeProvider:
    """Scripted stand-in for an LLM provider.

    `responses` is consumed in order (strings are returned, exceptions are raised);
    `handler(messages)` computes a response instead.
    """

    def __init__(self, label: str = "fake:model", responses=None, handler=None):
        self.label = label
        self.calls: list[list[dict]] = []
        self._responses = list(responses or [])
        self._handler = handler

    async def complete(self, system, messages, schema, timeout):
        self.calls.append(messages)
        if self._handler is not None:
            return self._handler(messages)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


def ground_truth_handler(cases: list[dict]):
    """Answer like a perfect model for the public cases (test-only lookup by note text)."""
    table = {
        tuple(case["input"]["operator_notes"]): case["expected_output"]["directive_interpretation"]
        for case in cases
    }

    def handler(messages):
        return json.dumps({"interpretations": table[notes_from_messages(messages)]})

    return handler


def test_settings(**overrides) -> Settings:
    values = {"warmup": False, "request_deadline_seconds": 25.0, "llm_timeout_seconds": 5.0}
    values.update(overrides)
    return Settings(**values)


def make_client(*providers, **setting_overrides) -> TestClient:
    settings = test_settings(**setting_overrides)
    interpreter = Interpreter(settings, list(providers))
    return TestClient(create_app(settings, interpreter))


@pytest.fixture(scope="session")
def public_cases() -> list[dict]:
    return load_public_cases()


@pytest.fixture
def sample_request(public_cases) -> dict:
    return copy.deepcopy(public_cases[0]["input"])


@pytest.fixture
def sample_scenario(sample_request):
    return parse_scenario(json.dumps(sample_request).encode())
