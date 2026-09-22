"""Configurable Locust scenarios for the PII proxy.

Environment variables:
    LOAD_SCENARIO: mask_only, roundtrip, mask_retry, demask_retry,
                   duplicate_payload, large_payload, or mixed.
    TARGET_RPS: desired aggregate HTTP request rate.
    LOCUST_USERS: user count selected by the runner.
    DUPLICATE_POOL_SIZE: number of shared payload ids in the contention test.
    LARGE_PAYLOAD_CHARS: approximate large payload length.
"""

from __future__ import annotations

import os
import random
import threading
import uuid
from collections.abc import Callable

from locust import FastHttpUser, constant_throughput, task

ORIGINAL = "Напишите мне на test@example.com или +7 999 123-45-67"
SCENARIO = os.getenv("LOAD_SCENARIO", "mixed")
TARGET_RPS = max(float(os.getenv("TARGET_RPS", "100")), 0.1)
USER_COUNT = max(int(os.getenv("LOCUST_USERS", "10")), 1)
DUPLICATE_POOL_SIZE = max(int(os.getenv("DUPLICATE_POOL_SIZE", "10")), 1)
LARGE_PAYLOAD_CHARS = max(int(os.getenv("LARGE_PAYLOAD_CHARS", "10000")), 100)
LARGE_PAYLOAD = ("Слово " * ((LARGE_PAYLOAD_CHARS // 6) + 1))[:LARGE_PAYLOAD_CHARS]
LARGE_PAYLOAD += " test@example.com"

REQUESTS_PER_TASK = {
    "mask_only": 1.0,
    "roundtrip": 2.0,
    "mask_retry": 2.0,
    "demask_retry": 3.0,
    "duplicate_payload": 1.0,
    "large_payload": 2.0,
    # Weighted average for the mixed selection below.
    "mixed": (5 * 2 + 2 * 2 + 2 * 3 + 2 * 1 + 1 * 2) / 12,
}
if SCENARIO not in REQUESTS_PER_TASK:
    raise ValueError(f"unsupported LOAD_SCENARIO: {SCENARIO}")

TASKS_PER_USER_SECOND = TARGET_RPS / USER_COUNT / REQUESTS_PER_TASK[SCENARIO]
_DUPLICATE_IDS = [f"load-shared-{i}" for i in range(DUPLICATE_POOL_SIZE)]
_EXPECTED_MASKS: dict[str, str] = {}
_EXPECTED_MASKS_LOCK = threading.Lock()


class ProcessUser(FastHttpUser):
    """Generate a bounded-cardinality, correctness-checked workload."""

    wait_time = constant_throughput(max(TASKS_PER_USER_SECOND, 0.001))

    def _post(
        self,
        payload_id: str,
        payload: str,
        name: str,
        validator: Callable[[str], str | None] | None = None,
    ) -> str | None:
        with self.client.post(
            "/process",
            json={"payload": payload, "payload_id": payload_id},
            name=name,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                retry_after = response.headers.get("Retry-After")
                suffix = f" retry_after={retry_after}" if retry_after else ""
                response.failure(f"status={response.status_code}{suffix}")
                return None
            try:
                body = response.json()
            except ValueError:
                response.failure("response is not JSON")
                return None
            result = body.get("result")
            if not isinstance(result, str):
                response.failure("response does not contain string result")
                return None
            if validator is not None:
                failure = validator(result)
                if failure is not None:
                    response.failure(failure)
                    return None
            return result

    def _mask(
        self,
        payload_id: str,
        payload: str,
        name: str = "POST /process [mask]",
    ) -> str | None:
        return self._post(
            payload_id,
            payload,
            name,
            validator=lambda result: (
                "mask leaked the sentinel email" if "test@example.com" in result else None
            ),
        )

    def _demask(self, payload_id: str, masked: str, expected: str, name: str) -> str | None:
        return self._post(
            payload_id,
            masked,
            name,
            validator=lambda result: (
                "demask returned a different original" if result != expected else None
            ),
        )

    def scenario_mask_only(self) -> None:
        self._mask(str(uuid.uuid4()), ORIGINAL)

    def scenario_roundtrip(self) -> None:
        payload_id = str(uuid.uuid4())
        masked = self._mask(payload_id, ORIGINAL)
        if masked is not None:
            self._demask(payload_id, masked, ORIGINAL, "POST /process [demask]")

    def scenario_mask_retry(self) -> None:
        payload_id = str(uuid.uuid4())
        first = self._mask(payload_id, ORIGINAL, "POST /process [retry-mask:first]")
        if first is None:
            return
        self._post(
            payload_id,
            ORIGINAL,
            "POST /process [retry-mask:second]",
            validator=lambda result: (
                "mask retry was not idempotent" if result != first else None
            ),
        )

    def scenario_demask_retry(self) -> None:
        payload_id = str(uuid.uuid4())
        masked = self._mask(payload_id, ORIGINAL)
        if masked is None:
            return
        self._demask(payload_id, masked, ORIGINAL, "POST /process [retry-demask:first]")
        self._demask(payload_id, masked, ORIGINAL, "POST /process [retry-demask:second]")

    def scenario_duplicate_payload(self) -> None:
        payload_id = random.choice(_DUPLICATE_IDS)
        def validate_duplicate(result: str) -> str | None:
            with _EXPECTED_MASKS_LOCK:
                expected = _EXPECTED_MASKS.setdefault(payload_id, result)
            return "duplicate payload_id returned a different mask" if result != expected else None

        self._post(
            payload_id,
            ORIGINAL,
            "POST /process [duplicate]",
            validator=validate_duplicate,
        )

    def scenario_large_payload(self) -> None:
        payload_id = str(uuid.uuid4())
        masked = self._mask(payload_id, LARGE_PAYLOAD, "POST /process [large-mask]")
        if masked is not None:
            self._demask(
                payload_id,
                masked,
                LARGE_PAYLOAD,
                "POST /process [large-demask]",
            )

    @task
    def execute_scenario(self) -> None:
        if SCENARIO == "mixed":
            selected = random.choices(
                (
                    self.scenario_roundtrip,
                    self.scenario_mask_retry,
                    self.scenario_demask_retry,
                    self.scenario_duplicate_payload,
                    self.scenario_large_payload,
                ),
                weights=(5, 2, 2, 2, 1),
                k=1,
            )[0]
            selected()
            return
        getattr(self, f"scenario_{SCENARIO}")()
