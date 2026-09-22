# Jury Guide

## What this project demonstrates

A PII Security Proxy that masks personal data before it reaches an LLM and
restores it afterwards, with a clean, extensible architecture.

## Key points to highlight

1. **Working contract**: `POST /process` implements the exact mask/demask
   contract with idempotent retries.
2. **Security by design**: sensitive values are never logged or emitted to
   metrics from the first commit.
3. **Extensibility**: new PII types, detectors, masking strategies, and
   consumer policies can be added without changing the pipeline.
4. **Team-ready structure**: modules are split so four developers can work in
   parallel.
5. **Performance-ready**: the hot path has no external I/O, no LLM, no
   filesystem access; a Locust load-test skeleton is included.

## Demo flow

1. Start the server.
2. `GET /health` returns `{"status": "ok"}`.
3. `POST /process` with an original text returns a masked result.
4. `POST /process` again with the masked text returns the original.
5. Retrying either request returns the same result (idempotency).

## Honest limitations

- Only EMAIL and PHONE are detected (regex-based).
- In-memory restoration store (single process).
- No real NER, no real LLM, no authentication, no rate limiting yet.
- These are documented extension points, not gaps in the contract.