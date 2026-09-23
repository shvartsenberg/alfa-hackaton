# Jury Guide

## What this project demonstrates

A PII Security Proxy that masks personal data before it reaches an LLM and
restores it afterwards, with a clean, extensible architecture and a fixed
`POST /process` contract.

## Key points to highlight

1. **Working contract**: `POST /process` implements the exact mask/demask
   contract with idempotent retries. Input is only `payload` + `payload_id`;
   output is only `result`.
2. **Security by design**: sensitive values are never logged or emitted to
   metrics. Restoration state is encrypted at rest (Fernet).
3. **Extensibility**: new PII types, detectors, masking strategies, and
   consumer policies can be added without changing the pipeline.
4. **Team-ready structure**: modules are split so four developers can work in
   parallel.
5. **Performance tooling**: the hot path has no external I/O, no LLM, no
   filesystem access; a Locust load-test runner and a synchronous benchmark are
   included with persistent reports. No RPS/SLA figure is claimed without a
   validated, consistent report (see "Load test" below).

## Contract: POST /process

```json
// Request
{ "payload": "<string>", "payload_id": "<string>" }

// Response (200)
{ "result": "<string>" }
```

State machine per `payload_id`:

| State | Input | Result |
| --- | --- | --- |
| New `payload_id` | original text | MASK, returns mask |
| MASKED | same original | retry MASK, same mask (idempotent) |
| MASKED | mask | DEMASK, returns original |
| DEMASKED | mask | retry DEMASK, same original (idempotent) |
| MASKED/DEMASKED | mismatched text | 422 `InvalidPayloadError` |

Every successful roundtrip guarantees: the mask contains no known original PII;
DEMASK fully equals the original; retry returns an identical result.

## Demo flow

1. Start the server (local or Docker, see below).
2. `GET /health` returns `{"status": "ok"}`.
3. `POST /process` with an original text returns a masked result.
4. `POST /process` again with the masked text returns the original.
5. Retrying either request returns the same result (idempotency).
6. Optional: pass `X-Consumer-ID: demo` to select a different consumer policy.

## Local run

```bash
pip install -e ".[dev]"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker run

```bash
export MASKING_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
docker compose up --build
```

`docker compose config` validates the compose file. Redis is not exposed to the
host; only the `api` service reaches it.

## Configuration

- **MASKING_KEY**: Fernet key, required when `RESTORATION_STORE_BACKEND=redis`
  (identical across workers). Generate with the command above.
- **Redis password**: the current `docker-compose.yml` does **not** enable Redis
  auth (`redis-server` runs without `--requirepass`; `REDIS_URL` has no
  password). For an external secured Redis use `redis://:password@host:6379/0`.
  A compose config with a real password is a Core-owner change, not shipped here.
- **X-Consumer-ID**: optional header selecting a consumer policy; unknown or
  disabled consumers get HTTP 403.

## Health / readiness

- `GET /health` → `{"status": "ok"}` (liveness; no dependency check).
- `GET /health/ready` → `{"status": "ready"}` (readiness; checks dependencies,
  may return `503 {"status": "not_ready"}`).
- `GET /metrics` → Prometheus metrics (does not change the `/process` contract).

## Benchmark

```bash
python -m benchmarks.score
```

Reports span-level precision/recall/F1 per type and overall. The benchmark
fails if a required type represented in the dataset has recall below 0.5,
overall recall below 0.8, or overall F1 below 0.8. A zero-recall required
type therefore cannot be hidden by the overall result.

## Load test

```bash
python scripts/run_performance.py --host http://localhost:8000 \
  --targets 100,330,500,1000 --scenario mixed
```

**Methodology**: each target runs as a **separate step** of `--duration`
(default 60 s) with linear user spawn within that step only. This is **not** a
single smooth profile averaging ~330 RPS with peaks to 1000 RPS. The `mixed`
scenario is weighted so MASK == DEMASK **in expectation**, not strictly per run
(interrupted roundtrips and 429s can unbalance actual counts). It covers retry
MASK, retry DEMASK, payload_id conflicts, and a separate large-payload scenario.
No external LLM. Reports persist to `artifacts/performance/`.

**Honest status**: the only saved local run
(`artifacts/performance/20260923T000000Z/`) is a **preliminary diagnostic** with
15-second steps and **inconsistent counters** (e.g. `rps-500` CSV
`Request Count=7055` vs custom `http_2xx=7243`). It is **not** a valid PASS and
yields **no** capacity/SLA estimate. Those artifacts are gitignored and are not
in the PR/ZIP. Redis store, multiworker, and large-payload scenarios were **not**
verified in that run.

### Organizer continuous profile (`--profile organizer`)

A separate opt-in mode that reproduces the organizers' requested load shape as a
**single continuous** Locust run (no process restart between phases), driven by
a `LoadTestShape` + dynamic `wait_time`:

```bash
python scripts/run_performance.py --host http://localhost:8000 \
  --profile organizer --scenario roundtrip
```

The main SLA scenario is `roundtrip` (strict MASK → DEMASK with paired
counters). `mixed` remains an additional stress profile; retry, conflict and
large payload run as separate scenarios.

480s profile: ramp 0→330 (0–180s), steady 330 (180–300s), peak 1000 (300–330s),
decay 1000→330 (330–360s), steady 330 (360–480s). Target average = **330.9375
RPS**; max concurrent `FastHttpUser` is hard-capped at **200** (CLI rejects
`--max-users > 200` in organizer mode; the locustfile also caps at 200 as
defense-in-depth). `--profile organizer` is incompatible with `--targets`.
Actual RPS/latency/status/errors come from the report, not the formula;
`summary.json` includes profile metadata (phases, `target_average_rps`,
`actual_average_rps`, `target_vs_actual_delta`).

> **Previously reported Redis run (4 workers, roundtrip).** A full 480s
> organizer run against a 4-worker Redis deployment was previously reported to
> achieve **307.62 RPS** average (target 330.94), p50 17ms, p95 130ms, p99
> 160ms, MASK 73867 / DEMASK 73851 / incomplete 16, 0 errors. This is a
> **previously reported** measurement: its artifacts were saved under the old
> schema and do **not** confirm the backend/worker configuration. `configured_max_users`
> (200) is the configured limit; `observed_max_users` (~100) is the actual
> concurrency from the history CSV. `observed_peak_rps` (~852) is the actual
> peak; **1000 RPS is not confirmed**. Use `--required-concurrent-users 200` to
> force the generator to create 200 users. The main production profile is Redis;
> memory is diagnostic only.

## Submission ZIP

```bash
python scripts/package_submission.py submission.zip
python scripts/package_submission.py --validate submission.zip
python scripts/package_submission.py --smoke submission.zip
```

The archive is deterministic and excludes `.env*` (except `.env.example`), VCS,
virtualenvs, caches, `.pyc`, `.egg-info`, previous ZIPs, performance artifacts,
and IDE metadata. Smoke validation unpacks, installs, imports `app.main`,
runs MASK/DEMASK, checks exact restore, and runs a quick pytest subset.

## Honest limitations

- The earlier zero-recall `PASSPORT_ISSUER` defect was fixed in `main`. The
  local benchmark now passes (8/8 issuer spans; overall F1 0.995). Remote CI
  and hidden organizer data are not covered by this local result.
- In-memory restoration store is single-process; use Redis for multiple workers.
- No real NER, no external LLM in the hot path (LLM detector is optional and
  off by default).
- Rate limiting is global and returns 429 with `Retry-After`.
- Authentication/authorization are not implemented.
- The current `docker-compose.yml` does not enable Redis authentication.
- No RPS or latency figures are claimed without a validated, consistent report;
  the only saved run is a preliminary diagnostic with inconsistent counters and
  short duration (see "Load test").
