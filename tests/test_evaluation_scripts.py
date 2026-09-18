import json
import sys
from unittest.mock import patch

from scripts import eval_paraphrases, eval_public
from tests.conftest import load_public_cases


def test_paraphrase_evaluator_fails_when_no_llm_is_configured(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["eval_paraphrases.py"])
    with patch.dict("os.environ", {}, clear=True):
        assert eval_paraphrases.main() == 2


def test_paraphrase_evaluator_fails_when_selection_is_empty(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["eval_paraphrases.py", "--only", "missing"])
    assert eval_paraphrases.main() == 2


def test_interpretation_comparison_handles_malformed_numeric_value():
    expected = [{
        "note_index": 0,
        "applies": True,
        "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [1], "factor": 0.5},
    }]
    got = [{**expected[0], "structured_adjustment": {"hours": [1], "factor": {}}}]
    assert eval_public.interpretation_errors(got, expected) == ["note 0: factor is not numeric"]


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    def __init__(self, *, health, posts, **kwargs):
        self.health = health
        self.posts = iter(posts)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url):
        return self.health

    def post(self, url, json):
        return next(self.posts)


def run_public_main(monkeypatch, tmp_path, *, health, post):
    case = load_public_cases()[0]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps({"cases": [case]}), encoding="utf-8")
    monkeypatch.setattr(
        eval_public.httpx,
        "Client",
        lambda **kwargs: FakeClient(health=health, posts=[post]),
    )
    monkeypatch.setattr(
        sys, "argv", ["eval_public.py", "--cases", str(cases_file)]
    )
    return eval_public.main()


def test_public_evaluator_fails_bad_health_even_if_post_is_non_200(monkeypatch, tmp_path):
    result = run_public_main(
        monkeypatch,
        tmp_path,
        health=FakeResponse(503, {"status": "error"}, '{"status":"error"}'),
        post=FakeResponse(500, {}, "error"),
    )
    assert result == 1


def test_public_evaluator_counts_invalid_json_instead_of_crashing(monkeypatch, tmp_path):
    result = run_public_main(
        monkeypatch,
        tmp_path,
        health=FakeResponse(200, {"status": "ok"}, '{"status":"ok"}'),
        post=FakeResponse(200, ValueError("bad json"), "not-json"),
    )
    assert result == 1
