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
  Covered by unit tests of `target_rps_at(t)`, the shape `tick()` cap, the
  dynamic rate, and CLI validation. **Four full 480s runs were executed in
  total: three with INFO/access logging and one quiet** against a local 1-worker
  in-memory API (no external LLM, ≤200 users); see the dedicated section below
  for the results and the stale-CSV defect that was found and fixed.
- **Authoritative final stats CSV**: Locust's periodic `--csv` writer rewrites
  `stats_stats.csv` on a timer and can be killed at test stop before it captures
  the very last requests, leaving a stale snapshot that disagrees with the final
  console table and the custom metrics. The locustfile now writes
  `final_stats.csv` from `environment.stats` on the `test_stop` event (public
  `StatsCSV.requests_csv` export), and the runner treats it as the authoritative
  aggregate. If `final_stats.csv` is missing, the step FAILs rather than trusting
  a possibly-stale periodic CSV. The file is written atomically (temp file +
  replace) so a failed export leaves no partial file. The runner also removes any
  stale `final_stats.csv`/`custom_metrics.json` from the output dir before each
  run so a rerun cannot mistake old data for new. The periodic `stats_stats.csv`
  is kept as a sidecar for audit. Covered by
  `test_read_authoritative_aggregate_prefers_final_stats`,
  `test_read_authoritative_aggregate_missing_final_raises`,
  `test_write_final_stats_csv_exports_environment_stats`, and
  `test_reset_step_outputs_removes_stale_files`.
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
- `pytest` — **173 passed, 6 deselected** (re-verified after the latest
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

No RPS/latency figure is claimed beyond the raw diagnostic observations above;
this statement applies only to the old 15-second step-run diagnostic. The real
480s organizer results are documented in the sections below.

## Organizer 480s runs (single worker, in-memory store, `mixed` scenario)

Three noisy full 480s `--profile organizer` runs were executed against a local
1-worker in-memory API (`RESTORATION_STORE_BACKEND=memory`, no external LLM,
`--max-users 200 --rps-per-user 10`). All three are **FAIL** on the RPS gate
(actual ~283–286 RPS vs target average 330.94), and the first two also exposed a
**stale periodic CSV** defect that was fixed. A fourth, quiet run (no
INFO/access logging, `--rps-per-user 5`) is documented in the quiet-server
section below.

### Run 1 and Run 2 — stale periodic CSV defect (fixed)

In both runs the **final console table and the custom metrics agreed exactly**,
but the periodic `stats_stats.csv` was a stale snapshot:

| Run | Console Request Count | Custom `total_http_requests` | Periodic `stats_stats.csv` | Delta |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 137231 | 137231 | 137043 | +188 |
| 2 | 137231 | 137231 | 137093 | +138 |

The custom counters were **correct** all along; the mismatch was purely a
finalization artifact: Locust's periodic CSV writer was killed at test stop
before capturing the last in-flight requests. The `_post` counter logic was
**not** the cause (a stop-in-flight hypothesis was investigated and rejected).
The fix writes an authoritative `final_stats.csv` from `environment.stats` on
`test_stop` and makes the runner use it; the periodic CSV is kept as a sidecar.

### Run 3 — after the fix (counters reconciled)

| Metric | Value |
| --- | ---: |
| Target average RPS | 330.94 |
| Actual RPS | 283.61 |
| Achieved ratio | 0.857 |
| Request Count (`final_stats.csv`) | 136200 |
| Custom `total_http_requests` | 136200 |
| MASK / DEMASK | 65899 / 65353 |
| HTTP 2xx / 422 / 429 / other | 131252 / 4948 / 0 / 0 |
| Functional / transport errors | 0 / 0 |
| p50 / p95 / p99 | 14 / 210 / 230 ms |
| CPU / RAM (load generator) | 13.7% / 78.6 MB |
| Result | **FAIL** |

Verdict: **FAIL** — `achieved ratio 0.857 < 0.900` and `p95 210ms > 200ms`. The
counters reconcile exactly (`final_stats.csv` Request Count == custom
`total_http_requests` == 136200; `mask+demask == http_2xx`; `failures ==
functional+transport == 0`). The run sustained ~284 RPS average over the 480s
profile, below the 330.94 target.

**Important caveat — the server and the load generator are not yet separated.**
With `--rps-per-user 10` the organizer shape spawns only `ceil(target/10)`
users, so at the 1000 RPS peak it launches just **100** users (not the 200 cap),
and the `stats_stats_history.csv` shows a peak of only ~509 RPS. The observed
shortfall is therefore a **proven profile under-delivery**, but it does not by
itself prove the server cannot do more: the generator was not pushing the full
200-user / 1000 RPS target. No confirmed 1000 RPS result exists.

**Generator calibration (60s `--targets 1000`, `--max-users 200`,
`--rps-per-user 5`)** was run to see whether the generator can approach the
target with the full 200-user cap:

| Metric | Value |
| --- | ---: |
| Target RPS | 1000 |
| Actual RPS | 469.25 |
| Achieved ratio | 0.469 |
| Request Count (`final_stats.csv`) | 27942 |
| Custom `total_http_requests` | 27942 |
| p50 / p95 / p99 | 420 / 480 / 500 ms |
| HTTP 2xx / 422 / 429 | 26924 / 1018 / 0 |
| Functional / transport errors | 0 / 0 |
| Result | **FAIL** |

The calibration run reached **469.25 RPS** at the 1000 target with the full
200-user cap. Counters reconcile exactly (`final_stats.csv` == custom == 27942).
This is a single data point in this environment/configuration; it does **not**
localize the bottleneck (server vs. load generator), because server CPU was not
sampled and the generator and server run on the same machine. No confirmed 1000
RPS result exists. The organizer result is left as **FAIL** with the exact
observed numbers above; the bottleneck is not localized. Artifacts are
gitignored under `artifacts/performance/current-review/`.

### Quiet-server reruns (noisy INFO/access logging removed)

**Important**: the runs above (Run 1–3 and the first calibration) were executed
while the test server ran with **INFO/access logging** that emitted ~333 MB of
per-request log lines into the PTY on the same machine as the load generator.
This could distort RPS/latency. The server was restarted with
`LOG_LEVEL=WARNING` and `uvicorn --log-level warning --no-access-log`, and the
calibration and organizer were re-run on the quiet server.

**Quiet calibration (60s `--targets 1000`, `--max-users 200`,
`--rps-per-user 5`)**:

| Metric | Value |
| --- | ---: |
| Target RPS | 1000 |
| Actual RPS | 538.84 |
| Achieved ratio | 0.539 |
| Request Count (`final_stats.csv`) | 32077 |
| Custom `total_http_requests` | 32077 |
| p50 / p95 / p99 | 370 / 420 / 460 ms |
| HTTP 2xx / 422 / 429 | 30889 / 1188 / 0 |
| Functional / transport errors | 0 / 0 |
| Result | **FAIL** |

Throughput was **469.25 → 538.84 RPS** with the logging changed. This is
compatible with logging overhead, but the two runs are single samples and the
variation is not excluded, so no causal share is attributed to the logging
change. Counters reconcile exactly (`final_stats.csv` == custom == 32077).

**Quiet 480s organizer (`--rps-per-user 5`, up to 200 users)**:

| Metric | Value |
| --- | ---: |
| Target average RPS | 330.94 |
| Actual RPS | 290.12 |
| Achieved ratio | 0.877 |
| Request Count (`final_stats.csv`) | 139309 |
| Custom `total_http_requests` | 139309 |
| MASK / DEMASK | 67244 / 66833 |
| HTTP 2xx / 422 / 429 / other | 134077 / 5232 / 0 / 0 |
| Functional / transport errors | 0 / 0 |
| p50 / p95 / p99 | 12 / 380 / 410 ms |
| CPU / RAM (load generator) | 13.9% / 80.9 MB |
| Result | **FAIL** |

Verdict: **FAIL** — `achieved ratio 0.877 < 0.900` and `p95 380ms > 200ms`.
Counters reconcile exactly (`final_stats.csv` == custom == 139309;
`mask+demask == http_2xx`; `failures == functional+transport == 0`). This run
differs from Run 3 in **two** ways at once (quiet logging **and**
`--rps-per-user 10 → 5`), so the change from ~284 → ~290 RPS average is not
attributed to either factor alone; the conditions and numbers are reported as
observed. The `stats_stats_history.csv` shows a **max observed `Requests/s` of
543.8** at a max user count of 200 — an observed peak, not a sustained 1000 RPS.
The 330.94 target is still not reached. The bottleneck (server vs. load
generator) is **not localized**: server CPU was not sampled and both run on the
same machine. No confirmed 1000 RPS result exists. Artifacts are gitignored
under `artifacts/performance/current-review/`.
