"""Targeted tests for the performance harness critical cases.

Covers counter reconciliation, missing metrics, 429 without Retry-After,
payload_id conflicts, and roundtrip/retry counter behaviour. These are unit
tests: they exercise the harness logic without a live service.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# Locust's __init__ monkey-patches gevent/ssl globally on import. In the real
# CLI Locust runs in its own process, but here the harness is imported inside a
# pytest process that has already imported ssl/anyio (via integration tests).
# Skip the global monkey-patch for this test process only; it is not passed to
# the real Locust CLI.
os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

from scripts.run_performance import (
    ORGANIZER_DURATION,
    ORGANIZER_TARGET_AVERAGE,
    build_result,
    check_health,
    read_authoritative_aggregate,
    read_custom_metrics,
)
from tests.performance import locustfile


class _FakeResponse:
    """Minimal stand-in for a Locust response object."""

    def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self._success = False
        self._failure: str | None = None

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def success(self) -> None:
        self._success = True

    def failure(self, message: str) -> None:
        self._failure = message


class _FakeClient:
    """Returns a scripted sequence of responses for one POST."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self._index = 0

    def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = self._responses[self._index]
        self._index += 1
        return _ResponseContext(response)


class _ResponseContext:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def __enter__(self) -> _FakeResponse:
        return self._response

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        return None


class _FakeUser(locustfile.ProcessUser):
    def __init__(self, responses: list[_FakeResponse]):
        self.client = _FakeClient(responses)


def _reset_metrics() -> None:
    for key in locustfile._CUSTOM_METRICS:
        locustfile._CUSTOM_METRICS[key] = 0


# --- Counter reconciliation (defect 1) -------------------------------------


def test_build_result_flags_counter_mismatch() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    # Custom http total (850) disagrees with Locust request count (1000).
    custom = {
        "http_2xx": 800,
        "http_429": 50,
        "http_422": 0,
        "http_other": 0,
        "transport_errors": 0,
        "mask_requests": 500,
        "demask_requests": 300,
        "functional_errors": 0,
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics=custom,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.passed is False
    assert any("counter mismatch" in v for v in result.violations)


def test_build_result_accepts_reconciled_counters() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    custom = {
        "http_2xx": 900,
        "http_429": 100,
        "http_422": 0,
        "http_other": 0,
        "transport_errors": 0,
        "mask_requests": 600,
        "demask_requests": 300,
        "functional_errors": 0,
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics=custom,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.passed is True


def test_build_result_flags_all_429_as_not_passed() -> None:
    # A run where every request is a valid 429 (with Retry-After) and no
    # successful MASK happened must NOT be a green verdict: it does not prove
    # the service works.
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    custom = {
        "http_2xx": 0,
        "http_429": 1000,
        "http_422": 0,
        "http_other": 0,
        "transport_errors": 0,
        "mask_requests": 0,
        "demask_requests": 0,
        "functional_errors": 0,
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics=custom,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.passed is False
    assert any("no successful 2xx" in v for v in result.violations)
    assert any("no successful mask" in v for v in result.violations)


def test_build_result_flags_missing_required_keys() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    # Missing mask_requests/demask_requests/functional_errors -> FAIL.
    custom = {
        "http_2xx": 1000,
        "http_429": 0,
        "http_422": 0,
        "http_other": 0,
        "transport_errors": 0,
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics=custom,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.passed is False
    assert any("missing required keys" in v for v in result.violations)


def test_build_result_flags_mask_demask_mismatch() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    # mask_requests + demask_requests (700) != http_2xx (1000) -> FAIL.
    custom = {
        "http_2xx": 1000,
        "http_429": 0,
        "http_422": 0,
        "http_other": 0,
        "transport_errors": 0,
        "mask_requests": 400,
        "demask_requests": 300,
        "functional_errors": 0,
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics=custom,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.passed is False
    assert any("mask+demask" in v for v in result.violations)


def test_build_result_flags_failure_count_mismatch() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    # Locust Failure Count is 0 but custom transport_errors is 50 -> FAIL.
    custom = {
        "http_2xx": 950,
        "http_429": 0,
        "http_422": 0,
        "http_other": 0,
        "transport_errors": 50,
        "mask_requests": 600,
        "demask_requests": 350,
        "functional_errors": 0,
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics=custom,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.passed is False
    assert any("failure count" in v for v in result.violations)


# --- Missing metrics (defect 6) --------------------------------------------


def test_build_result_flags_missing_custom_metrics() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics=None,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.passed is False
    assert result.custom_metrics_present is False
    assert any("custom metrics missing" in v for v in result.violations)


def test_read_custom_metrics_distinguishes_missing_from_zero(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert read_custom_metrics(missing) is None

    zero = tmp_path / "zero.json"
    zero.write_text(json.dumps({"http_2xx": 0}), encoding="utf-8")
    assert read_custom_metrics(zero) == {"http_2xx": 0}

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_custom_metrics(bad) is None


# --- authoritative final_stats.csv (stale periodic CSV defect) -------------


def test_read_authoritative_aggregate_prefers_final_stats(tmp_path: Path) -> None:
    # The periodic stats_stats.csv may be a stale snapshot (e.g. 137093) that
    # disagrees with the final console table and custom metrics (137231). The
    # runner must use final_stats.csv, written from environment.stats on
    # test_stop, as the authoritative aggregate.
    stale = tmp_path / "stats_stats.csv"
    stale.write_text(
        "Type,Name,Request Count,Failure Count,Requests/s,50%,95%,99%\n"
        ",Aggregated,137093,0,285.7,15,200,220\n",
        encoding="utf-8",
    )
    final = tmp_path / "final_stats.csv"
    final.write_text(
        "Type,Name,Request Count,Failure Count,Requests/s,50%,95%,99%\n"
        ",Aggregated,137231,0,285.8,15,200,220\n",
        encoding="utf-8",
    )
    aggregate = read_authoritative_aggregate(tmp_path)
    assert aggregate["Request Count"] == "137231"


def test_read_authoritative_aggregate_missing_final_raises(tmp_path: Path) -> None:
    # If final_stats.csv is absent, the runner must NOT fall back to a possibly
    # stale periodic CSV for a PASS; it must raise so the step fails.
    stale = tmp_path / "stats_stats.csv"
    stale.write_text(
        "Type,Name,Request Count,Failure Count,Requests/s,50%,95%,99%\n"
        ",Aggregated,137093,0,285.7,15,200,220\n",
        encoding="utf-8",
    )
    try:
        read_authoritative_aggregate(tmp_path)
    except RuntimeError as exc:
        assert "final_stats.csv missing" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when final_stats.csv is missing")


def test_write_final_stats_csv_exports_environment_stats(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # The test_stop listener must export the live environment.stats to
    # final_stats.csv via the public StatsCSV.requests_csv API, so the runner
    # reconciles against the same final numbers the console table shows. This
    # exercises our own listener (not just the Locust library) by pointing
    # CUSTOM_METRICS_PATH at a temp dir and invoking it with a fake environment.
    import csv as _csv

    class _FakeStatsEntry:
        method = "POST"
        name = "Aggregated"
        num_requests = 137231
        num_failures = 0
        median_response_time = 15
        avg_response_time = 44.0
        min_response_time = 1
        max_response_time = 500
        avg_content_length = 1576
        total_rps = 285.8
        total_fail_per_sec = 0.0

        def get_response_time_percentile(self, p: float) -> float:  # type: ignore[no-untyped-def]
            return {0.5: 15.0, 0.95: 200.0, 0.99: 220.0}.get(p, 0.0)

    class _FakeStats:
        entries = {}
        total = _FakeStatsEntry()

    class _FakeEnvironment:
        stats = _FakeStats()

    custom_path = tmp_path / "custom_metrics.json"
    monkeypatch.setattr(locustfile, "CUSTOM_METRICS_PATH", str(custom_path))
    locustfile._write_final_stats_csv(_FakeEnvironment())

    final_path = tmp_path / "final_stats.csv"
    assert final_path.exists()
    rows = list(_csv.DictReader(final_path.open(newline="", encoding="utf-8")))
    assert rows[0]["Name"] == "Aggregated"
    assert rows[0]["Request Count"] == "137231"
    assert rows[0]["Failure Count"] == "0"
    # No temporary file may be left behind after a successful atomic write.
    assert not (tmp_path / "final_stats.csv.tmp").exists()


def test_reset_step_outputs_removes_stale_files(tmp_path: Path) -> None:
    # A rerun into the same output_dir must not mistake the previous run's
    # final_stats.csv / custom_metrics.json for new data.
    from scripts import run_performance

    stale_final = tmp_path / "final_stats.csv"
    stale_final.write_text("stale", encoding="utf-8")
    stale_custom = tmp_path / "custom_metrics.json"
    stale_custom.write_text("{}", encoding="utf-8")
    keep = tmp_path / "stats_stats.csv"
    keep.write_text("periodic", encoding="utf-8")

    run_performance.reset_step_outputs(tmp_path)

    assert not stale_final.exists()
    assert not stale_custom.exists()
    # The periodic CSV is a sidecar and is intentionally left in place.
    assert keep.exists()


def test_reset_step_outputs_aborts_on_unlink_failure(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # If a stale final_stats.csv cannot be removed, the run must abort rather
    # than risk accepting a stale artifact as a false PASS. The OSError must
    # propagate, not be suppressed.
    from scripts import run_performance

    stale_final = tmp_path / "final_stats.csv"
    stale_final.write_text("stale", encoding="utf-8")

    def _boom_unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    try:
        run_performance.reset_step_outputs(tmp_path)
    except OSError as exc:
        assert "permission denied" in str(exc)
    else:
        raise AssertionError("expected OSError to propagate from reset_step_outputs")


def test_unmeasured_cpu_and_memory_are_unknown_not_fail() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics={
            "http_2xx": 1000,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 600,
            "demask_requests": 400,
            "functional_errors": 0,
        },
        cpu_measured=False,
        memory_measured=False,
    )
    # Absence of psutil/samples is "unknown", not a FAIL.
    assert result.passed is True
    assert result.cpu_percent is None
    assert result.memory_mb is None
    assert result.cpu_measured is False
    assert result.memory_measured is False


def test_build_result_flags_short_duration() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "1000",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    result = build_result(
        row,
        target_rps=1000,
        users=100,
        duration_seconds=15,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        custom_metrics={
            "http_2xx": 1000,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 600,
            "demask_requests": 400,
            "functional_errors": 0,
        },
        cpu_measured=True,
        memory_measured=True,
        min_duration_for_stable=30,
    )
    assert result.passed is False
    assert any("not stable" in v for v in result.violations)


# --- 429 without Retry-After (defect 1) ------------------------------------


def test_429_without_retry_after_is_functional_error() -> None:
    _reset_metrics()
    response = _FakeResponse(429, headers={})
    user = _FakeUser([response])
    locustfile.ProcessUser._post(user, "id", "payload", "POST /process [mask]", "mask")
    assert locustfile._CUSTOM_METRICS["http_429"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 1
    assert response._failure is not None


def test_429_with_retry_after_is_not_error() -> None:
    _reset_metrics()
    response = _FakeResponse(429, headers={"Retry-After": "1"})
    user = _FakeUser([response])
    locustfile.ProcessUser._post(user, "id", "payload", "POST /process [mask]", "mask")
    assert locustfile._CUSTOM_METRICS["http_429"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 0
    assert response._success is True


# --- payload_id conflict (defect 2) ----------------------------------------


def test_duplicate_conflict_422_is_expected_not_error() -> None:
    _reset_metrics()
    response = _FakeResponse(422, body={"error": "INVALID_PAYLOAD"})
    user = _FakeUser([response])
    locustfile.ProcessUser._post(
        user,
        "id",
        "different payload",
        "POST /process [dup:conflict]",
        "mask",
        expected_statuses={422},
        expected_error="INVALID_PAYLOAD",
    )
    assert locustfile._CUSTOM_METRICS["http_422"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 0
    assert response._success is True


def test_duplicate_conflict_422_with_wrong_error_is_functional_error() -> None:
    _reset_metrics()
    # A 422 with a different error (e.g. a Pydantic validation error) must not
    # be treated as the expected payload conflict. The status bucket is still
    # counted (complete status classification), but the wrong body is a
    # functional error with a safe, PII-free failure message.
    response = _FakeResponse(422, body={"error": "VALIDATION_ERROR"})
    user = _FakeUser([response])
    locustfile.ProcessUser._post(
        user,
        "id",
        "different payload",
        "POST /process [dup:conflict]",
        "mask",
        expected_statuses={422},
        expected_error="INVALID_PAYLOAD",
    )
    assert locustfile._CUSTOM_METRICS["http_422"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 1
    assert locustfile._CUSTOM_METRICS["transport_errors"] == 0
    assert response._failure is not None
    assert not any(sentinel in response._failure for sentinel in locustfile.MASKED_SENTINELS)


def test_duplicate_conflict_422_with_non_dict_body_is_functional_error() -> None:
    _reset_metrics()
    # A 422 whose body is a JSON list (not a dict) must be a functional error,
    # not a transport error, and must not raise AttributeError.
    response = _FakeResponse(422, body=["not", "a", "dict"])
    user = _FakeUser([response])
    locustfile.ProcessUser._post(
        user,
        "id",
        "different payload",
        "POST /process [dup:conflict]",
        "mask",
        expected_statuses={422},
        expected_error="INVALID_PAYLOAD",
    )
    assert locustfile._CUSTOM_METRICS["http_422"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 1
    assert locustfile._CUSTOM_METRICS["transport_errors"] == 0
    assert response._failure is not None
    assert not any(sentinel in response._failure for sentinel in locustfile.MASKED_SENTINELS)


def test_200_with_non_dict_body_is_functional_error() -> None:
    _reset_metrics()
    # A 200 whose body is a JSON list must be a functional error, not a
    # transport error, and must not raise AttributeError.
    response = _FakeResponse(200, body=["not", "a", "dict"])
    user = _FakeUser([response])
    locustfile.ProcessUser._post(user, "id", "payload", "POST /process [mask]", "mask")
    assert locustfile._CUSTOM_METRICS["http_2xx"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 1
    assert locustfile._CUSTOM_METRICS["transport_errors"] == 0
    assert response._failure is not None


def test_duplicate_scenario_preserves_mapping() -> None:
    _reset_metrics()
    masked = "t***@example.com"
    user = _FakeUser(
        [
            _FakeResponse(200, body={"result": masked}),  # dup:first (mask)
            _FakeResponse(422, body={"error": "INVALID_PAYLOAD"}),  # dup:conflict
            _FakeResponse(200, body={"result": masked}),  # dup:remask (mask)
            _FakeResponse(200, body={"result": locustfile.ORIGINAL}),  # dup:demask
        ]
    )
    locustfile.ProcessUser.scenario_duplicate_payload(user)
    assert locustfile._CUSTOM_METRICS["mask_requests"] == 2
    assert locustfile._CUSTOM_METRICS["demask_requests"] == 1
    assert locustfile._CUSTOM_METRICS["http_422"] == 1
    assert locustfile._CUSTOM_METRICS["http_2xx"] == 3
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 0


def test_unexpected_422_is_functional_error() -> None:
    _reset_metrics()
    response = _FakeResponse(422, body={"error": "INVALID_PAYLOAD"})
    user = _FakeUser([response])
    locustfile.ProcessUser._post(user, "id", "payload", "POST /process [mask]", "mask")
    assert locustfile._CUSTOM_METRICS["http_other"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 1
    assert response._failure is not None


# --- roundtrip / retry (defect 7) ------------------------------------------


def test_roundtrip_counts_mask_and_demask() -> None:
    _reset_metrics()
    masked = "t***@example.com"
    user = _FakeUser(
        [
            _FakeResponse(200, body={"result": masked}),
            _FakeResponse(200, body={"result": locustfile.ORIGINAL}),
        ]
    )
    locustfile.ProcessUser.scenario_roundtrip(user)
    assert locustfile._CUSTOM_METRICS["mask_requests"] == 1
    assert locustfile._CUSTOM_METRICS["demask_requests"] == 1
    assert locustfile._CUSTOM_METRICS["http_2xx"] == 2
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 0


def test_mask_retry_is_idempotent() -> None:
    _reset_metrics()
    masked = "t***@example.com"
    user = _FakeUser(
        [
            _FakeResponse(200, body={"result": masked}),
            _FakeResponse(200, body={"result": masked}),
        ]
    )
    locustfile.ProcessUser.scenario_mask_retry(user)
    assert locustfile._CUSTOM_METRICS["mask_requests"] == 2
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 0


def test_mask_validator_detects_leaked_pii() -> None:
    _reset_metrics()
    # Masked result still contains the original email -> must be flagged.
    response = _FakeResponse(200, body={"result": "test@example.com"})
    user = _FakeUser([response])
    user._mask("id", locustfile.ORIGINAL)
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 1
    # The failure message must never leak the original PII values.
    assert response._failure is not None
    assert not any(sentinel in response._failure for sentinel in locustfile.MASKED_SENTINELS)


def test_transport_error_is_not_functional() -> None:
    _reset_metrics()

    class _BoomClient:
        def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise ConnectionError("refused")

    user = _FakeUser([])
    user.client = _BoomClient()
    locustfile.ProcessUser._post(user, "id", "payload", "POST /process [mask]", "mask")
    assert locustfile._CUSTOM_METRICS["transport_errors"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 0


# --- readiness check (defect 2) --------------------------------------------


class _FakeUrlResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeUrlResponse:
        return self

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        return None


def test_check_health_uses_readiness_and_accepts_ready(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import urllib.request

    captured: dict[str, str] = {}

    def fake_urlopen(url, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = url
        return _FakeUrlResponse(200, b'{"status": "ready"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    check_health("http://localhost:8000")
    # Must hit the readiness endpoint, not the always-200 liveness endpoint.
    assert captured["url"] == "http://localhost:8000/health/ready"


def test_check_health_rejects_not_ready(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout: _FakeUrlResponse(200, b'{"status": "not_ready"}'),
    )
    try:
        check_health("http://localhost:8000")
    except RuntimeError as exc:
        assert "status=ready" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for not_ready")


def test_check_health_rejects_non_json_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout: _FakeUrlResponse(200, b"not json"),
    )
    try:
        check_health("http://localhost:8000")
    except RuntimeError as exc:
        assert "non-JSON" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for non-JSON body")


# --- organizer profile (target_rps_at, caps, CLI) ---------------------------


def test_target_rps_at_boundaries() -> None:
    assert locustfile.target_rps_at(0) == 0.0
    assert locustfile.target_rps_at(90) == pytest.approx(165.0)
    assert locustfile.target_rps_at(180) == 330.0
    assert locustfile.target_rps_at(240) == 330.0
    assert locustfile.target_rps_at(300) == 1000.0
    assert locustfile.target_rps_at(315) == 1000.0
    assert locustfile.target_rps_at(330) == 1000.0
    assert locustfile.target_rps_at(345) == pytest.approx(665.0)
    assert locustfile.target_rps_at(360) == 330.0
    assert locustfile.target_rps_at(420) == 330.0
    assert locustfile.target_rps_at(480) == 0.0
    assert locustfile.target_rps_at(600) == 0.0


def test_target_rps_at_average_matches_organizer_claim() -> None:
    # Exact analytic integral of the piecewise-linear profile over [0, 480]:
    #   ramp 0->330 (triangle) + steady 330 + peak 1000 + decay 1000->330
    #   (trapezoid) + steady 330.
    area = (
        0.5 * 180 * 330  # 0-180 ramp
        + 120 * 330  # 180-300 steady
        + 30 * 1000  # 300-330 peak
        + 0.5 * 30 * (1000 + 330)  # 330-360 decay
        + 120 * 330  # 360-480 steady
    )
    average = area / ORGANIZER_DURATION
    assert average == pytest.approx(ORGANIZER_TARGET_AVERAGE, abs=1e-9)
    # Midpoint-rule integration is exact for the piecewise-linear profile (the
    # midpoint of each subinterval lies on the linear segment), so it must match
    # the analytic average to high precision.
    steps = 4800
    dt = ORGANIZER_DURATION / steps
    numeric = sum(
        locustfile.target_rps_at((i + 0.5) * dt) for i in range(steps)
    ) * dt
    assert numeric / ORGANIZER_DURATION == pytest.approx(
        ORGANIZER_TARGET_AVERAGE, abs=1e-6
    )


def test_organizer_shape_tick_caps_users_at_200() -> None:
    # Independent test of the actual OrganizerShape.tick() in organizer mode.
    # With ORGANIZER_RPS_PER_USER=1 the naive user count at peak (1000 RPS) is
    # 1000, and ORGANIZER_MAX_USERS=500 requests more than the hard cap; the
    # shape must clamp to exactly 200 at peak and never exceed it.
    import importlib

    old_profile = os.environ.get("LOAD_PROFILE")
    old_max = os.environ.get("ORGANIZER_MAX_USERS")
    old_rpu = os.environ.get("ORGANIZER_RPS_PER_USER")
    try:
        os.environ["LOAD_PROFILE"] = "organizer"
        os.environ["ORGANIZER_MAX_USERS"] = "500"
        os.environ["ORGANIZER_RPS_PER_USER"] = "1"
        importlib.reload(locustfile)
        assert hasattr(locustfile, "OrganizerShape")
        shape = locustfile.OrganizerShape()
        # At peak (t=300) target is 1000 RPS; with rps_per_user=1 the naive
        # count is 1000, but the hard cap must clamp it to exactly 200.
        shape.get_run_time = lambda: 300.0  # type: ignore[method-assign]
        users, _spawn = shape.tick()
        assert users == locustfile.ORGANIZER_HARD_MAX_USERS
        # Min phase: at t=0 target is 0 -> at least 1 user.
        shape.get_run_time = lambda: 0.0  # type: ignore[method-assign]
        users_min, _ = shape.tick()
        assert users_min == 1
        # Max phase across the whole profile never exceeds the hard cap.
        for t in (0, 90, 180, 300, 330, 345, 420, 479):
            shape.get_run_time = lambda t=t: t  # type: ignore[method-assign]
            tick = shape.tick()
            assert tick is not None
            users_t, _spawn_t = tick
            assert users_t <= locustfile.ORGANIZER_HARD_MAX_USERS
            assert users_t >= 1
        # After the profile ends the shape must stop the run.
        shape.get_run_time = lambda: locustfile.ORGANIZER_DURATION + 1  # type: ignore[method-assign]
        assert shape.tick() is None
    finally:
        if old_profile is None:
            os.environ.pop("LOAD_PROFILE", None)
        else:
            os.environ["LOAD_PROFILE"] = old_profile
        if old_max is None:
            os.environ.pop("ORGANIZER_MAX_USERS", None)
        else:
            os.environ["ORGANIZER_MAX_USERS"] = old_max
        if old_rpu is None:
            os.environ.pop("ORGANIZER_RPS_PER_USER", None)
        else:
            os.environ["ORGANIZER_RPS_PER_USER"] = old_rpu
        importlib.reload(locustfile)


def test_organizer_dynamic_rate_changes_with_time() -> None:
    # _current_per_user_rate must actually change as the target changes, not
    # just return the static fallback.
    import importlib

    old_profile = os.environ.get("LOAD_PROFILE")
    try:
        os.environ["LOAD_PROFILE"] = "organizer"
        importlib.reload(locustfile)
        locustfile.OrganizerShape()
        # At t=0 target is 0 -> rate clamped to 0.001.
        rate_at_0 = locustfile._current_per_user_rate(t=0)
        # At t=300 target is 1000 -> rate must be much higher.
        rate_at_peak = locustfile._current_per_user_rate(t=300)
        assert rate_at_peak > rate_at_0
        assert rate_at_0 == pytest.approx(0.001)
    finally:
        if old_profile is None:
            os.environ.pop("LOAD_PROFILE", None)
        else:
            os.environ["LOAD_PROFILE"] = old_profile
        importlib.reload(locustfile)


def test_organizer_profile_cli_rejects_targets() -> None:
    # --profile organizer must be incompatible with --targets.
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_performance.py",
            "--profile",
            "organizer",
            "--targets",
            "100,330",
            "--output-dir",
            "artifacts/pytest-qa-final/organizer-cli",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "--targets cannot be used with --profile organizer" in proc.stderr


def test_organizer_profile_cli_rejects_max_users_over_200() -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_performance.py",
            "--profile",
            "organizer",
            "--max-users",
            "500",
            "--output-dir",
            "artifacts/pytest-qa-final/organizer-cli-max",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "--max-users must be <= 200" in proc.stderr


def test_organizer_dynamic_rate_propagates() -> None:
    # The dynamic wait_time must derive a per-user rate from the current target
    # and user count, and never be zero/negative.
    rate = locustfile._current_per_user_rate()
    assert rate > 0.0
    # With no shape active (static mode) it falls back to the static rate.
    assert locustfile._ORGANIZER_SHAPE is None
    assert rate == locustfile.TASKS_PER_USER_SECOND


# --- organizer custom-metrics metadata (defect 3) ---------------------------


def test_organizer_custom_metrics_metadata_is_mode_aware(tmp_path: Path) -> None:
    # In organizer mode the static target_rps/users defaults must NOT be written
    # as true values; instead profile metadata is written. For a short (smoke)
    # duration, target_average_rps must be omitted.
    import importlib

    old_profile = os.environ.get("LOAD_PROFILE")
    old_dur = os.environ.get("ORGANIZER_DURATION")
    old_max = os.environ.get("ORGANIZER_MAX_USERS")
    old_custom = os.environ.get("CUSTOM_METRICS_PATH")
    try:
        os.environ["LOAD_PROFILE"] = "organizer"
        os.environ["ORGANIZER_DURATION"] = "5"
        os.environ["ORGANIZER_MAX_USERS"] = "7"
        os.environ["CUSTOM_METRICS_PATH"] = str(tmp_path / "custom_metrics.json")
        importlib.reload(locustfile)
        locustfile._write_custom_metrics(None)
        data = json.loads((tmp_path / "custom_metrics.json").read_text(encoding="utf-8"))
        assert data["profile"] == "organizer"
        assert data["profile_duration_seconds"] == 5
        assert data["profile_max_users"] == 7
        # Static defaults must not be reported as true in organizer mode.
        assert "target_rps" not in data
        assert "users" not in data
        # Short (smoke) run: target_average_rps is not meaningful -> omitted.
        assert "target_average_rps" not in data
    finally:
        if old_profile is None:
            os.environ.pop("LOAD_PROFILE", None)
        else:
            os.environ["LOAD_PROFILE"] = old_profile
        if old_dur is None:
            os.environ.pop("ORGANIZER_DURATION", None)
        else:
            os.environ["ORGANIZER_DURATION"] = old_dur
        if old_max is None:
            os.environ.pop("ORGANIZER_MAX_USERS", None)
        else:
            os.environ["ORGANIZER_MAX_USERS"] = old_max
        if old_custom is None:
            os.environ.pop("CUSTOM_METRICS_PATH", None)
        else:
            os.environ["CUSTOM_METRICS_PATH"] = old_custom
        importlib.reload(locustfile)


def test_organizer_custom_metrics_full_profile_has_target_average(tmp_path: Path) -> None:
    # For the full 480s profile, target_average_rps IS written.
    import importlib

    old_profile = os.environ.get("LOAD_PROFILE")
    old_dur = os.environ.get("ORGANIZER_DURATION")
    old_custom = os.environ.get("CUSTOM_METRICS_PATH")
    try:
        os.environ["LOAD_PROFILE"] = "organizer"
        os.environ["ORGANIZER_DURATION"] = "480"
        os.environ["CUSTOM_METRICS_PATH"] = str(tmp_path / "custom_metrics_full.json")
        importlib.reload(locustfile)
        locustfile._write_custom_metrics(None)
        data = json.loads(
            (tmp_path / "custom_metrics_full.json").read_text(encoding="utf-8")
        )
        assert data["target_average_rps"] == pytest.approx(
            locustfile.ORGANIZER_TARGET_AVERAGE
        )
    finally:
        if old_profile is None:
            os.environ.pop("LOAD_PROFILE", None)
        else:
            os.environ["LOAD_PROFILE"] = old_profile
        if old_dur is None:
            os.environ.pop("ORGANIZER_DURATION", None)
        else:
            os.environ["ORGANIZER_DURATION"] = old_dur
        if old_custom is None:
            os.environ.pop("CUSTOM_METRICS_PATH", None)
        else:
            os.environ["CUSTOM_METRICS_PATH"] = old_custom
        importlib.reload(locustfile)


# --- child env isolation (defect 2) -----------------------------------------


def test_run_organizer_profile_forces_duration_480(monkeypatch, tmp_path: Path) -> None:
    # run_organizer_profile must force ORGANIZER_DURATION=480 in the child env so
    # an ambient ORGANIZER_DURATION (e.g. from a smoke) cannot leak in.
    import subprocess
    import sys

    from scripts import run_performance

    captured_env: dict[str, str] = {}

    class _FakeProc:
        def __init__(self, pid: int):
            self.pid = pid

        def wait(self) -> int:
            return 0

    def fake_popen(command, env):  # type: ignore[no-untyped-def]
        captured_env.update(env)
        return _FakeProc(12345)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run_performance, "_sample_process", lambda *a, **k: True)
    monkeypatch.setattr(sys, "executable", sys.executable)
    os.environ["ORGANIZER_DURATION"] = "5"  # ambient leak attempt
    try:
        run_performance.run_organizer_profile(
            "http://localhost:8000", "mixed", 10.0, 200, tmp_path
        )
    finally:
        os.environ.pop("ORGANIZER_DURATION", None)
    assert captured_env["LOAD_PROFILE"] == "organizer"
    assert captured_env["ORGANIZER_DURATION"] == "480"


def test_run_step_forces_static_profile_and_clears_organizer_env(
    monkeypatch, tmp_path: Path
) -> None:
    import subprocess

    from scripts import run_performance

    captured_env: dict[str, str] = {}

    class _FakeProc:
        def __init__(self, pid: int):
            self.pid = pid

        def wait(self) -> int:
            return 0

    def fake_popen(command, env):  # type: ignore[no-untyped-def]
        captured_env.update(env)
        return _FakeProc(12346)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run_performance, "_sample_process", lambda *a, **k: True)
    os.environ["LOAD_PROFILE"] = "organizer"
    os.environ["ORGANIZER_DURATION"] = "5"
    os.environ["ORGANIZER_MAX_USERS"] = "500"
    os.environ["ORGANIZER_RPS_PER_USER"] = "1"
    try:
        run_performance.run_step(
            "http://localhost:8000", 100.0, 10, 10, 60, "mixed", tmp_path
        )
    finally:
        os.environ.pop("LOAD_PROFILE", None)
        os.environ.pop("ORGANIZER_DURATION", None)
        os.environ.pop("ORGANIZER_MAX_USERS", None)
        os.environ.pop("ORGANIZER_RPS_PER_USER", None)
    assert captured_env["LOAD_PROFILE"] == "steps"
    assert "ORGANIZER_DURATION" not in captured_env
    assert "ORGANIZER_MAX_USERS" not in captured_env
    assert "ORGANIZER_RPS_PER_USER" not in captured_env


# --- benchmark exit codes and reconciliation (defect 4) ---------------------


def test_benchmark_counter_smoke() -> None:
    # Honest counter smoke: manually bumping counters to check the invariant
    # total == mask_attempts + demask_attempts. This does NOT exercise the real
    # _run_user/_post path (see test_benchmark_real_path below).
    from scripts import benchmark

    counters = benchmark._Counters()
    counters.bump("mask_attempts")
    counters.bump("total_requests")
    counters.bump("successful_mask")
    counters.bump("http_2xx")
    counters.bump("demask_attempts")
    counters.bump("total_requests")
    counters.bump("http_429")
    counters.bump("mask_attempts")
    counters.bump("total_requests")
    counters.bump("transport_errors")
    assert counters.mask_attempts + counters.demask_attempts == counters.total_requests


def test_benchmark_real_path_reconciliation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Exercise the actual _run_user() production path with a fake httpx.Client
    # and a deterministic single loop iteration (monkeypatched monotonic clock),
    # so the production code itself bumps the attempt/success counters.
    from scripts import benchmark

    class _FakeResp:
        def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
            self.status_code = status_code
            self._body = body
            self.headers = headers or {}

        def json(self) -> dict:
            if self._body is None:
                raise ValueError("no body")
            return self._body

    class _FakeClient:
        def __init__(self, responses: list[_FakeResp]):
            self._responses = list(responses)
            self._index = 0
            self.closed = False

        def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            resp = self._responses[self._index]
            self._index += 1
            return resp

        def close(self) -> None:
            self.closed = True

    masked = "t***@example.com"
    fake_client = _FakeClient(
        [
            _FakeResp(200, body={"result": masked}),  # mask
            _FakeResp(200, body={"result": benchmark.ORIGINAL}),  # demask
        ]
    )
    monkeypatch.setattr(benchmark.httpx, "Client", lambda *a, **k: fake_client)
    # Deterministic single iteration: the first monotonic() call computes the
    # deadline (0.0), the second is the first loop check (0.0 < 1.0 -> run), and
    # every later call is past the deadline so the loop exits.
    clock = iter([0.0, 0.0, 999.0, 999.0])

    def fake_monotonic() -> float:
        return next(clock)

    monkeypatch.setattr(benchmark.time, "monotonic", fake_monotonic)

    counters = benchmark._Counters()
    benchmark._run_user("http://localhost:8000", 1.0, counters)

    assert counters.mask_attempts == 1
    assert counters.demask_attempts == 1
    assert counters.successful_mask == 1
    assert counters.successful_demask == 1
    assert counters.total_requests == 2
    assert counters.functional_errors == 0
    assert counters.transport_errors == 0
    assert counters.mask_attempts + counters.demask_attempts == counters.total_requests
    assert fake_client.closed is True


def test_benchmark_exit_nonzero_on_all_429(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # All requests are valid 429s (with Retry-After): no functional error, but
    # zero successful mask/demask -> must exit non-zero.
    from scripts import benchmark

    counters = benchmark._Counters()
    counters.bump("total_requests")
    counters.bump("mask_attempts")
    counters.bump("http_429")
    monkeypatch.setattr(benchmark, "_run_user", lambda *a, **k: None)
    monkeypatch.setattr(benchmark, "_Counters", lambda: counters)
    assert benchmark.main(["--users", "1", "--duration", "0.1"]) != 0


def test_benchmark_exit_nonzero_on_zero_success(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Requests happened but no successful mask/demask -> must exit non-zero.
    from scripts import benchmark

    counters = benchmark._Counters()
    counters.bump("total_requests")
    counters.bump("mask_attempts")
    counters.bump("http_2xx")
    counters.bump("functional_errors")
    monkeypatch.setattr(benchmark, "_run_user", lambda *a, **k: None)
    monkeypatch.setattr(benchmark, "_Counters", lambda: counters)
    assert benchmark.main(["--users", "1", "--duration", "0.1"]) != 0


def test_benchmark_exit_nonzero_on_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from scripts import benchmark

    counters = benchmark._Counters()
    counters.bump("total_requests")
    counters.bump("mask_attempts")
    counters.bump("functional_errors")
    monkeypatch.setattr(benchmark, "_run_user", lambda *a, **k: None)
    monkeypatch.setattr(benchmark, "_Counters", lambda: counters)
    assert benchmark.main(["--users", "1", "--duration", "0.1"]) != 0


def test_benchmark_exit_nonzero_on_no_requests(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from scripts import benchmark

    counters = benchmark._Counters()
    monkeypatch.setattr(benchmark, "_run_user", lambda *a, **k: None)
    monkeypatch.setattr(benchmark, "_Counters", lambda: counters)
    assert benchmark.main(["--users", "1", "--duration", "0.1"]) == 2