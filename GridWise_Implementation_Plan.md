# GridWise LLM — Verified Implementation Plan

BUP CSE Fest 2026 Hackathon · Online Preliminary

**Status:** specification audit complete; implementation not yet present in this repository

---

## 0. Authority and audit result

This plan was checked against all three organizer-provided files in this repository:

1. `BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.pdf` — canonical for challenge behavior, API schemas, directives, guardrails, battery rules, energy accounting, and optimization validity.
2. `BUP_CSE_FEST_2026_Participant_Guide_&_Evaluation_Rubric_GridWise_LLM.pdf` — canonical for deployment, repository policy, submission, performance, scoring, penalties, and tie-breaks.
3. `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` — ten worked examples for local validation, not the hidden judge set.

The earlier draft had the correct high-level LLM → guardrails → optimizer → replay architecture, but several details were unsafe or unsupported by the specification. This revision corrects them:

- A deterministic phrase parser must not produce the final interpretation when all LLM calls fail. That would create requests whose operator-note path contains no language model, contrary to the mandatory LLM requirement. Provider exhaustion now ends in a controlled internal error.
- Invalid model output must not be silently converted to `no_op`. `no_op` is only for a note that genuinely does not affect the current schedule.
- Directives remain hard constraints. The service must never return a “relaxed” plan that knowingly violates one.
- Overlapping solar-reduction directives are enforced as individual upper bounds, equivalent to using the smallest remaining factor for that hour. Multiplying factors is not stated by the problem and can make a valid plan unnecessarily expensive.
- `factor = 1.0` remains a valid `solar_reduction`; the canonical guardrail explicitly allows the inclusive range `[0, 1]`. It must not automatically become `no_op`.
- The retry and timeout policy now fits within the 30-second request timeout and is designed around the 5-second p95 scoring threshold.
- The implementation belongs in this repository. Creating a nested Git repository would complicate submission and history.
- The plan no longer claims that an optimizer has already been implemented. Only the organizer reference outputs and an independent audit of them have been verified so far.

### Verified public baseline

An independent replay of every public `expected_output` confirmed all ten schedules satisfy their published directives, hourly energy balance, effective-solar limits, battery transitions/bounds/rates, end-of-day neutrality, and reported aggregates. A separate 0.5 kWh state-grid dynamic program reproduced every published optimal cost:

| Case | Cost (BDT) |
|---|---:|
| SAMPLE-01 | 38,365 |
| SAMPLE-02 | 42,885 |
| SAMPLE-03 | 35,480 |
| SAMPLE-04 | 40,495 |
| SAMPLE-05 | 33,950 |
| SAMPLE-06 | 34,090 |
| SAMPLE-07 | 38,550 |
| SAMPLE-08 | 37,665 |
| SAMPLE-09 | 34,873 |
| SAMPLE-10 | 41,620 |

These are regression targets, not values to hard-code.

---

## 1. Required outcome

Build and deploy one public JSON HTTP service:

- `GET /health` returns HTTP 200 and `{"status":"ok"}` when ready.
- `POST /optimize-energy` accepts one 24-hour scenario and 1–3 operator notes, uses a language-capable generative model to interpret every note, deterministically validates the interpretations, applies every relevant directive as a hard optimization constraint, and returns the exact required response schema.

Correctness comes before price. The judge independently replays the schedule using organizer ground-truth directives, and only valid cases receive optimization credit.

The implementation should target the rubric in this order:

1. exact API and schema;
2. LLM interpretation accuracy;
3. deterministic guardrails;
4. directive application and energy correctness;
5. optimal cost;
6. reliability and latency;
7. deployment, Docker, and documentation;
8. the three-minute tie-break video.

---

## 2. Architecture

```text
POST /optimize-energy
  1. Parse and validate request
  2. Interpret all notes with an LLM using structured output
  3. Deterministically validate the complete interpretation
     ├─ valid: continue
     └─ invalid: bounded repair/backup-model attempt, otherwise controlled 500
  4. Compile validated directives into hard hourly constraints
  5. Solve the minimum-cost linear program
     └─ infeasible: do not relax directives; diagnose/retry interpretation or fail safely
  6. Convert solver variables to the exact response schema
  7. Replay every organizer rule against the candidate response
     ├─ valid: return HTTP 200
     └─ invalid: retry numerical solve once, otherwise controlled 500
```

The LLM performs semantic interpretation. Deterministic code validates structure and numeric ranges and compiles the accepted meaning into constraints. The optimizer performs scheduling. No rule-based phrase matcher replaces the LLM.

---

## 3. Proposed repository layout

Use the existing repository root rather than creating a nested repository:

```text
.
├── app/
│   ├── main.py                 # FastAPI app and error handlers
│   ├── config.py               # environment settings; no secret values
│   ├── schemas.py              # request, response, and internal models
│   ├── request_validation.py   # deterministic request checks
│   ├── pipeline.py             # deadline-aware orchestration
│   ├── llm/
│   │   ├── prompt.py           # prompt and structured-output schema
│   │   ├── providers.py        # primary and optional backup provider adapters
│   │   └── interpreter.py      # call, validate, repair, and cache flow
│   ├── guardrails.py           # exact interpretation guardrails
│   ├── directives.py           # directive-to-hourly-constraint compilation
│   ├── optimizer.py            # LP construction and solution
│   ├── response_builder.py     # action rows, aggregates, and summary
│   └── validator.py            # independent response replay
├── tests/
│   ├── test_api.py
│   ├── test_guardrails.py
│   ├── test_optimizer.py
│   ├── test_replay.py
│   └── test_public_cases.py
├── scripts/
│   ├── eval_public.py
│   └── eval_paraphrases.py
├── eval_data/
│   └── paraphrases.json        # original labelled development cases
├── BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json
├── Dockerfile
├── .dockerignore
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

The two organizer PDFs and public JSON remain reference artifacts. Tests may read the public JSON directly; do not duplicate or modify its expected results.

---

## 4. Request validation

### 4.1 Structural validation — HTTP 400

Return a controlled 400 JSON response for malformed JSON or a structurally invalid request, including:

- body is not a JSON object;
- missing required top-level, hour, or battery fields;
- wrong JSON types, treating booleans as invalid numbers;
- non-finite numbers such as `NaN` or infinity;
- `hours` is not exactly 24 entries containing each integer hour 0–23 exactly once;
- `operator_notes` is not an array of 1–3 non-empty strings.

Incoming hour rows may be normalized to ascending hour order internally. Do not truncate a valid operator note before interpretation, because truncation could change its meaning. A separate generous HTTP body-size limit may protect the service from abuse.

### 4.2 Semantic validation — HTTP 422 (optional by specification)

Use a controlled 422 consistently for well-formed but impossible or out-of-domain input, including:

- negative demand, solar, tariff, capacity, reserve, or charge/discharge rate;
- `minimum_energy_kwh > capacity_kwh`;
- `initial_energy_kwh` outside `[minimum_energy_kwh, capacity_kwh]`.

All numeric inputs must be finite. Error bodies must not expose stack traces, prompts, credentials, or provider responses.

---

## 5. LLM interpretation

### 5.1 Mandatory role

The language model must interpret `operator_notes` into the structured directives that are used by the optimizer. Using a model only for `plan_summary`, documentation, or cosmetic text is non-compliant. Hard-coded phrase matching cannot be the sole interpreter and must not become the final interpreter during provider failure.

Use one structured-output call for all 1–3 notes when possible. Give the model:

- the notes with explicit data delimiters and prompt-injection warnings;
- the six allowed directive types and exact adjustment shapes;
- the start-inclusive/end-exclusive hour convention;
- the battery capacity, so a percentage reserve can become kWh;
- rules that unsupported or irrelevant effects map to `no_op` rather than a fabricated directive;
- a small, diverse set of original examples that does not encourage matching public phrases.

The model should emit the final machine-checkable fields for each note:

```json
{
  "interpretations": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "hours": [13, 14],
        "factor": 0.2
      },
      "explanation": "Usable solar is reduced during the stated window."
    }
  ]
}
```

Requiring final `hours` and final numeric values from the model keeps the LLM visibly in the semantic path. Deterministic code may sort/deduplicate an otherwise identical hours list only if the semantic content is unchanged; a wrong or ambiguous time/value must be repaired by a model, not guessed by code.

### 5.2 Supported meanings

Only these final interpretations are allowed:

| Type | Exact `structured_adjustment` |
|---|---|
| `solar_reduction` | `{"hours":[...], "factor": number}` |
| `minimum_battery_reserve` | `{"hours":[...], "minimum_energy_kwh": number}` |
| `no_charge_window` | `{"hours":[...]}` |
| `no_discharge_window` | `{"hours":[...]}` |
| `max_grid_window` | `{"hours":[...], "max_grid_kwh": number}` |
| `no_op` | `null` |

Important semantic rules:

- every note maps to exactly one entry and exactly one supported type;
- windows include the start hour and exclude the end hour;
- `factor` is the usable fraction remaining: an 80% reduction means `0.2`;
- `factor = 1.0` is allowed and remains `solar_reduction` if that is what the note says;
- a stated reserve below the base battery minimum is still returned as `minimum_battery_reserve`; the optimizer later takes the maximum of the two floors;
- irrelevant notes and unsupported schedule changes use `no_op` with `applies = false` and a null adjustment;
- the model must not change demand, tariff, battery parameters, or any other base input.

Do not encode undocumented deterministic guesses such as “1–6 without AM/PM always means PM.” Let the model use the note's language and context. Add labelled test cases for ambiguous clocks, noon/midnight, number words, percentages, and paraphrases. Cross-midnight wording is not explicitly defined by the organizer documents; the most natural candidate mapping is the sorted set of covered hours (for example, 10 PM–2 AM → `[0,1,22,23]`), but this remains a tested interpretation assumption rather than a canonical rule.

### 5.3 Guardrails

Treat model output as untrusted until every check passes:

- exactly one entry per input note, in `note_index` order `0..N-1`;
- no missing, duplicate, or out-of-range indices;
- `directive_type` is one of the six allowed values;
- `hours` is a non-empty array of unique ascending integers from 0 through 23 for every applicable directive;
- `solar_reduction.factor` is finite and in `[0,1]`;
- `minimum_energy_kwh` is finite, non-negative, and no greater than battery capacity;
- `max_grid_kwh` is finite and non-negative;
- adjustment keys exactly match the selected directive shape;
- `no_op` has `applies = false` and `structured_adjustment = null`;
- every other type has `applies = true` and a non-null adjustment.

Failure policy:

1. Reject the entire model result if any entry is invalid; do not partially invent replacements.
2. Make at most one deadline-aware repair call with validation errors and the original notes.
3. If configured, try one independent backup language model with the original task.
4. If no valid complete interpretation is obtained, return a generic controlled HTTP 500. Never relabel the failed note as `no_op`, and never continue with a rule-only interpretation.

### 5.4 Latency, retries, and cache

The judge timeout is 30 seconds, while p95 at or below 5 seconds receives full latency credit. Therefore:

- use a fast structured-output-capable primary model;
- impose a short primary timeout appropriate to the measured deployment latency;
- start a repair or backup attempt only if enough of the request deadline remains;
- reserve time for optimization, replay, serialization, and network overhead;
- cap the whole pipeline below 30 seconds, preferably well below it;
- benchmark the actual deployed provider before fixing timeout values.

An in-memory bounded cache is acceptable because cached interpretations were originally produced by an LLM. Its key must include the exact notes, battery context, prompt/schema version, provider/model identifier, and any setting that can affect interpretation. Do not hard-code public case IDs, notes, outputs, or numeric values.

---

## 6. Directive compilation

Start with per-hour base values:

- `effective_solar[h] = original_solar[h]`;
- `reserve_floor[h] = battery.minimum_energy_kwh`;
- charge and discharge upper bounds equal their base hourly rates;
- grid upper bound is unbounded.

Apply every validated directive:

- `solar_reduction`: for each listed hour, add `solar_used[h] <= original_solar[h] * factor`;
- `minimum_battery_reserve`: raise `reserve_floor[h]` with `max`;
- `no_charge_window`: set the charge upper bound to zero;
- `no_discharge_window`: set the discharge upper bound to zero;
- `max_grid_window`: lower the grid upper bound with `min`;
- `no_op`: make no model change.

If multiple solar reductions cover one hour, keeping all individual upper bounds is equivalent to the smallest factor, not the product of factors. This satisfies every stated reduction without inventing an additional compounded reduction.

Organizer scoring scenarios are promised to have feasible, non-contradictory ground-truth directives. If the compiled model is infeasible, treat that as a likely interpretation or implementation error. A bounded independent LLM retry may be attempted if time remains; otherwise fail safely. Do not soften a directive and return an invalid schedule.

---

## 7. Optimizer

Use a continuous linear program such as HiGHS through SciPy. For each hour `h`, define non-negative variables:

- `g[h]`: grid import;
- `s[h]`: solar used;
- `c[h]`: battery charge;
- `d[h]`: battery discharge;
- `E[h]`: battery energy after the hour.

### 7.1 Hard constraints

For every hour:

```text
g[h] + s[h] + d[h] = demand[h] + c[h]
E[h] = E[h-1] + c[h] - d[h]
reserve_floor[h] <= E[h] <= capacity
0 <= s[h] <= every active solar upper bound
0 <= c[h] <= active charge limit[h]
0 <= d[h] <= active discharge limit[h]
0 <= g[h] <= active grid cap[h]
```

For hour 0, use `initial_energy_kwh` in place of `E[-1]`. Enforce end-of-day neutrality exactly in the model:

```text
E[23] = initial_energy_kwh
```

Do not add charge/discharge efficiency, grid export, demand shifting, or other behavior absent from the specification.

### 7.2 Objective and degeneracy

Primary objective:

```text
minimize sum(g[h] * tariff_bdt_per_kwh[h])
```

The LP formulation permits simultaneous charge and discharge algebraically. Because the specified battery has no efficiency loss, a secondary solve may eliminate this degeneracy:

1. solve for minimum grid cost;
2. constrain cost to the optimum within a solver tolerance far below the judge's 0.01 BDT tolerance;
3. minimize `sum(c[h] + d[h])`.

The secondary objective must not sacrifice the primary optimum. An alternative is a solver formulation that directly prevents simultaneous flows, but that introduces integer variables and is unnecessary if the secondary solve and replay are reliable.

---

## 8. Response construction and independent replay

### 8.1 Required response

Return exactly the required top-level fields:

- `scenario_id` copied from the request;
- `directive_interpretation` with one entry per note in note order;
- `hourly_plan` with 24 unique entries for hours 0–23;
- `total_grid_kwh`;
- `total_cost_bdt`;
- `peak_grid_kwh`;
- `plan_summary`.

Each hourly entry contains exactly:

- `hour`;
- `grid_kwh`;
- `solar_used_kwh`;
- `battery_action`, one of `charge`, `discharge`, or `idle`;
- non-negative `battery_kwh`, which is zero for `idle`;
- `battery_energy_after_kwh`.

JSON object key order is not semantically important, but array order should be deterministic and ascending.

### 8.2 Numerical handling

- Convert negligible solver noise to zero so `-0.0` is never emitted.
- Derive the public battery action from the net flow after the secondary solve.
- Emit enough decimal precision to stay comfortably within 0.01 kWh/BDT.
- Recalculate battery energy sequentially and grid balance consistently from the final emitted flows.
- Calculate all aggregates from the final emitted hourly rows, not directly from raw solver internals.
- Do not “fix” a residual after solving if doing so can break a reserve, rate, solar, or grid-cap constraint.

If the first serialization fails replay only because of numerical tolerance, solve again with small inward numerical margins and rebuild the response. If it still fails, return a controlled 500 instead of an invalid plan.

### 8.3 Replay validator

The replay validator must be independent enough to catch optimizer or serialization bugs. For the final emitted response it checks:

- scenario ID echo and exact array lengths;
- interpretation count, order, types, shapes, and numeric ranges;
- hours 0–23 exactly once and in ascending order;
- finite, non-negative numeric output;
- action/`battery_kwh` consistency;
- effective-solar limits after the organizer-style directive application;
- no-charge, no-discharge, reserve, and grid-cap directives;
- hourly energy balance;
- battery transition, capacity, base/directed reserve, and rate limits;
- final energy equal to initial energy;
- totals and peak recalculated from `hourly_plan`;
- cost recalculated using request tariffs.

Use an absolute comparison tolerance no larger than the published 0.01 unless an official judge package later specifies a stricter value.

---

## 9. Error handling, security, and observability

- `GET /health` must not call the LLM and must become ready within 60 seconds of startup.
- A missing provider key may allow the process and health endpoint to start, but `POST /optimize-energy` must fail with a controlled generic error rather than bypassing the LLM requirement.
- Valid requests should not produce 5xx responses under normal operation; provider quota, rate limits, and availability are deployment responsibilities.
- Use bounded network timeouts and no unbounded retries.
- Return generic 500 JSON; never return raw exceptions, provider payloads, prompts, or secrets.
- Do not log authorization headers, API keys, `.env` contents, or full provider responses. Prefer request IDs, scenario IDs, stage timings, model identifiers, cache status, and sanitized error categories.
- Notes are synthetic, but avoid unnecessary full-prompt logging.
- Keep `.env`, credentials, local caches, and test artifacts out of Git and Docker build contexts.

---

## 10. Test plan

### 10.1 Deterministic unit tests

- request schema and every 400/422 branch;
- each exact directive shape and each guardrail rejection;
- one interpretation per note and strict note ordering;
- factor, reserve, grid-cap, finite-number, and hour bounds;
- every directive's compiled hourly constraints;
- overlapping reserve/grid/solar constraints;
- optimizer balance, bounds, rates, neutrality, and optimality on small hand-solvable cases;
- replay validator catches one injected violation of every rule;
- aggregation and floating-point boundary tests;
- controlled provider/model failure without a rule-only fallback.

### 10.2 Public sample regression

For all ten cases in the organizer JSON:

1. POST each `input` to the service.
2. Compare structured directive semantics with `expected_output` while ignoring exact explanation wording.
3. Replay the returned schedule independently.
4. Recalculate totals and cost.
5. Require valid cost equal to the public optimum within published tolerance.

Target: 10/10 interpretation semantics, 10/10 valid schedules, all ten optimal costs, no 5xx responses.

### 10.3 Paraphrase evaluation

Build an original labelled set covering all six directive types, with:

- alternate vocabulary and word order;
- 12-hour and 24-hour clocks, noon/midnight, and number words;
- reduction-versus-remaining percentages;
- reserves in kWh and percentage of battery capacity;
- grid caps, including zero;
- multiple notes and distractors;
- unsupported demand/tariff/capacity changes;
- same-day versus clearly other-day notes;
- prompt-injection-like text inside note delimiters;
- ambiguous and cross-midnight wording tracked separately as assumptions.

Report exact accuracy for applicability, directive type, hours, numeric value, and complete entry. Do not tune only against the ten public wordings.

### 10.4 Reliability and deployment tests

- repeated valid requests, concurrent requests, cache hits/misses, and provider timeout/failure;
- measure p50/p95/p99 end-to-end latency on the deployed endpoint;
- verify p95 against the rubric bands: ≤5 s, >5–15 s, >15–30 s, and timeout beyond 30 s;
- call both endpoints from outside the development network;
- build and run the submitted Docker image from a clean machine;
- execute the README quickstart exactly as written;
- scan the repository and image history for secrets.

---

## 11. Implementation phases

### Phase 1 — deterministic core

1. Add request/response schemas and controlled errors.
2. Implement directive guardrails and constraint compilation.
3. Implement the LP, response builder, and independent replay.
4. Make all ten public cases pass using their organizer-provided directive interpretations, without calling an LLM.

This phase validates optimization and serialization only; it does not satisfy the final LLM requirement by itself.

### Phase 2 — interpretation path

1. Select a structured-output-capable model/provider whose credentials and quota are available during judging.
2. Implement the prompt, adapter, complete-output validation, one repair attempt, and optional backup LLM.
3. Add the prompt/schema-versioned cache.
4. Reach 10/10 public interpretations and strong held-out paraphrase accuracy.
5. Verify the accepted interpretation is exactly what compiles into optimizer constraints.

### Phase 3 — full pipeline and reliability

1. Exercise model → guardrails → directives → optimizer → replay end to end.
2. Tune deadline-aware timeouts using deployed latency measurements.
3. Add concurrency limits and controlled provider failure behavior.
4. Run regression, paraphrase, malformed-input, and repeated-request tests.

### Phase 4 — packaging and submission

1. Build a non-root Docker image that binds to `0.0.0.0` and exposes the documented port.
2. Push an exact version tag and record its digest.
3. Deploy an always-reachable public endpoint and test externally.
4. Finish the self-contained README and verified copy-paste commands.
5. Record a maximum three-minute architecture/solution video.
6. Run the final checklist and make the repository public only after the submission deadline, per the rulebook.

---

## 12. Configuration decisions still required

The organizer documents intentionally leave implementation technology open. The team must choose and verify:

| Decision | Acceptance criterion |
|---|---|
| Primary model/provider | Structured output, strong paraphrase accuracy, sufficient quota, and deployed p95 compatible with scoring |
| Optional backup LLM | Independent credentials/quota and usable only within the total deadline |
| Hosting platform | Public, always reachable, supports secrets and the chosen solver, no cold start that threatens readiness/latency |
| Container registry | Pullable throughout evaluation with exact tag/digest |
| Solver packaging | Reproducible clean install/build and public-case optimality |

Do not place speculative model identifiers in the committed plan. Record the actually tested provider/model and all required environment-variable names in the final README.

Suggested environment-variable interface:

| Name | Purpose |
|---|---|
| `LLM_PROVIDER` | primary provider adapter |
| `LLM_MODEL` | exact primary model identifier |
| `LLM_API_KEY` | primary secret |
| `LLM_BASE_URL` | optional compatible endpoint |
| `LLM_BACKUP_PROVIDER` | optional backup adapter |
| `LLM_BACKUP_MODEL` | optional backup model identifier |
| `LLM_BACKUP_API_KEY` | optional backup secret |
| `LLM_BACKUP_BASE_URL` | optional backup endpoint |
| `REQUEST_DEADLINE_SECONDS` | total internal request deadline, below 30 seconds |
| `LLM_CACHE_SIZE` | bounded in-memory interpretation cache |
| `LOG_LEVEL` | logging level |
| `PORT` | service port |

Never commit real values for secret variables.

---

## 13. Submission and README checklist

The final package requires:

- a working public base URL for both endpoints, with no login/VPN/manual approval;
- the source repository created after question reveal, private during the event and public after the deadline;
- a self-contained README;
- a tested, pullable Docker fallback image with exact tag or digest;
- an accessible solution video no longer than three minutes.

The README must document:

1. architecture and the LLM's mandatory interpretation role;
2. deterministic guardrails and optimizer/solver;
3. clean local setup and exact run command;
4. required environment-variable names, without values;
5. `GET /health` and `POST /optimize-energy` examples;
6. public-sample test command and expected result;
7. Docker pull/run command, port, tag/digest, and runtime configuration;
8. dependencies, credits, limitations, and secret handling;
9. actual provider/model identifier used for judging.

Final verification:

- [ ] `/health` returns HTTP 200 and exactly the required readiness object.
- [ ] `/optimize-energy` accepts the canonical request and returns every required field.
- [ ] Every note has exactly one guarded LLM-produced interpretation in note order.
- [ ] `no_op` and applicable-directive semantics are exact.
- [ ] All directives are hard constraints and no relaxed invalid plan can be returned.
- [ ] The final plan passes independent replay under organizer-style ground truth.
- [ ] Aggregates exactly match the emitted hourly rows within tolerance.
- [ ] Public regression is 10/10 valid and optimal.
- [ ] Deployed p95 and failure rate have been measured under repeated requests.
- [ ] Docker and README work from a clean environment.
- [ ] Repository/image scans find no credentials or sensitive values.
- [ ] Endpoint, repository, image, and video remain accessible for evaluation.

---

## 14. Three-minute video outline

| Time | Content |
|---|---|
| 0:00–0:25 | Problem: notes → supported directives → valid minimum-cost schedule |
| 0:25–1:15 | Architecture: LLM → deterministic guardrails → hard constraints → LP → replay |
| 1:15–2:10 | Live demonstration of `/health` and one public/paraphrased optimization case |
| 2:10–2:40 | Public regression, paraphrase results, latency, and controlled failure handling |
| 2:40–3:00 | Clean run path through README and Docker |

The video carries no base points. It is the first tie-break only after equal total scores.
