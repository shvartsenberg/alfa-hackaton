# Security

This document separates what is **provided by the architecture** from what is
**implemented today**. The skeleton does not claim to satisfy all banking
information-security requirements.

## Sensitive data

The following are considered sensitive and must never be logged or emitted to
metrics:

- `payload`
- `original_text`
- masked mapping values
- `PIIEntity.value`
- request bodies

## What may be logged

- `request_id`
- `payload_id` hash (see `app/core/security.py`)
- `consumer_id`
- `operation` (MASK / DEMASK)
- detected PII **types**
- number of entities
- latency
- status
- error type

## Restoration state lifecycle

- Created on the first MASK request for a `payload_id`.
- Stores the original text and the mask-to-original mappings.
- Marked `DEMASKED` after the original is restored.
- Expires after a configurable TTL (`RESTORATION_TTL_SECONDS`).

## TTL

- Default TTL: 3600 seconds.
- Expired entries are evicted lazily on access and when the store is full.

## Metrics

- No payload content is placed in metric labels.
- Only counts, timings, and PII types are recorded.
- Histograms use cumulative Prometheus bucket counters and do not retain raw
  observations in process memory.
- Exposed via `GET /metrics` in the Prometheus text format.
- HTTP labels are limited to method, normalized route and status code.
- `payload_id`, its hash and `request_id` are never used as metric labels.

## Logging

- Logs are structured JSON by default (`LOG_FORMAT=json`).
- Each HTTP response contains a validated/generated `X-Request-ID` header.
- The JSON formatter never includes exception text or tracebacks, which may
  contain PII; only the exception type is recorded.
- Unexpected-error handlers log only the exception type, never the message.

## Error responses

- Stack traces are never returned to the client.
- Unexpected exceptions return a generic `INTERNAL_ERROR` body; exception
  text, PII, and tracebacks are never leaked to the response.

## Storage

### Provided by architecture

- `RestorationStore` abstraction allows swapping the in-memory store for a
  Redis-backed store.
- `RedisRestorationStore` is implemented and selected via
  `RESTORATION_STORE_BACKEND=redis`, enabling multiple workers to share state.
- Both stores encrypt the full restoration state (including `original_text`
  and mappings) with Fernet before writing it, so PII never appears in
  plaintext at rest.
- Redis keys are derived from an HMAC of `payload_id`, so the raw identifier
  is never used as a key.
- In Redis mode the `MASKING_KEY` must be set; a missing key fails fast at
  startup instead of silently generating a random one (which would make state
  unrecoverable across workers).

### Implemented today

- `InMemoryRestorationStore` (single process, not horizontally scalable).
- A full store preserves live mappings and rejects new state with HTTP 429;
  it never evicts a reversible mapping before its TTL expires.
- `RedisRestorationStore` (shared across workers, uses Redis TTL, atomic
  compare-and-set lifecycle via WATCH/MULTI).
- Encryption at rest (Fernet) for both stores.

## Redis configuration

- Set `MASKING_KEY` to a Fernet key (identical across all workers). Generate
  with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- The key must be identical across all workers and must never be committed.
  In production (`APP_ENV=production`) a missing key fails fast at startup
  instead of generating a random one that would be unrecoverable across
  restarts.
- Redis auth: the `redis` service in `docker-compose.yml` requires a password
  (`REDIS_PASSWORD`). Generate with:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  The password is required (`${REDIS_PASSWORD:?REDIS_PASSWORD is required}`),
  passed to `redis-server --requirepass`, used by the healthcheck
  (`redis-cli -a ... ping`), and embedded in the app `REDIS_URL`
  (`redis://:${REDIS_PASSWORD}@redis:6379/0`). It must never be committed.
- TLS: terminate TLS at a reverse proxy / load balancer in front of the API,
  or use a Redis with a signed server certificate. mTLS is not required.
- The Redis port is not exposed to the host in `docker-compose.yml`; only the
  `api` service reaches it.

## Threat model (initial implementation)

| Threat | Status |
| --- | --- |
| PII leakage via logs | Mitigated: sensitive fields are never logged |
| PII leakage via metrics | Mitigated: no payload in labels |
| Retry corrupting state | Mitigated: explicit state machine + atomic CAS |
| Horizontal scaling | Supported via Redis store |
| Encryption at rest | Implemented (Fernet) for memory and Redis |
| Authentication / authorization | Not implemented |
| Rate limiting | Implemented (global, returns 429 with Retry-After) |
