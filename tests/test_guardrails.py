import pytest

from app.guardrails import GuardrailError, validate_interpretations
from app.llm.interpreter import parse_json_text

CAPACITY = 200.0


def entry(index, kind, adjustment, applies=None, explanation="ok"):
    return {
        "note_index": index,
        "applies": (kind != "no_op") if applies is None else applies,
        "directive_type": kind,
        "structured_adjustment": adjustment,
        "explanation": explanation,
    }


def check(entries, count=None):
    return validate_interpretations({"interpretations": entries}, count or len(entries), CAPACITY)


def rejects(entries, count=None, fragment=""):
    with pytest.raises(GuardrailError) as info:
        check(entries, count)
    assert any(fragment in e for e in info.value.errors), info.value.errors


def test_accepts_every_directive_shape():
    directives = check([
        entry(0, "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
        entry(1, "minimum_battery_reserve", {"hours": [18, 19, 20], "minimum_energy_kwh": 100}),
        entry(2, "no_op", None),
    ])
    assert [d.directive_type for d in directives] == ["solar_reduction", "minimum_battery_reserve", "no_op"]
    assert directives[2].applies is False and directives[2].structured_adjustment is None
    for kind, adjustment in [
        ("no_charge_window", {"hours": [2, 3]}),
        ("no_discharge_window", {"hours": [18]}),
        ("max_grid_window", {"hours": [19], "max_grid_kwh": 0}),
    ]:
        assert check([entry(0, kind, adjustment)])[0].structured_adjustment == adjustment


def test_factor_one_stays_solar_reduction():
    directive = check([entry(0, "solar_reduction", {"hours": [12], "factor": 1.0})])[0]
    assert directive.directive_type == "solar_reduction" and directive.applies is True


def test_reserve_below_base_minimum_is_kept():
    directive = check([entry(0, "minimum_battery_reserve", {"hours": [5], "minimum_energy_kwh": 1})])[0]
    assert directive.structured_adjustment["minimum_energy_kwh"] == 1


def test_hours_are_sorted_and_deduplicated_without_changing_meaning():
    directive = check([entry(0, "no_charge_window", {"hours": [15, 14, 14]})])[0]
    assert directive.structured_adjustment["hours"] == [14, 15]


def test_entries_are_returned_in_note_order():
    directives = check([entry(1, "no_op", None), entry(0, "no_charge_window", {"hours": [1]})])
    assert [d.note_index for d in directives] == [0, 1]


def test_missing_explanation_gets_a_neutral_default():
    directive = check([entry(0, "no_op", None, explanation="")])[0]
    assert directive.explanation


@pytest.mark.parametrize("entries,count,fragment", [
    ([entry(0, "no_op", None)], 2, "Expected exactly 2"),
    ([entry(0, "no_op", None), entry(0, "no_op", None)], 2, "more than once"),
    ([entry(5, "no_op", None)], 1, "note_index must be"),
    ([entry(0, "demand_increase", {"hours": [1]})], 1, "directive_type"),
    ([entry(0, "no_op", None, applies=True)], 1, "applies = false"),
    ([entry(0, "no_op", {"hours": [1]}, applies=False)], 1, "null"),
    ([entry(0, "no_charge_window", {"hours": [1]}, applies=False)], 1, "applies = true"),
    ([entry(0, "no_charge_window", None, applies=True)], 1, "structured_adjustment object"),
    ([entry(0, "no_charge_window", {"hours": [1], "factor": 0.5})], 1, "exactly the keys"),
    ([entry(0, "solar_reduction", {"hours": [1]})], 1, "exactly the keys"),
    ([entry(0, "no_charge_window", {"hours": []})], 1, "non-empty"),
    ([entry(0, "no_charge_window", {"hours": [24]})], 1, "0 to 23"),
    ([entry(0, "no_charge_window", {"hours": [-1]})], 1, "0 to 23"),
    ([entry(0, "no_charge_window", {"hours": [True]})], 1, "0 to 23"),
    ([entry(0, "no_charge_window", {"hours": [1.5]})], 1, "0 to 23"),
    ([entry(0, "solar_reduction", {"hours": [1], "factor": 1.2})], 1, "factor"),
    ([entry(0, "solar_reduction", {"hours": [1], "factor": -0.1})], 1, "factor"),
    ([entry(0, "solar_reduction", {"hours": [1], "factor": "0.2"})], 1, "factor"),
    ([entry(0, "minimum_battery_reserve", {"hours": [1], "minimum_energy_kwh": 250})], 1, "capacity"),
    ([entry(0, "minimum_battery_reserve", {"hours": [1], "minimum_energy_kwh": -5})], 1, "non-negative"),
    ([entry(0, "max_grid_window", {"hours": [1], "max_grid_kwh": -1})], 1, "non-negative"),
    ([entry(0, "max_grid_window", {"hours": [1], "max_grid_kwh": float("inf")})], 1, "finite"),
    ([entry(0, "max_grid_window", {"hours": [1], "max_grid_kwh": False})], 1, "finite"),
])
def test_rejections(entries, count, fragment):
    rejects(entries, count, fragment)


def test_whole_result_is_rejected_when_one_entry_is_invalid():
    with pytest.raises(GuardrailError):
        check([entry(0, "no_op", None), entry(1, "solar_reduction", {"hours": [1], "factor": 2})])


@pytest.mark.parametrize(("kind", "key"), [
    ("solar_reduction", "factor"),
    ("minimum_battery_reserve", "minimum_energy_kwh"),
    ("max_grid_window", "max_grid_kwh"),
])
def test_oversized_model_integer_is_a_guardrail_error(kind, key):
    huge = 10 ** 400
    rejects([entry(0, kind, {"hours": [1], key: huge})], fragment="finite")


def test_non_object_output_is_rejected():
    with pytest.raises(GuardrailError):
        validate_interpretations([entry(0, "no_op", None)], 1, CAPACITY)


def test_json_text_parsing_tolerates_fences_and_rejects_garbage():
    assert parse_json_text('```json\n{"interpretations": []}\n```') == {"interpretations": []}
    assert parse_json_text('Here you go: {"a": 1} done') == {"a": 1}
    with pytest.raises(GuardrailError):
        parse_json_text("not json at all")
