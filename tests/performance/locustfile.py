"""Locust load test for POST /process.

Run against a running server:

    locust -f tests/performance/locustfile.py --host http://localhost:8000

Scenarios for 100 / 500 / 1000 / 2000 RPS can be configured in the Locust UI
or via --users / --spawn-rate. Percentiles (p50/p95/p99) and error rate are
reported by Locust automatically.
"""

from __future__ import annotations

import uuid

from locust import HttpUser, between, task

ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"


class ProcessUser(HttpUser):
    """Simulates a consumer calling POST /process."""

    wait_time = between(0.01, 0.05)

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