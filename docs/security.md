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

## Storage

### Provided by architecture

- `RestorationStore` abstraction allows swapping the in-memory store for a
  Redis-backed store with optional encryption at rest.
- `RedisRestorationStore` is implemented and selected via
  `RESTORATION_STORE_BACKEND=redis`, enabling multiple workers to share state.

### Implemented today

- `InMemoryRestorationStore` (single process, not horizontally scalable).
- `RedisRestorationStore` (shared across workers, uses Redis TTL).
- No encryption at rest.

## Threat model (initial implementation)

| Threat | Status |
| --- | --- |
| PII leakage via logs | Mitigated: sensitive fields are never logged |
| PII leakage via metrics | Mitigated: no payload in labels |
| Retry corrupting state | Mitigated: explicit state machine |
| Horizontal scaling | Not supported (in-memory store) |
| Encryption at rest | Not implemented |
| Authentication / authorization | Not implemented |
| Rate limiting | Not implemented (429 handler exists) |