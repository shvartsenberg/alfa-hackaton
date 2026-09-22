"""Smoke test for the running service.

Usage:
    python scripts/smoke_test.py [base_url]

Checks /health and a full mask/demask roundtrip with retries.
"""

from __future__ import annotations

import sys

import httpx

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"


def main() -> None:
    client = httpx.Client(base_url=BASE_URL, timeout=10.0)

    health = client.get("/health")
    assert health.status_code == 200, f"health failed: {health.status_code}"
    assert health.json() == {"status": "ok"}
    print("health: ok")

    payload_id = "smoke-1"
    mask = client.post(
        "/process", json={"payload": ORIGINAL, "payload_id": payload_id}
    )
    assert mask.status_code == 200, f"mask failed: {mask.status_code}"
    masked = mask.json()["result"]
    assert masked != ORIGINAL
    print("mask: ok")

    mask_retry = client.post(
        "/process", json={"payload": ORIGINAL, "payload_id": payload_id}
    )
    assert mask_retry.json()["result"] == masked
    print("mask retry: ok")

    demask = client.post(
        "/process", json={"payload": masked, "payload_id": payload_id}
    )
    assert demask.status_code == 200, f"demask failed: {demask.status_code}"
    assert demask.json()["result"] == ORIGINAL
    print("demask: ok")

    demask_retry = client.post(
        "/process", json={"payload": masked, "payload_id": payload_id}
    )
    assert demask_retry.json()["result"] == ORIGINAL
    print("demask retry: ok")

    print("smoke test passed")


if __name__ == "__main__":
    main()