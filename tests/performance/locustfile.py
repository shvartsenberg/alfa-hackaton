"""Configurable Locust scenarios for the PII proxy.

Environment variables:
    LOAD_SCENARIO: mask_only, roundtrip, mask_retry, demask_retry,
                   duplicate_payload, large_payload, or mixed.
    TARGET_RPS: desired aggregate HTTP request rate (static mode).
    LOCUST_USERS: user count selected by the runner (static mode).
    LARGE_PAYLOAD_CHARS: approximate large payload length.
    CUSTOM_METRICS_PATH: where to write the per-run custom metrics JSON
        (mask/demask counts, HTTP status buckets, functional and transport
        errors). Written on test stop by an event listener.
    LOAD_PROFILE: "organizer" enables the single continuous organizer profile
        (a LoadTestShape + dynamic wait_time); any other value keeps the static
        per-target mode. When "organizer", ORGANIZER_MAX_USERS and
        ORGANIZER_RPS_PER_USER tune the user-count cap and per-user rate.

Counter model
-------------
The custom metrics partition every HTTP request into mutually exclusive
status buckets so they reconcile with the Locust ``Aggregated`` row:

    http_2xx + http_429 + http_422 + http_other + transport_errors
        == Locust "Request Count"

``functional_errors`` is a cross-cutting counter (not part of the partition):
it counts 200 responses whose body/result failed validation, 429 responses
without a valid ``Retry-After``, and unexpected non-2xx statuses. A 429 that
carries a valid ``Retry-After`` is a legitimate overload signal and is counted
only in ``http_429``, never as an error.

The ``mixed`` scenario is weighted so that the aggregate number of MASK and
DEMASK operations is equal *in expectation*. Actual counts are not guaranteed
to be strictly equal because roundtrips can be interrupted (a mask succeeds but
the demask is dropped) and 429s can abort a step; the report shows the real
counts rather than asserting a false exact balance. No external LLM is used.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from locust import FastHttpUser, constant_throughput, events, task

# Representative Russian payload covering several PII types. Each sentinel is
# an original value that MUST NOT survive masking.
ORIGINAL = (
    "Клиент Иванов Иван Иванович, паспорт 4509 123456, ИНН 7707083893, "
    "карта 4111 1111 1111 1111, телефон +7 999 123-45-67, "
    "email test@example.com, дата рождения 15.03.1990"
)
# A different payload used to provoke a payload_id conflict (expect 422).
CONFLICT_PAYLOAD = (
    "Клиент Петров Пётр Петрович, паспорт 4509 654321, ИНН 500100732259, "
    "карта 4111 1111 1111 1111, телефон +7 916 555-12-34, "
    "email petr@example.com, дата рождения 20.07.1985"
)
# Original values that must be absent from any masked result.
MASKED_SENTINELS = (
    "test@example.com",
    "+7 999 123-45-67",
    "4111 1111 1111 1111",
    "7707083893",
    "4509 123456",
    "Иванов Иван Иванович",
    "15.03.1990",
)

SCENARIO = os.getenv("LOAD_SCENARIO", "mixed")
TARGET_RPS = max(float(os.getenv("TARGET_RPS", "100")), 0.1)
USER_COUNT = max(int(os.getenv("LOCUST_USERS", "10")), 1)
LARGE_PAYLOAD_CHARS = max(int(os.getenv("LARGE_PAYLOAD_CHARS", "10000")), 100)
CUSTOM_METRICS_PATH = os.getenv("CUSTOM_METRICS_PATH", "")
LARGE_PAYLOAD = ("Слово " * ((LARGE_PAYLOAD_CHARS // 6) + 1))[:LARGE_PAYLOAD_CHARS]
LARGE_PAYLOAD += " test@example.com"

# Weights for the mixed scenario. Chosen so that total MASK == total DEMASK in
# expectation:
#   MASK   = w_roundtrip + 2*w_mask_retry + w_demask_retry + 2*w_duplicate + w_large
#   DEMASK = w_roundtrip + 2*w_demask_retry + w_duplicate + w_large
# With w_mask_retry=1 and w_duplicate=1, equality requires w_demask_retry=3.
# The duplicate conflict step performs 2 MASKs (first + remask) and 1 DEMASK,
# plus one expected 422 that is not a mask; it therefore contributes 2 MASK and
# 1 DEMASK to the balance. Actual counts may still differ due to interrupted
# roundtrips/429s.
_MIXED_WEIGHTS = {
    "roundtrip": 5,
    "mask_retry": 1,
    "demask_retry": 3,
    "duplicate_payload": 1,
    "large_payload": 1,
}

REQUESTS_PER_TASK = {
    "mask_only": 1.0,
    "roundtrip": 2.0,
    "mask_retry": 2.0,
    "demask_retry": 3.0,
    "duplicate_payload": 4.0,
    "large_payload": 2.0,
    # Weighted average for the mixed selection below.
    "mixed": (
        _MIXED_WEIGHTS["roundtrip"] * 2
        + _MIXED_WEIGHTS["mask_retry"] * 2
        + _MIXED_WEIGHTS["demask_retry"] * 3
        + _MIXED_WEIGHTS["duplicate_payload"] * 4
        + _MIXED_WEIGHTS["large_payload"] * 2
    )
    / sum(_MIXED_WEIGHTS.values()),
}
if SCENARIO not in REQUESTS_PER_TASK:
    raise ValueError(f"unsupported LOAD_SCENARIO: {SCENARIO}")

TASKS_PER_USER_SECOND = TARGET_RPS / USER_COUNT / REQUESTS_PER_TASK[SCENARIO]

# --- Organizer continuous profile (opt-in via LOAD_PROFILE=organizer) -------
#
# A single continuous Locust run (no process restart between phases) that
# reproduces the organizers' requested load shape:
#   0-180s   linear ramp target 0 -> 330 RPS
#   180-300s steady 330 RPS
#   300-330s peak 1000 RPS (flat)
#   330-360s linear decay 1000 -> 330 RPS
#   360-480s steady 330 RPS
# Integral over 480s / 480 = 330.9375 RPS average target.
LOAD_PROFILE = os.getenv("LOAD_PROFILE", "")
ORGANIZER_DURATION = max(int(os.getenv("ORGANIZER_DURATION", "480")), 1)
ORGANIZER_TARGET_AVERAGE = 330.9375
# Hard ceiling on concurrent users for the organizer profile. The CLI rejects
# --max-users > 200 in organizer mode; this is defense-in-depth so the shape can
# never exceed 200 even if ORGANIZER_MAX_USERS is set higher via env.
ORGANIZER_HARD_MAX_USERS = 200
ORGANIZER_MAX_USERS = min(
    max(int(os.getenv("ORGANIZER_MAX_USERS", "200")), 1),
    ORGANIZER_HARD_MAX_USERS,
)
ORGANIZER_RPS_PER_USER = max(float(os.getenv("ORGANIZER_RPS_PER_USER", "10")), 0.1)
ORGANIZER_PHASES = (
    {"start": 0, "end": 180, "target_rps": 330, "kind": "ramp_up"},
    {"start": 180, "end": 300, "target_rps": 330, "kind": "steady"},
    {"start": 300, "end": 330, "target_rps": 1000, "kind": "peak"},
    {"start": 330, "end": 360, "target_rps": 330, "kind": "ramp_down"},
    {"start": 360, "end": 480, "target_rps": 330, "kind": "steady"},
)

_ORGANIZER_SHAPE: OrganizerShape | None = None


def target_rps_at(t: float) -> float:
    """Return the organizer target RPS at elapsed time ``t`` (seconds)."""
    if t < 0:
        return 0.0
    if t < 180:
        return 330.0 * t / 180.0
    if t < 300:
        return 330.0
    if t < 330:
        return 1000.0
    if t < 360:
        return 1000.0 - (1000.0 - 330.0) * (t - 330.0) / 30.0
    if t < 480:
        return 330.0
    return 0.0


def _current_per_user_rate(t: float | None = None) -> float:
    """Per-user task rate (tasks/second) for the current organizer target.

    ``t`` is the elapsed time; when omitted it is read from the active shape.
    """
    if _ORGANIZER_SHAPE is None:
        return TASKS_PER_USER_SECOND
    if t is None:
        t = _ORGANIZER_SHAPE.get_run_time()
    target = target_rps_at(t)
    users = min(
        ORGANIZER_MAX_USERS,
        max(1, math.ceil(target / ORGANIZER_RPS_PER_USER)),
    )
    rate = target / users / REQUESTS_PER_TASK[SCENARIO]
    return max(rate, 0.001)


def _dynamic_wait_time(self) -> float:  # type: ignore[no-untyped-def]
    """wait_time that re-derives the per-user rate from the current target."""
    return constant_throughput(_current_per_user_rate())(self)


if LOAD_PROFILE == "organizer":
    from locust import LoadTestShape

    class OrganizerShape(LoadTestShape):
        """Drive user count from the organizer target-RPS profile."""

        def __init__(self) -> None:
            super().__init__()
            global _ORGANIZER_SHAPE
            _ORGANIZER_SHAPE = self

        def tick(self):  # type: ignore[no-untyped-def]
            t = self.get_run_time()
            if t >= ORGANIZER_DURATION:
                return None
            target = target_rps_at(t)
            users = min(
                ORGANIZER_MAX_USERS,
                max(1, math.ceil(target / ORGANIZER_RPS_PER_USER)),
            )
            return (users, max(1.0, float(users)))

# Per-run custom metrics aggregated across all users. These are not part of the
# standard Locust CSV and are written to CUSTOM_METRICS_PATH on test stop.
_METRICS_LOCK = threading.Lock()
_CUSTOM_METRICS: dict[str, int] = {
    "mask_requests": 0,
    "demask_requests": 0,
    "http_2xx": 0,
    "http_429": 0,
    "http_422": 0,
    "http_other": 0,
    "functional_errors": 0,
    "transport_errors": 0,
}


def _bump(key: str, amount: int = 1) -> None:
    with _METRICS_LOCK:
        _CUSTOM_METRICS[key] += amount


@events.test_stop.add_listener
def _write_custom_metrics(environment, **_kwargs) -> None:  # type: ignore[no-untyped-def]
    """Persist the aggregated custom metrics when the run finishes.

    Metadata is mode-aware: in organizer mode the static ``target_rps``/``users``
    defaults are NOT reported as true values; instead the profile name, the
    actual user cap, and the profile duration are written. ``target_average_rps``
    is only meaningful for the full 480s profile, so it is omitted for a shorter
    (smoke) run.
    """
    if not CUSTOM_METRICS_PATH:
        return
    with _METRICS_LOCK:
        snapshot = dict(_CUSTOM_METRICS)
    snapshot["scenario"] = SCENARIO
    snapshot["total_http_requests"] = (
        snapshot["http_2xx"]
        + snapshot["http_429"]
        + snapshot["http_422"]
        + snapshot["http_other"]
        + snapshot["transport_errors"]
    )
    if LOAD_PROFILE == "organizer":
        snapshot["profile"] = "organizer"
        snapshot["profile_duration_seconds"] = ORGANIZER_DURATION
        snapshot["profile_max_users"] = ORGANIZER_MAX_USERS
        if ORGANIZER_DURATION == 480:
            snapshot["target_average_rps"] = ORGANIZER_TARGET_AVERAGE
    else:
        snapshot["target_rps"] = TARGET_RPS
        snapshot["users"] = USER_COUNT
    path = Path(CUSTOM_METRICS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_json_object(response) -> dict | None:  # type: ignore[no-untyped-def]
    """Return the response body as a dict, or ``None`` if it is not one.

    Handles invalid JSON and non-object JSON (e.g. a JSON list) without raising,
    so a malformed body is a functional error rather than a transport error.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    return body


def _mask_validator(result: str) -> str | None:
    """Fail if any expected-masked original value survived masking.

    Returns a static, PII-free message: the leaked values themselves must never
    be written to Locust failure stats or the report.
    """
    if any(sentinel in result for sentinel in MASKED_SENTINELS):
        return "mask leaked one or more original PII values"
    return None


class ProcessUser(FastHttpUser):
    """Generate a bounded-cardinality, correctness-checked workload."""

    if LOAD_PROFILE == "organizer":
        wait_time = _dynamic_wait_time
    else:
        wait_time = constant_throughput(max(TASKS_PER_USER_SECOND, 0.001))

    def _post(
        self,
        payload_id: str,
        payload: str,
        name: str,
        kind: str,
        validator: Callable[[str], str | None] | None = None,
        expected_statuses: set[int] | None = None,
        expected_error: str | None = None,
    ) -> str | None:
        expected_statuses = expected_statuses or set()
        try:
            with self.client.post(
                "/process",
                json={"payload": payload, "payload_id": payload_id},
                name=name,
                catch_response=True,
            ) as response:
                status = response.status_code
                if status == 429:
                    # 429 is a legitimate overload signal, not a functional
                    # error. It must carry Retry-After; count it separately.
                    _bump("http_429")
                    retry_after = response.headers.get("Retry-After")
                    if retry_after is None:
                        _bump("functional_errors")
                        response.failure("429 without Retry-After")
                    else:
                        response.success()
                    return None
                if status in expected_statuses:
                    # Expected status (e.g. 422 payload conflict). The status
                    # bucket is bumped immediately so the status classification
                    # is complete regardless of the body. When an expected error
                    # code is given, the body must carry it; a wrong body (bad
                    # JSON, non-dict, or a different error) is a functional
                    # error, never a transport error. The failure message is
                    # static and never echoes untrusted response content.
                    _bump(f"http_{status}")
                    if expected_error is not None:
                        body = _parse_json_object(response)
                        if body is None:
                            _bump("functional_errors")
                            response.failure("expected status but body is not a JSON object")
                            return None
                        if body.get("error") != expected_error:
                            _bump("functional_errors")
                            response.failure("expected status but error code did not match")
                            return None
                    response.success()
                    return None
                if status != 200:
                    _bump("http_other")
                    _bump("functional_errors")
                    retry_after = response.headers.get("Retry-After")
                    suffix = f" retry_after={retry_after}" if retry_after else ""
                    response.failure(f"status={status}{suffix}")
                    return None
                _bump("http_2xx")
                _bump(f"{kind}_requests")
                body = _parse_json_object(response)
                if body is None:
                    _bump("functional_errors")
                    response.failure("response is not a JSON object")
                    return None
                result = body.get("result")
                if not isinstance(result, str):
                    _bump("functional_errors")
                    response.failure("response does not contain string result")
                    return None
                if validator is not None:
                    failure = validator(result)
                    if failure is not None:
                        _bump("functional_errors")
                        response.failure(failure)
                        return None
                return result
        except Exception:
            # Transport-level failure (connection refused, timeout, reset).
            _bump("transport_errors")
            return None

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
            "mask",
            validator=_mask_validator,
        )

    def _demask(self, payload_id: str, masked: str, expected: str, name: str) -> str | None:
        return self._post(
            payload_id,
            masked,
            name,
            "demask",
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
            "mask",
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
        # Deterministic conflict: mask one payload under a fresh id, then send a
        # *different* payload under the same id and expect a 422 with error
        # INVALID_PAYLOAD. The 422 is the expected outcome of the conflict test,
        # not a functional error. Afterwards re-mask the original and demask it
        # to prove the conflict did not corrupt the stored mapping.
        payload_id = str(uuid.uuid4())
        first = self._mask(payload_id, ORIGINAL, "POST /process [dup:first]")
        if first is None:
            return
        self._post(
            payload_id,
            CONFLICT_PAYLOAD,
            "POST /process [dup:conflict]",
            "mask",
            expected_statuses={422},
            expected_error="INVALID_PAYLOAD",
        )
        second = self._post(
            payload_id,
            ORIGINAL,
            "POST /process [dup:remask]",
            "mask",
            validator=lambda result: (
                "duplicate conflict corrupted the mask mapping"
                if result != first
                else None
            ),
        )
        if second is not None:
            self._demask(payload_id, second, ORIGINAL, "POST /process [dup:demask]")

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
                weights=(
                    _MIXED_WEIGHTS["roundtrip"],
                    _MIXED_WEIGHTS["mask_retry"],
                    _MIXED_WEIGHTS["demask_retry"],
                    _MIXED_WEIGHTS["duplicate_payload"],
                    _MIXED_WEIGHTS["large_payload"],
                ),
                k=1,
            )[0]
            selected()
            return
        getattr(self, f"scenario_{SCENARIO}")()