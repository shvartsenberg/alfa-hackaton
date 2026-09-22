# API

## POST /process

Masks or demasks a payload identified by `payload_id`.

### Request

```json
{
  "payload": "<string>",
  "payload_id": "<string>"
}
```

### Response

```json
{
  "result": "<string>"
}
```

### Behaviour

- First request with a new `payload_id` performs masking and stores state.
- A request with the previously returned mask performs demasking.
- Both stages are idempotent under retry.

### Example

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"payload": "Напишите мне на test@example.com или +7 999 123-45-67", "payload_id": "example-1"}'
```

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"payload": "<masked text>", "payload_id": "example-1"}'
```

## GET /health

```json
{
  "status": "ok"
}
```

## Error responses

| Status | Meaning |
| --- | --- |
| 400 | Bad request |
| 403 | Consumer not allowed |
| 404 | Restoration state not found |
| 422 | Invalid payload |
| 429 | Too many requests (includes `Retry-After`) |
| 500 | Internal error |
| 503 | Service unavailable |

Stack traces are never returned to the client.