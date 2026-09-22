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
- **Packaging**: `.gitignore` added; generated artifacts removed from git;
  `package_submission.py` now validates the ZIP and blocks `.env` files.

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

## Performance notes

- The hot path has no external I/O, no LLM, and no filesystem access.
- The in-memory restoration store is single-process; do **not** scale with
  multiple Uvicorn workers, as state would not be consistent across processes.
- Achieved RPS figures must come from a real Locust run
  (`scripts/run_performance.py`); no RPS claim is made here without a run.
