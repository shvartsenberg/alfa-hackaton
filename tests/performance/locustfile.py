"""Locust load test for POST /process.

Reproduces the target load profile:
- 200 simultaneous connections (users), each waits for the response before
  sending the next request (synchronous, keep-alive).
- ~330 RPS average, peaks up to 1000 RPS, with smooth ramp-up.
- To sustain 1000 RPS at 200 connections, latency must stay <= ~200ms.

Run against a running server:

    locust -f tests/performance/locustfile.py --host http://localhost:8000 \
        --users 200 --spawn-rate 20 --run-time 5m

`--spawn-rate 20` gives a smooth ramp-up (200 users over 10s). Percentiles
(p50/p95/p99) and error rate are reported by Locust automatically.

Scenarios for 100 / 500 / 1000 / 2000 RPS are selected by adjusting
`--users` and `--spawn-rate`; the per-user behaviour stays the same.
"""

from __future__ import annotations

import uuid

from locust import HttpUser, task

ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"


class ProcessUser(HttpUser):
    """Synchronous consumer: waits for each response before the next request.

    ``wait_time`` is intentionally 0 so each user issues requests back-to-back,
    matching the "each connection waits for the answer" profile.
    """

    wait_time = None

    @task
    def mask_and_demask(self) -> None:
        payload_id = str(uuid.uuid4())
        with self.client.post(
            "/process",
            json={"payload": ORIGINAL, "payload_id": payload_id},
            catch_response=True,
        ) as mask_resp:
            if mask_resp.status_code != 200:
                mask_resp.failure(f"mask failed: {mask_resp.status_code}")
                return
            masked = mask_resp.json()["result"]
        with self.client.post(
            "/process",
            json={"payload": masked, "payload_id": payload_id},
            catch_response=True,
        ) as demask_resp:
            if demask_resp.status_code != 200:
                demask_resp.failure(f"demask failed: {demask_resp.status_code}")
                return
            if demask_resp.json()["result"] != ORIGINAL:
                demask_resp.failure("demask did not restore original")