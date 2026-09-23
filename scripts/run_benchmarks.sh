#!/usr/bin/env bash
# Reproducible performance run commands.
#
# These commands produce comparable runs: same workers, logging, duration,
# shape, gates, generator, and (as far as controllable) hardware.
#
# The main production profile is Redis. Memory is for diagnostics only and is
# NOT the production benchmark.
#
# Usage: source this file or copy the commands you need.

set -euo pipefail

# A valid Fernet key (generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export MASKING_KEY="${MASKING_KEY:-IJhFOHCsPnO7Tr6FErj5PPTE5og8_wCVCF-EvR7gjeA=}"
export REDIS_PASSWORD="${REDIS_PASSWORD:-temp-perf-password}"

HOST="${HOST:-http://localhost:8000}"
OUT="${OUT:-artifacts/performance}"

# --- 1. Memory backend (diagnostic only, 1 worker) --------------------------
# Start the server:
#   uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level warning
# Then:
#   python scripts/run_performance.py --host "$HOST" \
#     --profile organizer --scenario roundtrip --max-users 200 \
#     --output-dir "$OUT/final-memory"

# --- 2. Redis backend (production, 4 workers) -------------------------------
# Start Redis:
#   docker run -d --name pii-redis -p 6380:6379 redis:7-alpine \
#     redis-server --requirepass "$REDIS_PASSWORD"
# Start the server (4 workers, Redis):
#   RESTORATION_STORE_BACKEND=redis \
#   REDIS_URL="redis://:$REDIS_PASSWORD@127.0.0.1:6380/0" \
#   MASKING_KEY="$MASKING_KEY" \
#   uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --log-level warning
# Then:
#   python scripts/run_performance.py --host "$HOST" \
#     --profile organizer --scenario roundtrip --max-users 200 \
#     --output-dir "$OUT/final-organizer-redis"

# --- 3. Final organizer-like Redis roundtrip (main SLA) ---------------------
#   python scripts/run_performance.py --host "$HOST" \
#     --profile organizer --scenario roundtrip --max-users 200 \
#     --required-concurrent-users 200 \
#     --output-dir "$OUT/final-organizer"

# --- 4. Separate 200 concurrent users/connections check ---------------------
#   python scripts/run_performance.py --host "$HOST" \
#     --profile steps --targets 1000 --scenario roundtrip --max-users 200 \
#     --required-concurrent-users 200 --duration 120 \
#     --output-dir "$OUT/concurrency-200"

# --- 5. Short smoke for CI (memory, 30s) ------------------------------------
#   python scripts/run_performance.py --host "$HOST" \
#     --profile steps --targets 100 --scenario roundtrip --max-users 20 \
#     --duration 30 --output-dir "$OUT/smoke-memory"

# --- 6. Short Redis roundtrip smoke -----------------------------------------
#   python scripts/run_performance.py --host "$HOST" \
#     --profile steps --targets 100 --scenario roundtrip --max-users 20 \
#     --duration 30 --output-dir "$OUT/smoke-redis"

echo "Reproducible run commands documented. Copy the relevant block above."