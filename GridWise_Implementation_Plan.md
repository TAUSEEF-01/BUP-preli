# GridWise LLM — Implementation Plan

BUP CSE Fest 2026 Hackathon · Online Preliminary

**Status:** draft, waiting for team decisions (§1) · Written 2026-09-18, 19:20 BST · Round deadline 23:00 BST

---

## 0. Summary

We are building one public HTTP API:

- `GET /health` returns `{"status":"ok"}`.
- `POST /optimize-energy` takes a 24-hour energy scenario plus 1–3 operator notes. It returns a directive interpretation for every note and a valid, minimum-cost 24-hour schedule.

How it works: an LLM reads each note and returns a small JSON directive. Deterministic guardrails check and normalize it. A linear program (HiGHS via SciPy) computes the cheapest valid schedule. A replay validator then runs the judge's checks on our own output before we respond.

**Already verified:** the LP, given the organizers' ground-truth directives, reproduces all 10 public reference costs exactly (38,365 · 42,885 · 35,480 · 40,495 · 33,950 · 34,090 · 38,550 · 37,665 · 34,873 · 41,620 BDT). The optimizer design is settled. The main remaining risk is how accurately the LLM reads paraphrased notes.

---

## 1. Decisions needed

| # | Decision | Default |
|---|---|---|
| 1 | **LLM provider and model.** Which paid key can we get? Phase 1 needs no key, so there is time until about 20:25. | Whichever key we can get fastest, plus a second provider as backup. The code supports Claude through the official `anthropic` SDK (`claude-opus-5` at low effort, or `claude-haiku-4-5` for more speed and lower cost). It also supports any OpenAI-compatible API: OpenAI, Groq, Gemini or OpenRouter. The model name is an env var. Free tiers (Groq, Gemini) have daily caps, which is risky during judging. Latency is measured against the 5 s target; the team decides whether to switch models. |
| 2 | **Hosting.** It must stay awake and have a public URL. | Railway, a paid Render instance (the free one sleeps), a Hugging Face Docker Space built from the Docker Hub image, or a VPS we already have. |
| 3 | **Rule-based last-resort interpreter.** It runs only if every LLM call fails, and it is flagged in logs and in the explanation. | ON. It keeps the service answering when the provider is down or when judges run the Docker image without our key. The LLM stays the primary interpreter, so this is allowed. |
| 4 | **Stack and location** | Python 3.12 + FastAPI, in a new `gridwise-llm/` folder inside this workspace with its own git repo. The organizer PDFs stay outside the repo. |

---

## 2. Architecture

```
POST /optimize-energy
 1 Request validation ─── 400 malformed/structural · 422 semantic
 2 LLM interpreter ────── primary → 1 repair retry → secondary provider → rule fallback
                          (cache by notes+battery hash, temperature 0, JSON-schema output)
 3 Guardrails ─────────── enums, note mapping, windows→hours, %→kWh, ranges, applies/null rules
 4 Constraint builder ─── effective solar, reserve floor[h], charge/discharge caps[h], grid cap[h]
 5 LP optimizer ───────── min cost → tie-break (least battery cycling at the same cost)
                          infeasible → relaxed LP (base rules hard, directive violations minimized)
 6 Post-processor ─────── net flow → action/kWh, rounding, recompute energy & grid, totals
 7 Replay validator ───── the judge's checklist on our own output; re-solve with margins on failure
 8 Response builder ───── exact field order + deterministic plan_summary
```

The LLM understands the language. Deterministic code validates what it produced. The LP does the math. This matches the pipeline required in Problem Statement §03.

---

## 3. Repository layout

```
gridwise-llm/
├── app/
│   ├── main.py              FastAPI app, 2 routes, JSON error handlers (400/404/405/422/500)
│   ├── config.py            env settings: providers, models, timeouts, flags (no secrets in code)
│   ├── schemas.py           request/response + internal Directive models
│   ├── request_parser.py    strict body parsing → 400 / 422
│   ├── pipeline.py          orchestration, stage timings, 25 s internal deadline
│   ├── llm/
│   │   ├── prompt.py        system prompt, rules, ~8 original few-shot examples
│   │   ├── providers.py     AnthropicProvider + OpenAICompatProvider (async, timeouts)
│   │   └── interpreter.py   call → parse → guardrails → repair → fallback chain → cache
│   ├── guardrails.py        validation + canonical structured_adjustment
│   ├── rule_parser.py       last-resort deterministic interpreter
│   ├── optimizer.py         LP (SciPy/HiGHS), tie-break stage, relaxed fallback
│   ├── postprocess.py       schedule rows, rounding, totals
│   ├── validator.py         judge-equivalent replay (shared with scripts)
│   └── summary.py           plan_summary text
├── tests/                   pytest, no LLM needed
├── scripts/
│   ├── eval_public.py       POSTs all public cases to any base URL → accuracy/validity/cost/p95 report
│   └── paraphrase_bench.py  ~60 labelled paraphrases + distractors → accuracy per type and model
├── data/                    public_samples.json, paraphrases.json
└── Dockerfile, .dockerignore, requirements.txt, .env.example, .gitignore, README.md
```

---

## 4. Component details

### 4.1 Request validation

The raw body is parsed manually. FastAPI's default returns 422 for schema errors, but the spec wants 400.

- **400 Bad Request:**
  - bad JSON, `NaN`/`Infinity`, or a body that is not an object
  - missing fields, or strings/bools where numbers are expected
  - `hours` not exactly 24 unique hours 0–23
  - `operator_notes` not 1–3 non-empty strings
- **422 Unprocessable:**
  - negative demand, solar or rate limits
  - `minimum_energy_kwh > capacity_kwh`
  - `initial_energy_kwh` outside [min, capacity]
- Unsorted hours are sorted, unknown extra fields are ignored, and note length is capped before the LLM sees it.

### 4.2 LLM interpreter

Most of the score depends on this step. Interpretation is worth 25 points on its own. Application (25) and optimization (10) only score when the directive was read correctly.

The LLM returns the meaning it read, and code does the arithmetic:

```json
{"interpretations": [{
  "note_index": 0,
  "reasoning": "≤30 words: what, when, how much",
  "directive_type": "solar_reduction",
  "windows": [{"start_hour": 13, "end_hour": 15}],
  "solar_factor": 0.2,
  "reserve_kwh": null, "reserve_percent_of_capacity": null,
  "max_grid_kwh": null,
  "explanation": "Solar cut to 20% during panel cleaning."}]}
```

- **Why this format:** LLMs rarely misread "until 9 PM", but they often get hour lists wrong. So code expands `[start, end)` into hours (including windows that wrap past midnight), converts % of capacity to kWh, and sets `applies` and `null` itself.
- **Vocabulary per type:**
  - PV, panels, inverter, cloud, washing → `solar_reduction`
  - charger isolated, charging circuit down → `no_charge_window`
  - relay or protection test, battery output locked → `no_discharge_window`
  - keep at least, backup, must not fall below → `minimum_battery_reserve`
  - feeder, transformer, substation, intake, grid outage (cap 0) → `max_grid_window`
- **Time rules:**
  - noon = 12; midnight = 0 as a start and 24 as an end; 24-hour clock ("13:00"); number words ("from one until three")
  - "for N hours from X" → [X, X+N); "after X" or "rest of the day" → [X, 24); "until X" → [0, X); "all day" → [0, 24)
  - a single hour, e.g. "at 5 PM" → [17, 18)
  - `end_hour` is the clock hour where the window ends; code excludes it, so the LLM never subtracts one
  - missing AM/PM: context words decide (morning → AM; afternoon, evening, night → PM; solar notes → daytime), otherwise 1–6 → PM and 7–11 → AM
- **Value rules:**
  - The solar factor is the fraction that remains: "80% reduction" → 0.2, "drop to 20%" → 0.2, "one-fifth of normal" → 0.2, "halved" → 0.5, "offline" → 0.
  - Reserves are in kWh or % of capacity; capacity and base minimum are passed in the prompt.
  - The grid cap is kWh per hour.
- **no_op rules:**
  - unrelated topics (cafeteria, library, registration, …)
  - other days (tomorrow, next week, yesterday), even if energy-related
  - effects the spec doesn't support: demand, tariff or capacity changes, solar increases
  - EV or phone chargers, which are not the campus battery
- **Safety:** notes are passed as marked-off data, so instructions written inside a note are not followed.
- **Few-shot examples:** written from scratch, not copied from the public cases, as the rules require.
- **Settings:** temperature 0, provider-native JSON schema output, and a cache keyed on notes and battery parameters.
- **Fallback chain:**
  1. primary call (8 s timeout)
  2. one repair retry with the validation errors
  3. secondary provider
  4. rule parser

  The whole chain is capped at about 20 s, so every request finishes well under the 30 s judge timeout.

### 4.3 Guardrails

| Check | On failure |
|---|---|
| JSON parse, schema, `directive_type` enum | 1 repair retry with the exact error list |
| One entry per note, indices 0..N-1, no duplicates | Repair (missing notes only) |
| Window hours are integers; start 0–23, end 1–24; non-empty unless no_op | Repair |
| Factor finite and in [0,1]; 1.0 means no change, so it becomes no_op | Repair |
| Reserve finite, ≥ 0, ≤ capacity after % conversion | Repair |
| `max_grid_kwh` finite, ≥ 0 | Repair |
| Still invalid after repair | That note goes to the rule parser, otherwise `no_op` ("could not be safely interpreted"). Never invented. |

Code always writes the final entry: hours sorted and unique, exact keys per type, `applies = (type != no_op)`, and `structured_adjustment = null` for no_op. The LLM has no field that could change demand, solar, tariff or battery parameters, so "no invention" holds by construction.

### 4.4 Optimizer (already tested)

Per hour: grid `g`, solar used `s`, charge `c`, discharge `d` (all ≥ 0), and battery energy `E`.

- **Energy:**
  - `g + s + d = demand + c`
  - `E[h] = E[h-1] + c - d`
  - `E[23] = initial`
- **Limits:**
  - `max(base_min, reserve[h]) ≤ E[h] ≤ capacity`
  - `s ≤ solar·factor`
  - `c ≤ rate` (0 in no-charge hours); `d ≤ rate` (0 in no-discharge hours)
  - `g ≤ cap[h]`
- **Two stages:** first minimize Σ tariff·g. Then hold that cost fixed and minimize Σ(c+d), which only removes pointless charge/discharge cycling.
- **Overlapping directives combine conservatively:** solar factors multiply, reserves take the max, caps take the min, and windows are merged.
- **Infeasible model** (only possible if the interpretation is wrong): solve a relaxed LP where base rules stay hard and directive violations are minimized, and log it.
- **Size:** about 120 variables; solves in about 10 ms.

### 4.5 Post-processing and replay

- `net = c − d` decides charge, discharge or idle (idle means `battery_kwh = 0`).
- **Rounding:**
  - Flows are rounded to 6 decimals.
  - Energy and grid are recomputed from the rounded flows, so every equation holds to about 1e-9.
  - Solar used is clamped to effective solar, the end-of-day battery residual is corrected, and `-0.0` never appears.
- Totals (`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`) come from the final rows.
- The replay validator runs every judge check from Problem Statement §11.3 plus each directive. If anything fails, the LP is re-solved with small safety margins.

### 4.6 API

- `GET /health` returns `{"status":"ok"}` and never calls the LLM. The app starts even with no API key, which the Docker check needs.
- Response fields come out in the exact spec order: `scenario_id`, `directive_interpretation`, `hourly_plan`, `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`.
- `plan_summary` is a fixed-template text: directives applied, notes ignored, charge and discharge hours, and cost.
- 500 responses carry a generic JSON message. No stack traces or secrets appear in responses or logs.

---

## 5. Testing

- **pytest (no LLM needed):**
  - the optimizer with the correct directives hits all 10 reference costs
  - the validator passes the reference plans and catches injected violations
  - guardrail checks, every 400/422 path, and the response schema
- **`scripts/eval_public.py`:** runs end-to-end against localhost, then against the deployed URL. Target: 10/10 interpretations, 10/10 valid plans, cost ratio 1.000, p95 under 5 s.
- **`scripts/paraphrase_bench.py`:** about 60 labelled notes (seeded from Appendix C) to pick the model and tune the prompt.
- **Optional add-ons**, kept only if the bench shows they help:
  - a disagreement check, where a short LLM re-check decides when the rule parser and the LLM disagree
  - conservative hedging, which schedules against the stricter of two candidate readings (valid under either reading, at a small cost)

---

## 6. Deployment and submission

- **Dockerfile:** `python:3.12-slim`, non-root user, `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`, HEALTHCHECK. `.dockerignore` excludes `.env`, so no secrets end up in the image.
- **Image:** push `docker.io/<user>/gridwise-llm:1.0.0` and record its digest.
- **Deploy early**, as soon as the public samples pass, so hosting problems show up early. Later changes are just redeploys.
- **Test from outside** the dev machine: `/health` plus `eval_public.py` against the public URL.
- **README:** one section per documentation scoring point (outline in Appendix D).
- **Video:** 3 minutes, tie-break only (outline in Appendix E).

**Submission checklist**

| # | Item |
|---|---|
| 1 | Public base URL (`/health`, `/optimize-energy`) |
| 2 | Private GitHub repo, made public after the deadline |
| 3 | README with quickstart, env var names, model/provider, solver, sample request/response |
| 4 | Docker image with exact tag or digest, env var names, port, one verified `docker run` command |
| 5 | Video of 3 minutes or less |

---

## 7. Timeline (deadline 23:00 BST)

| Time | Build track (Claude Code) | Team track (in parallel) |
|---|---|---|
| 19:20–19:35 | — | Review this plan and answer §1 |
| 19:35–20:25 | Phase 1: schemas, request parser, LP, post-processing, validator, API, tests | Get LLM key(s); create the private GitHub repo; set up Docker Hub and hosting accounts |
| 20:25–21:05 | Phase 2: providers, prompt, interpreter, guardrails, cache, fallback chain; public samples pass | Put keys in `gridwise-llm/.env` locally. Never paste keys in chat. |
| 21:05–21:35 | Phase 3: Dockerfile, rule parser, paraphrase bench, prompt tuning | First deploy; test the public URL from outside |
| 21:35–22:15 | Phase 4: README, fixes from the bench, redeploy | — |
| 22:15–22:45 | Video outline, final checks | Record the video; submit |
| 22:45–23:00 | Buffer | Buffer |

---

## 8. Main risks

| Risk | Mitigation |
|---|---|
| A paraphrase is misread (hours, factor, relevance) | Meaning-first output format, detailed prompt, guardrails, paraphrase bench |
| Provider down or rate-limited during judging | Cache, secondary provider, rule fallback, 25 s internal deadline |
| Host sleeps, so the first request misses the 60 s health window | Always-on host |
| Numeric edge cases (tolerance, `-0.0`, rounding drift) | Recompute from rounded values, then the replay validator |
| Judges run the Docker image without our key | App still starts and `/health` works; the rule fallback answers, flagged |

---

## 9. Assumptions to confirm

The spec does not settle these. The plan uses the choices below.

1. A window that crosses midnight ("10 PM to 2 AM") wraps within the same day → hours `[0, 1, 22, 23]`.
2. Missing AM/PM is resolved from context first, then by the default in §4.2.
3. A note saying solar stays at 100% (factor 1.0) is reported as `no_op`.
4. A reserve below the base minimum is still reported as `minimum_battery_reserve`; it just doesn't change the math.
5. "Grid outage" or "no grid import" → `max_grid_window` with `max_grid_kwh = 0`.
6. Several directives on the same hour: solar factors multiply, reserves take the max, caps take the min. Multiplying factors is the most conservative choice, so the plan stays valid however the judge combines them.
7. Notes about another day are `no_op`, even if they mention energy.
8. Structural errors return 400; semantic errors (negative values, min > capacity, initial outside [min, capacity]) return 422.

---

## Appendix A — Scoring rubric coverage

| Category (points) | Sub-items (from the Participant Guide §07) | Where the plan earns them |
|---|---|---|
| LLM Directive Interpretation (25) | 5 relevance/no_op · 5 type · 5 hours · 5 values/shape · 5 paraphrase robustness | §4.2 prompt and output format, §4.3 guardrails, paraphrase bench |
| Directive Application & Constraint Correctness (25) | 10 ground-truth application · 5 energy balance/effective solar · 5 battery transitions/bounds/rates · 5 action consistency/neutrality/non-negative | §4.4 LP, §4.5 rounding and replay validator |
| Optimization Quality (10) | min(1, organizer cost / our cost); invalid cases score 0 | Exact LP optimum (verified on 10/10 public cases) |
| API Contract & Schema (10) | 2 endpoints/status · 2 request validation · 3 interpretation schema/order/types · 3 plan/top-level schema + scenario_id echo | §4.1, §4.6 |
| Performance & Reliability (10) | 2 health readiness · 3 p95 latency (≤5 s full, 5–15 s 2/3, 15–30 s 1/3) · 3 stability · 2 failure handling and secret safety | Cache, timeouts, fallback chain, error handlers |
| Deployment & Docker Fallback (10) | 3 live endpoint · 4 pullable image reaching `/health` · 2 clean startup · 1 no judge debugging | §6 |
| Documentation & Local Reproducibility (10) | 3 quickstart · 2 env/config/model docs · 2 public-sample test + expected result · 1 architecture · 1 Docker · 1 deps/limitations/secrets | Appendix D |

---

## Appendix B — Environment variables (names only, never commit values)

Final names will be confirmed in the README.

| Name | Purpose | Default |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` or `openai_compat` | — |
| `LLM_MODEL` | model id, e.g. `claude-opus-5` | — |
| `LLM_API_KEY` | primary provider key (secret) | — |
| `LLM_BASE_URL` | endpoint for OpenAI-compatible providers (Groq, Gemini, OpenRouter) | provider default |
| `LLM_FALLBACK_PROVIDER`, `LLM_FALLBACK_MODEL`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_BASE_URL` | secondary provider | unset |
| `LLM_TIMEOUT_SECONDS` | per LLM call | 8 |
| `REQUEST_DEADLINE_SECONDS` | whole request | 25 |
| `ENABLE_RULE_FALLBACK` | last-resort interpreter on/off | `true` |
| `LLM_CACHE_SIZE` | cached interpretations | 512 |
| `LOG_LEVEL` | logging | `INFO` |
| `PORT` | HTTP port | 8000 |

---

## Appendix C — Interpretation test cases (seed for the paraphrase bench)

Capacity is assumed to be 250 kWh where a percentage is involved.

| Note | Expected interpretation |
|---|---|
| "PV output falls to roughly one-fifth between 13:00 and 15:00." | `solar_reduction` [13,14], factor 0.2 |
| "Expect a 30% drop in solar from 10 AM to noon." | `solar_reduction` [10,11], factor 0.7 |
| "Panels offline for inverter replacement, 9–11 AM." | `solar_reduction` [9,10], factor 0.0 |
| "Battery must stay at least 40% charged from 5 PM until 8 PM." | `minimum_battery_reserve` [17,18,19], 100 kWh |
| "Don't let the battery fall below 60 kWh at any time today." | `minimum_battery_reserve` [0..23], 60 kWh |
| "Charger firmware update at 1 PM (one hour)." | `no_charge_window` [13] |
| "The battery may discharge but must not be charged from noon to 2 PM." | `no_charge_window` [12,13] |
| "Protection relay test: battery output locked 17:00–19:00." | `no_discharge_window` [17,18] |
| "Keep grid draw at or below 120 kWh per hour from 6 to 9 PM." | `max_grid_window` [18,19,20], 120 |
| "Grid supply interrupted from 2 PM to 3 PM." | `max_grid_window` [14], 0 |
| "Transformer limit of 150 kWh after 8 PM." | `max_grid_window` [20,21,22,23], 150 |
| "Overnight maintenance from 10 PM to 2 AM: no battery charging." | `no_charge_window` [0,1,22,23] (assumption §9.1) |
| "The solar panels will be cleaned next Tuesday." | `no_op` (another day) |
| "EV charging bays closed 2–4 PM." | `no_op` (not the campus battery) |
| "Evening demand will rise 10% because of an event." | `no_op` (demand changes unsupported) |
| "Electricity tariffs go up next month." | `no_op` |

---

## Appendix D — README outline

1. Overview and architecture diagram (LLM → guardrails → LP → replay validator)
2. Quickstart from a clean machine: clone → venv → `pip install -r requirements.txt` → copy `.env.example` to `.env` → run → `curl /health` → `curl` a public sample
3. Configuration: env var table, model/provider used, the LLM's role
4. Testing: `pytest`; `python scripts/eval_public.py --base-url …` with the expected result (10/10 interpretations, 10/10 valid, cost ratio 1.000)
5. Docker: `docker pull` / `docker run` with the exact tag and digest, env vars, port
6. API reference: request/response example, error codes
7. Guardrails and optimizer details
8. Dependencies and credits (FastAPI, Uvicorn, Pydantic, NumPy, SciPy/HiGHS, httpx, LLM SDKs; AI coding assistant: Claude Code), known limitations, secret handling

---

## Appendix E — 3-minute video outline

| Time | Content |
|---|---|
| 0:00–0:25 | The problem: notes → directives → valid minimum-cost schedule |
| 0:25–1:15 | Architecture: LLM → guardrails → LP → replay validator; why the LLM returns meaning and code does the arithmetic |
| 1:15–2:15 | Live demo: `/health`, a public sample, a paraphrased note, a malformed request returning 400 |
| 2:15–2:45 | Testing and reliability: eval results, paraphrase bench, fallback chain, cache |
| 2:45–3:00 | How to run it (README, Docker) |
