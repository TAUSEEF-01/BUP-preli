# GridWise LLM

BUP CSE Fest 2026 Hackathon · Online Preliminary · Smart Campus Energy Optimization Challenge

A JSON HTTP service that reads campus operators' natural-language notes with a language model,
validates the interpretation deterministically, applies every relevant directive as a hard
constraint, and returns the minimum-cost valid 24-hour grid / solar / battery schedule.

| Item | Value |
|---|---|
| Health endpoint | `GET /health` → `{"status":"ok"}` |
| Main endpoint | `POST /optimize-energy` |
| Public base URL | **TODO before submission:** `https://…` |
| Docker image | **TODO before submission:** `docker.io/<user>/gridwise-llm:1.0.0` (digest `sha256:…`) |
| LLM used for judging | **TODO before submission:** provider + exact model identifier |
| Optimizer | Linear program solved with HiGHS (SciPy `linprog`) |
| Port | `8000` (override with `PORT`) |

---

## 1. Architecture

```text
POST /optimize-energy
  1. Request validation        400 malformed/structural · 422 semantic
  2. LLM interpretation        one structured-output call for all notes
  3. Deterministic guardrails  types, note mapping, hours, ranges, applies/null rules
     └─ invalid → one repair call → optional backup LLM → controlled 500
  4. Directive compilation     per-hour solar cap, reserve floor, charge/discharge caps, grid cap
  5. Linear program (HiGHS)    minimum grid cost; tie-break removes pointless battery cycling
     └─ infeasible → one independent re-interpretation → otherwise controlled 500
  6. Response builder          actions, rounding, totals, plan_summary
  7. Independent replay        every organizer rule re-checked before the response is sent
```

**The LLM's role (mandatory).** The language model is the only interpreter of `operator_notes`.
It returns, for every note, the final `directive_type`, `applies`, `structured_adjustment`
(exact hours and values) and `explanation`. Deterministic code never guesses a meaning. It only
checks the model's answer and compiles the accepted directives into constraints. There is no
rule-based fallback. If no model produces a valid interpretation in time, the request fails
with a controlled 500 rather than bypassing the LLM or relabelling a note as `no_op`.

### Guardrails (`app/guardrails.py`)

Model output is untrusted until all of these pass. Otherwise the whole result is rejected and
the errors are sent back to the model once for repair:

- exactly one entry per note, `note_index` 0..N-1, no missing or duplicate indices;
- `directive_type` is one of the six supported types;
- `no_op` ⇔ `applies = false` and `structured_adjustment = null`; every other type has `applies = true`;
- `structured_adjustment` has exactly the keys of its type;
- hours are integers 0–23 (sorted and de-duplicated, which cannot change meaning), non-empty;
- `factor` finite in [0, 1]; `minimum_energy_kwh` finite, ≥ 0 and ≤ battery capacity; `max_grid_kwh` finite and ≥ 0;
- the model has no field that could change demand, solar, tariff or battery parameters.

### Optimizer (`app/optimizer.py`)

Per hour: grid `g`, solar used `s`, charge `c`, discharge `d` (all ≥ 0) and battery energy `E`.

```text
g + s + d = demand + c                      energy balance
E[h] = E[h-1] + c - d, E[-1] = initial      battery transition
reserve_floor[h] <= E[h] <= capacity        base minimum and directive reserves (max)
E[23] = initial                             end-of-day neutrality
s <= solar * factor                         smallest active factor per hour
c <= rate (0 in no_charge hours), d <= rate (0 in no_discharge hours), g <= grid cap
minimize sum(g * tariff)
```

A second solve keeps the optimal cost and minimizes total charge + discharge, which removes
degenerate cycling such as simultaneous charge and discharge. Battery energy is rounded first
and all other hourly values are derived from it, so transitions, energy balance and totals are
consistent in the emitted JSON. `app/validator.py` then replays the response independently
(stricter than the published 0.01 tolerance). On a numerical failure it re-solves once with
small inward margins, and otherwise returns a controlled 500. It never returns an invalid plan.

---

## 2. Local quickstart

Requirements: Python 3.12 and an API key for a supported LLM provider.

```bash
git clone <repository-url> gridwise-llm
cd gridwise-llm
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # Windows: copy .env.example .env
# edit .env: set LLM_PROVIDER, LLM_MODEL, LLM_API_KEY (and LLM_BASE_URL for openai_compat)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Check the service from a second terminal:

```bash
curl http://localhost:8000/health
# {"status":"ok"}

python scripts/eval_public.py --base-url http://localhost:8000
```

One manual request (the public sample file holds the request under `cases[i].input`):

```bash
python -c "import json; print(json.dumps(json.load(open('BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json', encoding='utf-8'))['cases'][0]['input']))" > sample_request.json
curl -X POST http://localhost:8000/optimize-energy -H "Content-Type: application/json" --data-binary @sample_request.json
```

---

## 3. Configuration

Set these as environment variables or in a local `.env` file (never commit it). Only names are
listed here. Values are secrets or deployment choices.

| Variable | Required | Purpose |
|---|---|---|
| `LLM_PROVIDER` | yes | `anthropic` (official Anthropic SDK, native structured output) or `openai_compat` (any OpenAI-compatible `/chat/completions` API in JSON mode) |
| `LLM_MODEL` | yes | exact model identifier |
| `LLM_API_KEY` | yes* | provider key (*Anthropic can also read `ANTHROPIC_API_KEY`) |
| `LLM_BASE_URL` | no | endpoint for `openai_compat`, e.g. `https://api.openai.com/v1` (default), `https://api.groq.com/openai/v1`, `https://generativelanguage.googleapis.com/v1beta/openai`, `https://openrouter.ai/api/v1` |
| `LLM_EFFORT` | no | Anthropic `output_config.effort` (e.g. `low`) for models that support it |
| `LLM_TEMPERATURE` | no | number or `none`; default omitted for Anthropic, `0` for `openai_compat` |
| `LLM_BACKUP_PROVIDER`, `LLM_BACKUP_MODEL`, `LLM_BACKUP_API_KEY`, `LLM_BACKUP_BASE_URL`, `LLM_BACKUP_EFFORT`, `LLM_BACKUP_TEMPERATURE` | no | optional independent backup model, tried once if the primary fails |
| `REQUEST_DEADLINE_SECONDS` | no | internal deadline per request, 5–29 (default 25, under the judge's 30 s limit) |
| `LLM_TIMEOUT_SECONDS` | no | cap per LLM call (default 10) |
| `LLM_CACHE_SIZE` | no | bounded in-memory interpretation cache (default 512, `0` disables) |
| `LLM_MAX_CONCURRENCY` | no | simultaneous LLM calls (default 8) |
| `LLM_WARMUP` | no | one background LLM call at startup to pay connection/schema setup costs (default `true`) |
| `LOG_LEVEL` | no | default `INFO` |
| `PORT` | no | HTTP port inside the container (default 8000) |

The cache key includes the exact notes, battery capacity and minimum, prompt version and model
identifiers. Cached entries were produced by the LLM. Nothing from the public cases is hard-coded.

If no provider is configured, the service still starts and `/health` returns ok, but
`POST /optimize-energy` returns a controlled 500 because the LLM is mandatory.

---

## 4. API

### `GET /health`

```json
{"status": "ok"}
```

### `POST /optimize-energy`

Request and response follow Problem Statement §07 and §10 exactly. Response excerpt for public
case SAMPLE-01:

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
     "explanation": "Panel washing leaves about 25% of forecast solar usable."},
    {"note_index": 1, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null,
     "explanation": "The registration deadline does not affect today's schedule."}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 90.0, "solar_used_kwh": 0.0, "battery_action": "idle",
     "battery_kwh": 0.0, "battery_energy_after_kwh": 110.0}
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Applied operator directives: usable solar limited to 25% during 12:00-14:00. ..."
}
```

(`hourly_plan` always has 24 rows; only one is shown. Explanation wording comes from the model.)

| Status | Meaning | Body |
|---|---|---|
| 200 | success | response above |
| 400 | malformed JSON, non-finite numbers, wrong types, missing fields, not exactly hours 0–23, not 1–3 non-empty notes | `{"error": "bad_request", "message": "..."}` |
| 422 | well-formed but impossible input: negative values, `minimum_energy_kwh > capacity_kwh`, initial energy outside [minimum, capacity] | `{"error": "unprocessable_entity", "message": "..."}` |
| 500 | no valid LLM interpretation in time, or no valid schedule | generic `{"error": "internal_error", "message": "..."}`; no stack traces, prompts or provider details |

---

## 5. Testing

```bash
# Deterministic tests (no LLM, no network): request validation, guardrails, optimizer,
# replay validator, API error paths, and all ten public cases end to end with a scripted model.
pytest -q

# Public-sample regression against a running service with the real LLM:
python scripts/eval_public.py --base-url http://localhost:8000
python scripts/eval_public.py --base-url https://<deployment> --repeat 3   # latency under repetition

# LLM accuracy on original labelled paraphrases and distractors (eval_data/paraphrases.json):
python scripts/eval_paraphrases.py
```

**Expected public-sample result:** 10/10 HTTP 200, 10/10 exact interpretations, 10/10 schedules valid
against the organizer directives, 10/10 optimal costs (38,365 · 42,885 · 35,480 · 40,495 · 33,950 ·
34,090 · 38,550 · 37,665 · 34,873 · 41,620 BDT), mean quality ratio 1.0000. `eval_public.py` replays
every returned plan against the **organizer** directives, not ours. `pytest` currently reports
127 passed.

---

## 6. Docker

The image contains no secrets. Pass configuration at runtime.

```bash
docker pull docker.io/<user>/gridwise-llm:1.0.0
docker run --rm -p 8000:8000 \
  -e LLM_PROVIDER=<provider> -e LLM_MODEL=<model> -e LLM_API_KEY=<key> \
  docker.io/<user>/gridwise-llm:1.0.0
curl http://localhost:8000/health
```

`--env-file .env` also works. Build and publish:

```bash
docker build -t <user>/gridwise-llm:1.0.0 .
docker push <user>/gridwise-llm:1.0.0
docker inspect --format '{{index .RepoDigests 0}}' <user>/gridwise-llm:1.0.0
```

The container runs as a non-root user, binds `0.0.0.0:${PORT:-8000}` and has a HEALTHCHECK on `/health`.

---

## 7. Repository layout

```text
app/
  main.py               FastAPI app, routes, controlled errors, startup warm-up
  config.py             environment settings
  schemas.py            data models and exact field lists
  request_validation.py 400/422 request checks
  llm/prompt.py         prompt, few-shot examples (original wording), output JSON schema
  llm/providers.py      Anthropic SDK adapter and OpenAI-compatible adapter
  llm/interpreter.py    LLM call → guardrails → repair → backup → cache
  guardrails.py         deterministic interpretation checks
  directives.py         directive → hourly constraint compilation
  optimizer.py          LP model (HiGHS)
  response_builder.py   hourly rows, totals, plan_summary
  validator.py          independent replay of every organizer rule
  pipeline.py           deadline-aware orchestration
tests/                  pytest suite (no network)
scripts/                eval_public.py, eval_paraphrases.py
eval_data/              labelled paraphrase set
```

---

## 8. Dependencies and credits

- [FastAPI](https://fastapi.tiangolo.com/) and [Uvicorn](https://www.uvicorn.org/): HTTP service
- [SciPy](https://scipy.org/) with the [HiGHS](https://highs.dev/) solver, and [NumPy](https://numpy.org/): linear programming
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python): Claude adapter
- [httpx](https://www.python-httpx.org/): OpenAI-compatible adapter and evaluation scripts
- [python-dotenv](https://github.com/theskumar/python-dotenv): local `.env` loading
- [pytest](https://pytest.org/): tests
- AI coding assistant: Claude Code (Anthropic) was used during development. Architecture
  decisions, review and verification are the team's.

Exact versions are pinned in `requirements.txt` and `requirements-dev.txt`.

## 9. Limitations

- Interpretation quality depends on the configured model. Measure it with `scripts/eval_paraphrases.py`.
- The organizer documents do not define windows that cross midnight. The prompt maps them to the
  covered hours of the same day (10 PM–2 AM → `[0, 1, 22, 23]`), and this is tracked as an assumption.
- The service needs the LLM provider to be reachable, with valid credentials and quota. Without it,
  optimization requests return a controlled 500 by design.
- The cache is per process and in memory. Restarts clear it.

## 10. Secret handling

- Keys are read only from environment variables or a local `.env`. `.env` is git-ignored and excluded
  from the Docker build context.
- Keys, prompts and provider payloads are never logged or returned. Logs contain request IDs,
  scenario IDs, stage timings, model labels, cache status and sanitized error categories.
- Set production keys as secrets in the hosting platform, never in the image, repository or README.
