import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.interpreter import Interpreter
from app.llm.providers import ProviderError
from app.main import create_app
from app.schemas import HOURLY_PLAN_FIELDS, RESPONSE_FIELDS
from tests.conftest import FakeProvider, ground_truth_handler, load_public_cases, make_client

CASES = load_public_cases()
SAMPLE = CASES[0]
GOOD_ANSWER = json.dumps({"interpretations": SAMPLE["expected_output"]["directive_interpretation"]})
BAD_ANSWER = json.dumps({"interpretations": [
    {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [12, 13], "factor": 25}, "explanation": "x"},
    {"note_index": 1, "applies": False, "directive_type": "no_op", "structured_adjustment": None,
     "explanation": "x"},
]})
HUGE_NUMBER_ANSWER = (
    '{"interpretations":['
    '{"note_index":0,"applies":true,"directive_type":"solar_reduction",'
    '"structured_adjustment":{"hours":[12],"factor":1' + '0' * 400 + '},"explanation":"x"},'
    '{"note_index":1,"applies":false,"directive_type":"no_op",'
    '"structured_adjustment":null,"explanation":"x"}]}'
)


def post(client, payload):
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    return client.post("/optimize-energy", content=body, headers={"Content-Type": "application/json"})


def test_health():
    provider = FakeProvider(handler=ground_truth_handler(CASES))
    with make_client(provider) as client:
        response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_health_is_unready_without_an_llm():
    with make_client() as client:
        response = client.get("/health")
    assert response.status_code == 503 and response.json() == {"status": "error"}


def test_valid_request_returns_exact_schema():
    provider = FakeProvider(handler=ground_truth_handler(CASES))
    with make_client(provider) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 200
    body = response.json()
    assert list(body) == list(RESPONSE_FIELDS)
    assert body["scenario_id"] == SAMPLE["input"]["scenario_id"]
    assert [e["note_index"] for e in body["directive_interpretation"]] == [0, 1]
    assert len(body["hourly_plan"]) == 24
    assert all(list(row) == list(HOURLY_PLAN_FIELDS) for row in body["hourly_plan"])
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]


def test_repeated_request_uses_cache():
    provider = FakeProvider(handler=ground_truth_handler(CASES))
    with make_client(provider) as client:
        first = post(client, SAMPLE["input"])
        second = post(client, SAMPLE["input"])
    assert first.json() == second.json()
    assert len(provider.calls) == 1


def mutated(fn):
    payload = copy.deepcopy(SAMPLE["input"])
    fn(payload)
    return payload


@pytest.mark.parametrize("payload", [
    b"{not json",
    b"",
    b"[1, 2, 3]",
    b'"text"',
    mutated(lambda p: p.pop("battery")),
    mutated(lambda p: p.pop("operator_notes")),
    mutated(lambda p: p.update(scenario_id=5)),
    mutated(lambda p: p.update(operator_notes=[])),
    mutated(lambda p: p.update(operator_notes=["a", "b", "c", "d"])),
    mutated(lambda p: p.update(operator_notes=["   "])),
    mutated(lambda p: p.update(operator_notes="solar drops at noon")),
    mutated(lambda p: p.update(operator_notes=[42])),
    mutated(lambda p: p["hours"].pop()),
    mutated(lambda p: p["hours"][1].update(hour=0)),
    mutated(lambda p: p["hours"][1].update(hour=24)),
    mutated(lambda p: p["hours"][1].update(hour=True)),
    mutated(lambda p: p["hours"][1].update(hour=1.5)),
    mutated(lambda p: p["hours"][2].pop("tariff_bdt_per_kwh")),
    mutated(lambda p: p["hours"][2].update(demand_kwh="80")),
    mutated(lambda p: p["hours"][2].update(demand_kwh=True)),
    mutated(lambda p: p["hours"][2].update(demand_kwh=None)),
    mutated(lambda p: p["battery"].pop("capacity_kwh")),
    mutated(lambda p: p.update(battery=[1, 2])),
    mutated(lambda p: p.update(hours={"0": {}})),
])
def test_structural_errors_return_400(payload):
    with make_client() as client:
        response = post(client, payload)
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_non_finite_numbers_return_400():
    raw = json.dumps(SAMPLE["input"]).replace('"demand_kwh": 90', '"demand_kwh": NaN', 1)
    assert "NaN" in raw
    with make_client() as client:
        assert post(client, raw).status_code == 400
        assert post(client, raw.replace("NaN", "1e999")).status_code == 400


@pytest.mark.parametrize("payload", [
    mutated(lambda p: p["hours"][3].update(demand_kwh=-1)),
    mutated(lambda p: p["hours"][3].update(solar_kwh=-1)),
    mutated(lambda p: p["hours"][3].update(tariff_bdt_per_kwh=-1)),
    mutated(lambda p: p["battery"].update(max_charge_kwh_per_hour=-1)),
    mutated(lambda p: p["battery"].update(minimum_energy_kwh=300)),
    mutated(lambda p: p["battery"].update(initial_energy_kwh=10)),
    mutated(lambda p: p["battery"].update(initial_energy_kwh=500)),
])
def test_semantic_errors_return_422(payload):
    with make_client() as client:
        response = post(client, payload)
    assert response.status_code == 422
    assert response.json()["error"] == "unprocessable_entity"


@pytest.mark.parametrize("mutator", [
    lambda p: p["hours"][0].update(demand_kwh=1e20),
    lambda p: p["hours"][0].update(tariff_bdt_per_kwh=1e20),
    lambda p: p["battery"].update(capacity_kwh=1e20, initial_energy_kwh=1e20),
    lambda p: [h.update(demand_kwh=1e11, tariff_bdt_per_kwh=1e11) for h in p["hours"]],
])
def test_numerically_unsafe_finite_inputs_return_controlled_422(mutator):
    payload = mutated(mutator)
    with make_client() as client:
        response = post(client, payload)
    assert response.status_code == 422
    assert "numeric range" in response.json()["message"]


def test_large_but_precision_safe_battery_input_is_accepted_structurally():
    payload = mutated(lambda p: p["battery"].update(
        capacity_kwh=1e10,
        initial_energy_kwh=1e9,
        minimum_energy_kwh=1e8,
        max_charge_kwh_per_hour=0,
        max_discharge_kwh_per_hour=0,
    ))
    # No provider is configured, so reaching the LLM-stage 500 proves request validation passed.
    with make_client() as client:
        response = post(client, payload)
    assert response.status_code == 500


def test_unsorted_hours_and_extra_fields_are_accepted():
    payload = mutated(lambda p: (p["hours"].reverse(), p.update(extra="ignored")))
    provider = FakeProvider(handler=ground_truth_handler(CASES))
    with make_client(provider) as client:
        response = post(client, payload)
    assert response.status_code == 200
    assert [row["hour"] for row in response.json()["hourly_plan"]] == list(range(24))


def test_no_configured_llm_is_a_controlled_500():
    with make_client() as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error",
                               "message": "The request could not be processed. Please retry later."}


def test_successful_warmup_precedes_readiness():
    answer = json.dumps({"interpretations": [{
        "note_index": 0,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Unrelated note.",
    }]})
    provider = FakeProvider(responses=[answer])
    with make_client(provider, warmup=True) as client:
        assert client.get("/health").status_code == 200
    assert len(provider.calls) == 1


def test_failed_warmup_keeps_service_unready():
    provider = FakeProvider(responses=[ProviderError("http_401")])
    with make_client(provider, warmup=True) as client:
        response = client.get("/health")
    assert response.status_code == 503 and response.json() == {"status": "error"}


def test_provider_failure_without_backup_is_a_controlled_500_not_a_fallback():
    provider = FakeProvider(responses=[ProviderError("http_503")])
    with make_client(provider) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 500
    assert "503" not in response.text and "Traceback" not in response.text


def test_invalid_output_is_repaired_once():
    provider = FakeProvider(responses=[BAD_ANSWER, GOOD_ANSWER])
    with make_client(provider) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 200
    assert len(provider.calls) == 2
    repair_request = provider.calls[1][-1]["content"]
    assert "factor" in repair_request and "failed validation" in repair_request


def test_oversized_model_number_uses_normal_repair_path():
    provider = FakeProvider(responses=[HUGE_NUMBER_ANSWER, GOOD_ANSWER])
    with make_client(provider) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 200
    assert len(provider.calls) == 2


def test_invalid_output_twice_is_a_500_and_never_becomes_no_op():
    provider = FakeProvider(responses=[BAD_ANSWER, BAD_ANSWER])
    with make_client(provider) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 500
    assert len(provider.calls) == 2


def test_backup_model_used_when_primary_fails():
    primary = FakeProvider("primary:m", responses=[ProviderError("timeout")])
    backup = FakeProvider("backup:m", handler=ground_truth_handler(CASES))
    with make_client(primary, backup) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 200
    assert len(primary.calls) == 1 and len(backup.calls) == 1


def test_infeasible_interpretation_triggers_one_reinterpretation():
    impossible = json.dumps({"interpretations": [
        {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [0], "minimum_energy_kwh": 220}, "explanation": "x"},
        {"note_index": 1, "applies": False, "directive_type": "no_op", "structured_adjustment": None,
         "explanation": "x"},
    ]})
    provider = FakeProvider(responses=[impossible, GOOD_ANSWER])
    with make_client(provider) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 200
    assert response.json()["directive_interpretation"][0]["directive_type"] == "solar_reduction"


def test_repeated_infeasible_interpretation_is_a_500():
    impossible = json.dumps({"interpretations": [
        {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [0], "minimum_energy_kwh": 220}, "explanation": "x"},
        {"note_index": 1, "applies": False, "directive_type": "no_op", "structured_adjustment": None,
         "explanation": "x"},
    ]})
    provider = FakeProvider(responses=[impossible, impossible])
    with make_client(provider) as client:
        assert post(client, SAMPLE["input"]).status_code == 500


def test_unknown_route_and_method_return_json():
    with make_client() as client:
        assert client.get("/nope").json()["error"] == "not_found"
        assert client.get("/optimize-energy").json()["error"] == "method_not_allowed"


def test_unexpected_error_is_generic_and_does_not_log_a_traceback(monkeypatch, caplog):
    async def fail(*args, **kwargs):
        raise RuntimeError("internal detail")

    monkeypatch.setattr("app.main.run_pipeline", fail)
    provider = FakeProvider(handler=ground_truth_handler(CASES))
    settings = Settings(warmup=False)
    interpreter = Interpreter(settings, [provider])
    with TestClient(
        create_app(settings, interpreter), raise_server_exceptions=False
    ) as client:
        response = post(client, SAMPLE["input"])
    assert response.status_code == 500
    assert response.json()["message"] == "The request could not be processed. Please retry later."
    assert "unhandled error: RuntimeError" in caplog.text
    assert "Traceback" not in caplog.text
    assert "internal detail" not in caplog.text
