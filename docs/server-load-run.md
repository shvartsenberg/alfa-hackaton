# Server load run (480s Redis organizer profile)

This document gives the exact commands for the **user** to run a real 480-second
Redis-backed organizer load test on the RDP server with Docker Desktop. The
commands are safe: secrets are generated without being printed to the console
and are never committed to git.

> **Status:** the new reporting schema (peak CPU/RAM, during-run Docker
> sampling, ESTABLISHED TCP connection sampling, honest `server_configuration`)
> is **not yet verified by a full production run**. This document prepares that
> run. Do **not** claim PASS until a real run completes and the report is
> inspected.

## Prerequisites

- Docker Desktop is running on the server.
- The repository is checked out at the project root
  (`C:\Users\Sharcenberg\Projects\Python\alfa-hackaton`).
- Python 3.12+ and the dev toolchain are available (see "Install dev deps").

## 1. Generate secrets without printing them

Run from the repository root. The secrets are written to a temporary file that
is **gitignored** (`.env.perf` matches `.env.*` in `.gitignore`) and never
printed to the console.

```powershell
# Generate a unique Redis password and Fernet masking key.
$redisPass = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
$maskingKey = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Write them to a gitignored env file (never committed).
@"
REDIS_PASSWORD=$redisPass
MASKING_KEY=$maskingKey
"@ | Set-Content -Path .env.perf -Encoding UTF8

# Confirm the file is gitignored (should print .env.perf).
git check-ignore .env.perf
```

> The secrets are only in `.env.perf`, which is excluded by `.gitignore`
> (`.env.*`). They are never echoed to the console and never committed.

## 2. Install dev dependencies (including psutil)

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`psutil` is required for CPU/RAM and ESTABLISHED TCP connection sampling of the
Locust process. If it is missing, the runner reports those values as
`unavailable` (never a false zero).

## 3. Start the 4-worker Redis API

The performance overlay `docker-compose.perf.yml` overrides the `api` service to
run **4 uvicorn workers** with **no access log** and **WARNING** log level. It
does **not** touch the `redis` service or any other container.

```powershell
# Load the secrets from the gitignored env file.
Get-Content .env.perf | ForEach-Object {
  if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "Env:$($matches[1])" -Value $matches[2] }
}

# Build and start (api + redis) with the performance overlay.
docker compose -f docker-compose.yml -f docker-compose.perf.yml up --build -d

# Wait for readiness and confirm the api container is running 4 workers.
docker compose -f docker-compose.yml -f docker-compose.perf.yml ps
```

Check the api container is healthy and the service is ready:

```powershell
# Readiness (checks Redis dependency; returns 200 only when ready).
Invoke-RestMethod -Uri http://localhost:8000/health/ready
```

## 4. Run the 480s organizer profile

Run from the repository root. The runner samples the Locust process's own
ESTABLISHED TCP connections to `localhost:8000` and gates on
`--required-concurrent-connections 200`.

```powershell
.\.venv\Scripts\python.exe scripts/run_performance.py `
  --host http://localhost:8000 `
  --profile organizer `
  --scenario roundtrip `
  --rps-per-user 5 `
  --max-users 200 `
  --required-concurrent-users 200 `
  --required-concurrent-connections 200 `
  --min-peak-ratio 1.0 `
  --server-config backend=redis,workers=4,logging=quiet `
  --output-dir artifacts/performance/server-organizer
```

The run takes ~480 seconds. The report is written to
`artifacts/performance/server-organizer/` (`summary.json`, `summary.md`,
`final_stats.csv`, `custom_metrics.json`, `stats_stats_history.csv`).

> The `--server-config` value is recorded as `operator_declared_unverified` and
> is **not** proof of the server configuration; the runner does not verify it.
> The raw command line is never persisted (only a strict allowlist of safe
> parameters is stored), so secrets cannot leak into the report.

## 5. Inspect the result

```powershell
# Exit code 0 means all gates passed (RPS, latency, error rate, concurrency,
# peak RPS, and concurrent connections).
$LASTEXITCODE

# Show the key result fields.
.\.venv\Scripts\python.exe -c "import json; d=json.load(open('artifacts/performance/server-organizer/summary.json', encoding='utf-8')); r=d['results'][0]; print('passed:', r['passed']); print('violations:', r['violations']); print('observed_peak_connections:', r['observed_peak_connections']); print('observed_peak_rps:', r['observed_peak_rps']); print('observed_max_users:', r['observed_max_users'])"
```

## 6. Tear down (optional)

```powershell
docker compose -f docker-compose.yml -f docker-compose.perf.yml down
```

This stops only this compose project's containers; it does **not** remove other
containers.

## Honesty notes

- **PASS is only valid after a real run** with exit code 0 and no violations.
  Do not claim PASS from this document alone.
- **1000 RPS** and **200 concurrent HTTP connections** are only proven if the
  report shows `observed_peak_rps >= 1000` and
  `observed_peak_connections >= 200` with no violations.
- **Active Locust users are not simultaneous HTTP connections.** The
  `observed_peak_connections` value is the actual measured ESTABLISHED TCP
  connections from the Locust process to the target, sampled via `psutil`.
- If `psutil` is unavailable or connection sampling is denied, the runner
  reports `connections_measured: false` and the
  `--required-concurrent-connections` gate **cannot pass** (it is not a false
  zero).