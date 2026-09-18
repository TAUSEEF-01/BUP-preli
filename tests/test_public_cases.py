"""End-to-end public regression through the HTTP API with a perfect fake model.

This checks guardrails -> directives -> optimizer -> serialization -> replay for all ten public
cases. Real-model accuracy is measured separately with scripts/eval_public.py.
"""
import json

import pytest

from app.guardrails import validate_interpretations
from app.request_validation import parse_scenario
from app.validator import replay
from tests.conftest import FakeProvider, ground_truth_handler, load_public_cases, make_client

CASES = load_public_cases()


@pytest.fixture(scope="module")
def client():
    with make_client(FakeProvider(handler=ground_truth_handler(CASES))) as test_client:
        yield test_client


def semantics(entries):
    return [(e["note_index"], e["applies"], e["directive_type"], e["structured_adjustment"]) for e in entries]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_public_case_end_to_end(client, case):
    response = client.post("/optimize-energy", json=case["input"])
    assert response.status_code == 200
    body = response.json()
    expected = case["expected_output"]

    assert semantics(body["directive_interpretation"]) == semantics(expected["directive_interpretation"])

    scenario = parse_scenario(json.dumps(case["input"]).encode())
    truth = validate_interpretations(
        {"interpretations": expected["directive_interpretation"]},
        len(scenario.operator_notes),
        scenario.battery.capacity_kwh,
    )
    assert replay(scenario, truth, body, tolerance=0.01) == []
    assert abs(body["total_cost_bdt"] - expected["total_cost_bdt"]) <= 0.01
