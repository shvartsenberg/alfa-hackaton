# QA / Observability Report (Participant 4)

This document records observations, defects found in other modules, and
contract questions that require a decision from the Core owner. It does not
change the public `POST /process` contract.

## Summary of changes

- **Metrics**: cumulative Prometheus counters/histograms, HTTP status metrics,
  `GET /metrics`, and `MetricsRecorder.record_error()` in error handlers.
- **Logging**: structured JSON by default (`LOG_FORMAT=json`); the JSON
  formatter never includes exception text/tracebacks (may contain PII).
- **Error handling**: unexpected exceptions return a safe `INTERNAL_ERROR`
  body; only the exception type is logged.
- **Duration**: `ProcessOutcome.duration_ms` is now populated from the real
  timer in `ProcessService.process()`.
- **Restoration store**: expired entries are evicted first; if the store is
  still full, new state is rejected with 429 and all live mappings are kept.
- **Tests**: isolated dependency singletons, unique `payload_id`s, a
  `TestClient` with `raise_server_exceptions=False`, and markers
  (`integration`, `concurrency`, `performance`, `slow`).
- **Performance**: selectable Locust scenarios (retries, shared payload_id,
  large payload) and a target-RPS runner with persistent CSV/JSON/Markdown reports.
- **CI**: GitHub Actions workflow (ruff, mypy, pytest, submission validation).
  The local quality benchmark now passes after the detection changes in `main`
  (see defect #6); remote CI is not claimed green. `docker compose config` uses a safe test
  `MASKING_KEY`; `git diff --check` compares against the merge base.
- **Packaging**: `.gitignore` added; generated artifacts removed from git;
  `package_submission.py` now validates the ZIP, blocks `.env` files, enforces a
  required file/dir manifest (Dockerfile, compose, tests, scripts, docs), and
  treats a real pytest-subset failure as an error (skip only when the dev
  toolchain is objectively absent).

## Contract question (needs Core owner decision)

### Behaviour after DEMASK

Current behaviour: after a successful DEMASK, the state is marked `DEMASKED`.
A subsequent request with the **original** payload returns
`InvalidPayloadError` (422), because the state machine only accepts the masked
payload for re-demasking.

This is intentional per the current state machine, but it is a contract
question worth confirming:

- **Option A (current)**: after DEMASK, only the masked payload may be
  re-demasked; the original payload is rejected. Strict one-shot demask.
- **Option B**: after DEMASK, the original payload is accepted again and
  re-masked (full cycle restart).
- **Option C**: after DEMASK, the state is deleted so a new MASK starts fresh.

No change was made to this behaviour. A regression test documents the current
semantics.

## Defects / limitations found in other modules

### 1. Atomic lifecycle for one `payload_id`

`ProcessService.process()` protects the full `get -> route -> save` transition
with a bounded striped-lock table. Requests for the same `payload_id` are
serialized without retaining an unbounded dictionary of locks. Concurrency
tests cover identical MASK retries, DEMASK retries, and competing different
originals. This guarantee is process-local; multiple Uvicorn workers still
require a shared store with distributed atomic transitions.

### 2. `restoration_max_entries` could be exceeded

Previously, if the store was full of **live** (non-expired) entries, `save()`
would still insert a new entry, exceeding `max_entries`.

- **Fix applied**: `save()` evicts expired entries, but preserves every live
  reversible mapping. If capacity is still exhausted, the request receives
  `ServiceOverloadedError`/HTTP 429 with `Retry-After`.

### 3. `ProcessOutcome.duration_ms` was always `0.0`

The process log reported an incorrect duration.

- **Fix applied**: `process()` now sets `outcome.duration_ms` from the timer.

### 4. `MetricsRecorder.record_error()` was not wired in

Error counters were never incremented.

- **Fix applied**: error handlers now call `record_error()`.

### 5. `logger.exception("unexpected_error")` could leak PII

The exception message/traceback may contain PII.

- **Fix applied**: unexpected-error handler logs only the exception type.

### 6. `PASSPORT_ISSUER` zero recall — resolved in `main`

Previously, `_PASSPORT_ISSUER_RE` captured at most 5 words after the marker
(`{0,5}`), so the issuing authority name is truncated and the detected span
does not match the expected span in `benchmarks/dataset.yaml`:

- `pos_issuer_001`: detected `Отделом УФМС России` (14–33), expected
  `Отделом УФМС России по г. Москве` (14–46).
- `pos_issuer_002`: detected `ГУ МВД России` (11–24), expected
  `ГУ МВД России по Московской области` (11–46).

Before the detection changes, `PASSPORT_ISSUER` recall was 0.0 (0 TP, 2 FN).
The merged `main` detector and expanded dataset now yield 8 TP, 0 FN for
this type (recall 1.0); the local quality benchmark passes with overall F1
0.995. In the conflict resolution, the stricter `main` quality gate was kept:
required types have a 0.5 recall floor, overall recall and F1 have 0.8 floors.
These are local-dataset results, not a claim about hidden organizer data.

## Performance notes

- The hot path has no external I/O, no LLM, and no filesystem access.
- The in-memory restoration store is single-process; do **not** scale with
  multiple Uvicorn workers, as state would not be consistent across processes.
- Achieved RPS figures must come from a real Locust run
  (`scripts/run_performance.py`); no RPS claim is made here without a run.
- **Methodology**: each target runs as a separate step of `--duration`
  (default 60 s) with linear user spawn within that step only. This is **not** a
  single smooth profile averaging ~330 RPS with peaks to 1000 RPS. The `mixed`
  scenario is weighted so MASK == DEMASK **in expectation**, not strictly per
  run (interrupted roundtrips and 429s can unbalance actual counts). It covers
  retry MASK, retry DEMASK, payload_id conflicts, and a separate large-payload
  scenario. No external LLM.
- Reports persist target RPS, actual RPS, average latency, p50/p95/p99, request
  count, MASK/DEMASK counts, HTTP 2xx, HTTP 429, HTTP 422, other HTTP statuses,
  functional errors, transport errors, and CPU/RAM of the load generator (when
  `psutil` is installed; otherwise `n/a`, not FAIL). 429 is counted separately
  from correctness errors and is only accepted with `Retry-After`. HTTP status
  counters are reconciled strictly against the Locust `Request Count`; a
  mismatch marks the report FAIL rather than a valid PASS. The Locust `Failure
  Count` is also reconciled against the custom `functional_errors +
  transport_errors` (valid 429s and expected 422s are not failures); a mismatch
  marks the report FAIL. This gate is covered by
  `test_build_result_flags_failure_count_mismatch`.
- **Not verified** in the saved run: Redis store, multiworker, large payloads.
- **Organizer continuous profile** (`--profile organizer`): a separate opt-in
  mode reproducing the organizers' requested load shape as a single continuous
  480s Locust run (ramp 0→330, steady 330, peak 1000, decay 1000→330, steady
  330; target average 330.9375 RPS). Max concurrent users is hard-capped at 200:
  the CLI rejects `--max-users > 200` in organizer mode and the locustfile caps
  at 200 as defense-in-depth. Driven by a `LoadTestShape` + dynamic `wait_time`;
  incompatible with `--targets`. Actual RPS/latency/status/errors come from the
  report; `summary.json` includes profile metadata (phases,
  `target_average_rps`, `actual_average_rps`, `target_vs_actual_delta`).
  **Not verified by a real full 480s run** — implemented and covered by unit
  tests of `target_rps_at(t)`, the shape `tick()` cap, the dynamic rate, and CLI
  validation. A short executable smoke was run against a local app
  (`LOAD_PROFILE=organizer`, `ORGANIZER_DURATION=5`, `ORGANIZER_MAX_USERS=5`,
  `ORGANIZER_RPS_PER_USER=1`): exit 0, 19 requests, 0 failures, 0
  functional/transport/429, custom `total_http_requests=19` == CSV Aggregated
  `Request Count=19` (counters reconciled). This confirms the shape + dynamic
  wait_time + custom-metrics-on-stop work end-to-end; it is **not** a capacity
  or SLA result and does not replace the full 480s run. The earlier low-load
  smoke belongs to the old scenario version and is not evidence for this
  profile.
- **Redis auth**: the current `docker-compose.yml` does not enable Redis
  authentication (`redis-server` runs without `--requirepass`; `REDIS_URL` has
  no password). A compose config with a real password is a Core-owner change.
- **Health**: `GET /health` is liveness (always 200); `GET /health/ready` is
  readiness and may return 503 when dependencies are unavailable.

## Final validation (local, Windows)

The following were executed and passed on this machine:

- `pip install -e ".[dev]"` — OK (missing `cryptography`/`redis`/`fakeredis`
  installed first).
- `ruff check .` — OK.
- `mypy app scripts benchmarks` — OK (56 files).
- `pytest` — **168 passed, 6 deselected** (re-verified after the latest
  changes).
- `pytest -m concurrency` — 4 passed.
- `python -m benchmarks.score` — OK after merging `main`: overall F1 0.995,
  `PASSPORT_ISSUER` recall 1.0 (8/8). Remote CI is not yet verified here.
- `python scripts/package_submission.py artifacts/pytest-qa-final/submission.zip`
  — OK (**91 files**, re-verified after the latest changes).
- `python scripts/package_submission.py --validate artifacts/pytest-qa-final/submission.zip`
  — OK.
- `python scripts/package_submission.py --smoke artifacts/pytest-qa-final/submission.zip`
  — OK (incl. pytest subset: ok).
- `git diff --check` — OK (local working-tree check; CI diffs against the PR
  base branch or the previous push commit).
- `docker compose config` — **not run**: Docker is not installed on this
  machine; it runs in CI with a safe test `MASKING_KEY`.

### Harness smoke on low load (not a capacity confirmation)

A live harness run was executed against a local server to verify the runner and
counter reconciliation end-to-end at low load (not as a capacity/SLA result):

```bash
python scripts/run_performance.py --host http://127.0.0.1:18081 \
  --targets 5 --duration 30 --rps-per-user 1 --spawn-rate 5 \
  --output-dir artifacts/performance/review-smoke-20260923
```

Result: exit 0. CSV `Request Count=144`; custom metrics `137` 2xx + `7` expected
422 = `144` (reconciled); `0` functional/transport/429; actual `5.274` RPS;
avg `12.712` ms, p50 `10` ms, p95 `35` ms, p99 `44` ms. This confirms the
harness (runner, counters, expected-422 handling) works; it is **not** a
capacity or SLA measurement.

### Real load test (single worker, in-memory store, `mixed` scenario)

> **Preliminary diagnostic only — NOT a valid PASS.** Each step ran for only
> **15 seconds** (too short for stable percentiles) and the counters are
> **inconsistent**: in `rps-500` the Locust CSV `Request Count=7055` while the
> custom `http_2xx=7243`; other steps disagree similarly. No stable ~500 RPS,
> achieved SLA, or exact performance conclusion is claimed from this run.

A real Locust run was executed against a live `uvicorn` server (1 worker,
`RESTORATION_STORE_BACKEND=memory`). Report saved to
`artifacts/performance/20260923T000000Z/`:

| Target RPS | Actual RPS | avg | p50 | p95 | p99 | MASK | DEMASK | 2xx | 429 | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| 100 | 101.1 | 20.3 ms | 18 ms | 39 ms | 51 ms | 725 | 711 | 1436 | 0 | diagnostic |
| 330 | 331.1 | 50.7 ms | 48 ms | 83 ms | 95 ms | 2399 | 2377 | 4776 | 0 | diagnostic |
| 500 | 501.2 | 52.1 ms | 52 ms | 88 ms | 110 ms | 3619 | 3624 | 7243 | 0 | diagnostic |
| 1000 | 498.5 | 197.9 ms | 200 ms | 230 ms | 250 ms | 3662 | 3601 | 7263 | 0 | diagnostic |

Findings (raw observations, not validated):

- The single-worker in-memory server reached ~500 RPS on the 100/330/500 steps
  (ratio ≈ 1.0) and ~498 RPS on the 1000 step, but these are **not** validated
  PASS results: the run was too short and the counters are inconsistent.
- **0 HTTP 429, 0 functional errors, 0 transport errors** were recorded, but the
  counter mismatch means these figures are not trustworthy as-is.
- CPU of the load generator scaled 10% → 28%; RAM stable ~66–71 MB.

The saved artifacts are gitignored and are **not** in the PR/ZIP. For a valid
rerun with consistent counters and stable percentiles:

```bash
python scripts/run_performance.py --host http://localhost:8000 \
  --targets 100,330,500,1000 --scenario mixed --duration 120
```

No RPS/latency figure is claimed beyond the raw diagnostic observations above.
