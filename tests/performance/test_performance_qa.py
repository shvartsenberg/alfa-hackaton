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
    _apply_server_config,
    _connection_peak,
    _container_id_matches,
    _docker_peak,
    _finalize_connection_samples,
    _finalize_docker_samples,
    _parse_host_port,
    _sample_connections,
    build_result,
    check_health,
    collect_environment,
    collect_stage_timings,
    read_authoritative_aggregate,
    read_custom_metrics,
    read_history_metrics,
    redact_host,
    redact_url,
    run_parameters,
)
from tests.performance import locustfile

# Mark every test in this module as a performance test so that
# ``pytest -m performance`` selects exactly this file. The default pytest
# ``addopts`` excludes the ``performance`` marker, so these tests are opt-in.
pytestmark = pytest.mark.performance


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


# --- roundtrip reconciliation (MASK == DEMASK + incomplete) -----------------


def _roundtrip_result(custom: dict[str, int]) -> object:
    row = {
        "Request Count": str(custom.get("http_2xx", 0)),
        "Failure Count": "0",
        "Requests/s": "100",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }
    return build_result(
        row,
        target_rps=100,
        users=10,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=custom,
        cpu_measured=True,
        memory_measured=True,
    )


def test_roundtrip_balanced_passes() -> None:
    result = _roundtrip_result(
        {
            "http_2xx": 200,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 100,
            "demask_requests": 100,
            "incomplete_roundtrips": 0,
            "functional_errors": 0,
        }
    )
    assert result.passed is True


def test_roundtrip_reconciliation_with_one_incomplete() -> None:
    # MASK=100, DEMASK=99, incomplete=1 -> MASK == DEMASK + incomplete.
    result = _roundtrip_result(
        {
            "http_2xx": 199,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 100,
            "demask_requests": 99,
            "incomplete_roundtrips": 1,
            "functional_errors": 0,
        }
    )
    assert result.passed is True


def test_roundtrip_reconciliation_mismatch_fails() -> None:
    # MASK=100, DEMASK=98, incomplete=1 -> 100 != 98+1=99 -> FAIL.
    result = _roundtrip_result(
        {
            "http_2xx": 199,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 100,
            "demask_requests": 98,
            "incomplete_roundtrips": 1,
            "functional_errors": 0,
        }
    )
    assert result.passed is False
    assert any("reconciliation" in v for v in result.violations)


def test_roundtrip_invalid_incomplete_fails() -> None:
    # incomplete > MASK is impossible; reconciliation must fail.
    result = _roundtrip_result(
        {
            "http_2xx": 200,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 100,
            "demask_requests": 99,
            "incomplete_roundtrips": 5,
            "functional_errors": 0,
        }
    )
    assert result.passed is False


def test_roundtrip_large_incomplete_share_fails() -> None:
    # incomplete / MASK > 1% -> FAIL.
    result = _roundtrip_result(
        {
            "http_2xx": 200,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 100,
            "demask_requests": 90,
            "incomplete_roundtrips": 10,
            "functional_errors": 0,
        }
    )
    assert result.passed is False
    assert any("incomplete pairs" in v for v in result.violations)


def test_roundtrip_zero_mask_demask_fails() -> None:
    result = _roundtrip_result(
        {
            "http_2xx": 0,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 0,
            "demask_requests": 0,
            "incomplete_roundtrips": 0,
            "functional_errors": 0,
        }
    )
    assert result.passed is False
    assert any("no MASK or DEMASK" in v for v in result.violations)


def test_roundtrip_429_after_mask_increments_incomplete() -> None:
    """A 429 on the demask step after a successful MASK is an incomplete pair."""
    _reset_metrics()
    masked = "t***@example.com"
    # First response: successful MASK. Second response: 429 with Retry-After.
    user = _FakeUser(
        [
            _FakeResponse(200, body={"result": masked}),
            _FakeResponse(429, headers={"Retry-After": "1"}),
        ]
    )
    locustfile.ProcessUser.scenario_roundtrip(user)
    assert locustfile._CUSTOM_METRICS["mask_requests"] == 1
    assert locustfile._CUSTOM_METRICS["demask_requests"] == 0
    assert locustfile._CUSTOM_METRICS["incomplete_roundtrips"] == 1
    assert locustfile._CUSTOM_METRICS["http_429"] == 1
    assert locustfile._CUSTOM_METRICS["functional_errors"] == 0


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


# --- organizer scheduler (integral/token pacing) ----------------------------


def test_cumulative_tasks_is_monotone_and_matches_total() -> None:
    # The cumulative task integral must be monotone non-decreasing and reach the
    # analytic total at ORGANIZER_DURATION.
    prev = -1.0
    for t in (0, 1, 90, 180, 300, 330, 360, 480):
        val = locustfile._cumulative_tasks(t)
        assert val >= prev
        prev = val
    total = locustfile._cumulative_tasks(locustfile.ORGANIZER_DURATION)
    assert total > 0
    # The integral of target_rps_at / REQUESTS_PER_TASK over [0,480] must equal
    # the analytic average * duration / REQUESTS_PER_TASK.
    expected = (
        locustfile.ORGANIZER_TARGET_AVERAGE
        * locustfile.ORGANIZER_DURATION
        / locustfile.REQUESTS_PER_TASK[locustfile.SCENARIO]
    )
    assert total == pytest.approx(expected, rel=1e-6)


def test_scheduler_first_slot_is_after_zero_and_tasks_continue() -> None:
    # Regression: the old constant_throughput pacing parked users for ~1000s at
    # t=0 (rate clamped to 0.001). The integral scheduler must schedule the first
    # task strictly after t=0 and continue scheduling many tasks across the run.
    first = locustfile._scheduled_time_for_ordinal(1)
    assert first > 0.0
    second = locustfile._scheduled_time_for_ordinal(2)
    assert second > first
    # 200 users' first tasks must spread over the ramp, not burst at t=0.
    slot_200 = locustfile._scheduled_time_for_ordinal(200)
    assert slot_200 > first
    # A late slot must be scheduled well into the run (sustained load).
    late = locustfile._scheduled_time_for_ordinal(10000)
    assert late > 100.0
    # The last slot is at (or very near) the end of the profile.
    total = locustfile._cumulative_tasks(locustfile.ORGANIZER_DURATION)
    last = locustfile._scheduled_time_for_ordinal(total)
    assert last == pytest.approx(locustfile.ORGANIZER_DURATION, abs=1.0)


def test_scheduler_reset_restores_ordinal() -> None:
    # _reset_scheduler must restore the next ordinal to 1 so a rerun does not
    # continue from a stale counter.
    locustfile._SCHED_NEXT_ORDINAL = 500
    locustfile._reset_scheduler()
    assert locustfile._SCHED_NEXT_ORDINAL == 1
    assert locustfile._SCHED_TOTAL_SLOTS > 0


# --- authoritative achieved RPS (not Locust aggregate) ----------------------


def test_build_result_uses_measured_duration_for_achieved_rps() -> None:
    # Regression: the failed 480s run processed 400 requests but Locust's
    # aggregate "Requests/s" reported ~904 because it divides over the
    # request-active span. The authoritative achieved RPS must be
    # requests / measured wall duration.
    row = {
        "Request Count": "400",
        "Failure Count": "0",
        "Requests/s": "904",  # misleading Locust aggregate
        "50%": "350",
        "95%": "350",
        "99%": "350",
    }
    result = build_result(
        row,
        target_rps=330.9375,
        users=200,
        duration_seconds=480,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics={
            "http_2xx": 400,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 200,
            "demask_requests": 200,
            "incomplete_roundtrips": 0,
            "functional_errors": 0,
        },
        measured_duration=480.0,
    )
    # 400 requests / 480s = 0.833 RPS, NOT the misleading 904.
    assert result.achieved_rps == pytest.approx(400.0 / 480.0, rel=1e-6)
    assert result.passed is False


def test_build_result_elapsed_duration_gate_fails_when_too_short() -> None:
    row = {
        "Request Count": "400",
        "Failure Count": "0",
        "Requests/s": "904",
        "50%": "350",
        "95%": "350",
        "99%": "350",
    }
    result = build_result(
        row,
        target_rps=330.9375,
        users=200,
        duration_seconds=480,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics={
            "http_2xx": 400,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 200,
            "demask_requests": 200,
            "incomplete_roundtrips": 0,
            "functional_errors": 0,
        },
        measured_duration=480.0,
        min_elapsed_duration=475.0,
    )
    # 480s >= 475s, so the elapsed gate passes; the FAIL comes from the low
    # achieved RPS ratio.
    assert not any("measured elapsed" in v for v in result.violations)
    assert result.passed is False


def test_build_result_elapsed_duration_gate_fails_when_short_run() -> None:
    row = {
        "Request Count": "400",
        "Failure Count": "0",
        "Requests/s": "904",
        "50%": "350",
        "95%": "350",
        "99%": "350",
    }
    result = build_result(
        row,
        target_rps=330.9375,
        users=200,
        duration_seconds=480,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics={
            "http_2xx": 400,
            "http_429": 0,
            "http_422": 0,
            "http_other": 0,
            "transport_errors": 0,
            "mask_requests": 200,
            "demask_requests": 200,
            "incomplete_roundtrips": 0,
            "functional_errors": 0,
        },
        measured_duration=100.0,
        min_elapsed_duration=475.0,
    )
    assert any("measured elapsed" in v for v in result.violations)
    assert result.passed is False


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

# --- Reporting: configured vs observed users, peak RPS, gates ---------------


def _roundtrip_custom(mask: int = 100, demask: int = 100, incomplete: int = 0) -> dict[str, int]:
    return {
        "http_2xx": mask + demask,
        "http_429": 0,
        "http_422": 0,
        "http_other": 0,
        "transport_errors": 0,
        "mask_requests": mask,
        "demask_requests": demask,
        "incomplete_roundtrips": incomplete,
        "functional_errors": 0,
    }


def _row(requests: int = 200) -> dict[str, str]:
    return {
        "Request Count": str(requests),
        "Failure Count": "0",
        "Requests/s": "100",
        "50%": "5",
        "95%": "10",
        "99%": "20",
    }


def test_configured_users_not_mixed_with_observed() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        observed_max_users=100.0,
        observed_peak_rps=500.0,
        target_peak_rps=1000.0,
    )
    assert result.users == 200  # configured
    assert result.observed_max_users == 100.0  # observed, distinct


def test_required_concurrency_gate_fails_when_not_reached() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        observed_max_users=100.0,
        required_concurrent_users=200,
    )
    assert result.passed is False
    assert any("observed max users" in v for v in result.violations)


def test_required_concurrency_gate_passes_when_reached() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        observed_max_users=200.0,
        required_concurrent_users=200,
    )
    assert result.passed is True


def test_peak_rps_gate_fails_when_below_target() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        observed_peak_rps=500.0,
        target_peak_rps=1000.0,
        min_peak_ratio=0.9,
    )
    assert result.passed is False
    assert any("observed peak RPS" in v for v in result.violations)


def test_incomplete_ratio_gate_uses_configured_threshold() -> None:
    # 10 incomplete / 100 MASK = 10% > 5% threshold -> FAIL.
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(mask=100, demask=90, incomplete=10),
        max_incomplete_ratio=0.05,
    )
    assert result.passed is False
    assert any("incomplete pairs" in v for v in result.violations)


def test_redact_url_strips_credentials() -> None:
    assert redact_url("redis://:secret@host:6379/0") == "redis://host:6379/0"
    assert redact_url("redis://user:pass@host:6379/0") == "redis://host:6379/0"
    assert redact_url("http://localhost:8000") == "http://localhost:8000"


def test_redact_host_strips_userinfo_and_query() -> None:
    assert redact_host("http://user:pass@localhost:8000") == "http://localhost:8000"
    assert redact_host("http://localhost:8000?token=secret") == "http://localhost:8000"
    assert redact_host("http://user:pass@localhost:8000?token=secret#frag") == (
        "http://localhost:8000"
    )


def test_run_parameters_never_contains_secret_in_server_config() -> None:
    argv = [
        "python",
        "scripts/run_performance.py",
        "--profile",
        "organizer",
        "--server-config",
        "backend=redis,password=supersecret",
        "--host",
        "http://localhost:8000",
    ]
    params = run_parameters(argv)
    # The --server-config value (which may contain a password) must never be
    # persisted; only the allowlisted safe parameters are returned.
    assert "supersecret" not in json.dumps(params)
    assert "server_config" not in params
    assert params["profile"] == "organizer"
    assert params["host"] == "http://localhost:8000"


def test_run_parameters_redacts_host_query_token() -> None:
    argv = [
        "python",
        "scripts/run_performance.py",
        "--host",
        "http://localhost:8000?token=supersecret",
    ]
    params = run_parameters(argv)
    assert "supersecret" not in json.dumps(params)
    assert params["host"] == "http://localhost:8000"


def test_run_parameters_ignores_unknown_flags() -> None:
    argv = [
        "python",
        "scripts/run_performance.py",
        "--profile",
        "steps",
        "--unknown-flag",
        "MASKING_KEY=supersecret",
        "--targets",
        "100,330",
    ]
    params = run_parameters(argv)
    assert "supersecret" not in json.dumps(params)
    assert "unknown_flag" not in params
    assert params["profile"] == "steps"
    assert params["targets"] == "100,330"


def test_run_parameters_keeps_plain_args() -> None:
    argv = ["python", "scripts/run_performance.py", "--profile", "organizer"]
    assert run_parameters(argv) == {"profile": "organizer"}


def test_read_history_metrics(tmp_path: Path) -> None:
    history = tmp_path / "stats_stats_history.csv"
    history.write_text(
        "Timestamp,User Count,Type,Name,Requests/s\n"
        "1,0,,Aggregated,0.0\n"
        "2,50,,Aggregated,100.0\n"
        "3,200,,Aggregated,850.0\n"
        "4,150,,Aggregated,500.0\n",
        encoding="utf-8",
    )
    max_users, peak_rps = read_history_metrics(tmp_path)
    assert max_users == 200.0
    assert peak_rps == 850.0


def test_read_history_metrics_missing_returns_zero(tmp_path: Path) -> None:
    max_users, peak_rps = read_history_metrics(tmp_path)
    assert max_users == 0.0
    assert peak_rps == 0.0


# --- peak CPU/RAM reporting (new schema) ------------------------------------


def test_build_result_records_peak_cpu_and_memory() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        cpu_percent=10.0,
        memory_mb=50.0,
        peak_cpu_percent=45.0,
        peak_memory_mb=80.0,
        cpu_measured=True,
        memory_measured=True,
    )
    assert result.cpu_percent == 10.0
    assert result.memory_mb == 50.0
    assert result.peak_cpu_percent == 45.0
    assert result.peak_memory_mb == 80.0


def test_build_result_peak_unknown_when_not_measured() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        cpu_measured=False,
        memory_measured=False,
    )
    assert result.peak_cpu_percent is None
    assert result.peak_memory_mb is None


# --- docker peak reduction (during-run sampling) ----------------------------


def test_docker_peak_reduces_samples_to_peaks() -> None:
    samples = [
        {
            "app": {"cpu_percent": 10.0, "memory_mb": 50.0},
            "redis": {"cpu_percent": 5.0, "memory_mb": 20.0},
        },
        {
            "app": {"cpu_percent": 45.0, "memory_mb": 80.0},
            "redis": {"cpu_percent": 8.0, "memory_mb": 25.0},
        },
        {
            "app": {"cpu_percent": 30.0, "memory_mb": 70.0},
            "redis": {"cpu_percent": 6.0, "memory_mb": 22.0},
        },
    ]
    peak = _docker_peak(samples)
    assert peak["unavailable"] is False
    assert peak["app"] == {
        "sampled_peak_cpu_percent": 45.0,
        "sampled_peak_memory_mb": 80.0,
    }
    assert peak["redis"] == {
        "sampled_peak_cpu_percent": 8.0,
        "sampled_peak_memory_mb": 25.0,
    }


def test_docker_peak_unavailable_when_no_samples() -> None:
    peak = _docker_peak([])
    assert peak["unavailable"] is True
    assert peak["app"] is None
    assert peak["redis"] is None


def test_docker_peak_unavailable_when_docker_missing() -> None:
    peak = _docker_peak([{"unavailable": True}])
    assert peak["unavailable"] is True
    assert peak["app"] is None
    assert peak["redis"] is None


def test_container_id_matches_full_and_short_forms() -> None:
    full = "a1b2c3d4e5f60718293a4b5c6d7e8f901a2b3c4d5e6f708192a3b4c5d6e7f809"
    short = "a1b2c3d4e5f6"
    # docker compose ps -q returns the full ID; docker stats returns the short
    # ID. Both directions must match.
    assert _container_id_matches(short, full) is True
    assert _container_id_matches(full, short) is True
    # Identical IDs match.
    assert _container_id_matches(full, full) is True
    # Unrelated 12+ hex IDs do not match.
    assert _container_id_matches("deadbeefcafe", full) is False
    # Empty IDs never match.
    assert _container_id_matches("", full) is False
    assert _container_id_matches(short, "") is False


def test_container_id_matches_rejects_short_and_non_hex() -> None:
    full = "a1b2c3d4e5f60718293a4b5c6d7e8f901a2b3c4d5e6f708192a3b4c5d6e7f809"
    # A single-character prefix must not match (too short to be unambiguous).
    assert _container_id_matches("a", full) is False
    # A non-hexadecimal string must not match even if long enough.
    assert _container_id_matches("zzzzzzzzzzzz", full) is False
    assert _container_id_matches(full, "zzzzzzzzzzzz") is False
    # A short non-hex string must not match.
    assert _container_id_matches("deadbeef", full) is False


def test_finalize_docker_samples_marks_incomplete_when_thread_still_alive() -> None:
    import threading

    stop = threading.Event()
    samples: list[dict[str, object]] = []

    class _NeverEnding(threading.Thread):
        def join(self, timeout=None):  # type: ignore[override]
            # Simulate a sampler that never finishes within the join timeout.
            return None

        def is_alive(self) -> bool:
            return True

    thread = _NeverEnding()
    _finalize_docker_samples(thread, stop, samples)
    # The sampler did not finish, so the samples must be marked unavailable and
    # incomplete rather than racing with a still-running thread.
    assert samples[-1] == {"unavailable": True, "incomplete": True}
    assert stop.is_set()


def test_finalize_docker_samples_does_not_mark_when_thread_finishes() -> None:
    import threading

    stop = threading.Event()
    samples: list[dict[str, object]] = [{"app": None, "redis": None}]

    class _Quick(threading.Thread):
        def join(self, timeout=None):  # type: ignore[override]
            return None

        def is_alive(self) -> bool:
            return False

    _finalize_docker_samples(_Quick(), stop, samples)
    # The sampler finished, so no incomplete marker is appended.
    assert samples == [{"app": None, "redis": None}]
    assert stop.is_set()


def test_finalize_connection_samples_returns_false_when_thread_alive() -> None:
    import threading

    stop = threading.Event()
    samples: list[int | None] = [5]

    class _NeverEnding(threading.Thread):
        def join(self, timeout=None):  # type: ignore[override]
            return None

        def is_alive(self) -> bool:
            return True

    completed = _finalize_connection_samples(_NeverEnding(), stop, samples)
    # The sampler did not finish, so the caller must treat connections as
    # unavailable. The samples list is left untouched (the caller decides).
    assert completed is False
    assert samples == [5]
    assert stop.is_set()


def test_finalize_connection_samples_returns_true_when_thread_finishes() -> None:
    import threading

    stop = threading.Event()
    samples: list[int | None] = [5]

    class _Quick(threading.Thread):
        def join(self, timeout=None):  # type: ignore[override]
            return None

        def is_alive(self) -> bool:
            return False

    completed = _finalize_connection_samples(_Quick(), stop, samples)
    assert completed is True
    assert samples == [5]
    assert stop.is_set()


def test_connection_result_is_unavailable_when_sampler_does_not_finish() -> None:
    # Regression: even if old samples contain a numeric peak (5), a sampler that
    # is still alive after the join must NOT yield a partial peak. The final
    # result must be connections_measured=False, observed_peak_connections=None.
    import threading

    stop = threading.Event()
    samples: list[int | None] = [5]

    class _NeverEnding(threading.Thread):
        def join(self, timeout=None):  # type: ignore[override]
            return None

        def is_alive(self) -> bool:
            return True

    completed = _finalize_connection_samples(_NeverEnding(), stop, samples)
    if completed:
        conn_measured, conn_peak = _connection_peak(samples)
    else:
        conn_measured, conn_peak = False, None
    assert conn_measured is False
    assert conn_peak is None


# --- operator-declared server config ----------------------------------------


def test_apply_server_config_defaults_to_unverified() -> None:
    env = collect_environment()
    assert env["server_configuration"]["source"] == "operator_declared_unverified"
    assert env["server_configuration"]["backend"] is None


def test_apply_server_config_merges_operator_values() -> None:
    env = collect_environment()
    merged = _apply_server_config(env, "backend=redis,workers=4,logging=quiet")
    server = merged["server_configuration"]
    assert server["backend"] == "redis"
    assert server["workers"] == 4
    assert server["logging"] == "quiet"
    assert server["source"] == "operator_declared_unverified"


def test_apply_server_config_ignores_unknown_keys() -> None:
    env = collect_environment()
    merged = _apply_server_config(env, "backend=redis,secret=leak")
    server = merged["server_configuration"]
    assert server["backend"] == "redis"
    assert "secret" not in server


def test_apply_server_config_rejects_invalid_values() -> None:
    env = collect_environment()
    merged = _apply_server_config(
        env,
        "backend=mysql,workers=0,logging=verbose,workers=abc",
    )
    server = merged["server_configuration"]
    # Invalid backend, non-positive workers, and unknown logging are rejected.
    assert server["backend"] is None
    assert server["workers"] is None
    assert server["logging"] is None


def test_apply_server_config_never_stores_launch_command() -> None:
    env = collect_environment()
    merged = _apply_server_config(
        env,
        "backend=redis,launch_command=uvicorn app.main:app MASKING_KEY=topsecret",
    )
    server = merged["server_configuration"]
    # launch_command is deliberately not accepted: an arbitrary command may
    # contain secrets that cannot be reliably redacted.
    assert "launch_command" not in server
    assert "topsecret" not in json.dumps(server)
    assert server["backend"] == "redis"


# --- stage timings: observed max from gauge, not histogram bound ------------


def test_collect_stage_timings_uses_observed_max_gauge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import urllib.request

    body = (
        "# HELP pii_proxy_stage_duration_seconds Duration of a processing stage.\n"
        "# TYPE pii_proxy_stage_duration_seconds histogram\n"
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.001"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.005"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.01"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.025"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.05"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.1"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.25"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="0.5"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="1"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="2.5"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="5"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="10"} 0\n'
        'pii_proxy_stage_duration_seconds_bucket{stage="prepare",le="+Inf"} 2\n'
        'pii_proxy_stage_duration_seconds_sum{stage="prepare"} 0.02\n'
        'pii_proxy_stage_duration_seconds_count{stage="prepare"} 2\n'
        "# HELP pii_proxy_stage_max_seconds Largest observed duration of a processing stage.\n"
        "# TYPE pii_proxy_stage_max_seconds gauge\n"
        'pii_proxy_stage_max_seconds{stage="prepare"} 0.012\n'
    )

    class _FakeResp:
        def __init__(self, text: str):
            self._text = text

        def read(self) -> bytes:
            return self._text.encode("utf-8")

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResp(body),
    )
    result = collect_stage_timings("http://localhost:8000")
    prepare = result["prepare"]
    assert prepare["count"] == 2
    # The observed max must come from the gauge (0.012), not the largest
    # histogram bucket bound (10).
    assert prepare["max_seconds"] == 0.012
    assert prepare["scope"] == "responding_worker_only"


# --- ESTABLISHED TCP connection sampling (Locust process only) -------------


def test_parse_host_port_extracts_host_and_port() -> None:
    assert _parse_host_port("http://localhost:8000") == ("localhost", 8000)
    assert _parse_host_port("http://127.0.0.1:18099") == ("127.0.0.1", 18099)
    # Default port for http when omitted.
    assert _parse_host_port("http://example.com") == ("example.com", 80)
    # Userinfo is stripped safely.
    assert _parse_host_port("http://user:pass@example.com:8080") == ("example.com", 8080)
    # IPv6 literal is parsed safely.
    assert _parse_host_port("http://[::1]:8000") == ("::1", 8000)
    # Non-http schemes are not valid HTTP targets for connection sampling.
    assert _parse_host_port("redis://localhost:6379") is None
    # A non-numeric port cannot be resolved.
    assert _parse_host_port("http://localhost:abc") is None
    # Malformed input returns None.
    assert _parse_host_port("not a url") is None


def test_is_loopback_host() -> None:
    from scripts.run_performance import _is_loopback_host

    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("10.0.0.5") is False
    assert _is_loopback_host("example.com") is False


def test_connection_peak_returns_measured_and_peak() -> None:
    measured, peak = _connection_peak([1, 5, 3, 2])
    assert measured is True
    assert peak == 5.0


def test_connection_peak_unavailable_when_no_numeric_samples() -> None:
    # None samples (psutil missing / PermissionError / NoSuchProcess) must not
    # be treated as a false zero.
    measured, peak = _connection_peak([None, None])
    assert measured is False
    assert peak is None
    measured, peak = _connection_peak([])
    assert measured is False
    assert peak is None


def test_connection_peak_ignores_none_but_uses_numeric() -> None:
    measured, peak = _connection_peak([None, 4, None, 7])
    assert measured is True
    assert peak == 7.0


def test_sample_connections_counts_only_established_to_target(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import threading
    import time

    class _Conn:
        def __init__(self, status, ip, port):
            self.status = status
            self.raddr = type("R", (), {"ip": ip, "port": port})()

    class _FakeProc:
        def net_connections(self, kind="tcp"):
            return [
                _Conn("ESTABLISHED", "127.0.0.1", 8000),
                _Conn("ESTABLISHED", "127.0.0.1", 8000),
                _Conn("ESTABLISHED", "127.0.0.1", 9999),  # wrong port
                _Conn("TIME_WAIT", "127.0.0.1", 8000),  # not established
                _Conn("ESTABLISHED", "10.0.0.5", 8000),  # wrong host
            ]

    monkeypatch.setattr("psutil.Process", lambda pid: _FakeProc())
    stop = threading.Event()
    samples: list[int | None] = []
    thread = threading.Thread(
        target=_sample_connections,
        args=(123, stop, ("127.0.0.1", 8000), samples),
        daemon=True,
    )
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=2.0)
    # Only the two ESTABLISHED connections to 127.0.0.1:8000 are counted.
    assert samples == [2]


def test_sample_connections_does_not_count_loopback_for_remote_target(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import threading
    import time

    class _Conn:
        def __init__(self, status, ip, port):
            self.status = status
            self.raddr = type("R", (), {"ip": ip, "port": port})()

    class _FakeProc:
        def net_connections(self, kind="tcp"):
            return [
                _Conn("ESTABLISHED", "127.0.0.1", 8000),  # loopback, wrong target
                _Conn("ESTABLISHED", "10.0.0.5", 8000),  # matches target
            ]

    monkeypatch.setattr("psutil.Process", lambda pid: _FakeProc())
    stop = threading.Event()
    samples: list[int | None] = []
    thread = threading.Thread(
        target=_sample_connections,
        args=(123, stop, ("10.0.0.5", 8000), samples),
        daemon=True,
    )
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=2.0)
    # The loopback connection to 127.0.0.1:8000 must NOT be counted against the
    # remote target 10.0.0.5:8000; only the matching remote connection counts.
    assert samples == [1]


def test_sample_connections_records_none_on_access_denied(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import threading
    import time

    class _FakeProc:
        def net_connections(self, kind="tcp"):
            raise PermissionError("access denied")

    monkeypatch.setattr("psutil.Process", lambda pid: _FakeProc())
    stop = threading.Event()
    samples: list[int | None] = []
    thread = threading.Thread(
        target=_sample_connections,
        args=(123, stop, ("127.0.0.1", 8000), samples),
        daemon=True,
    )
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=2.0)
    # PermissionError must be recorded as None (unavailable), not a false zero.
    assert samples == [None]


def test_sample_connections_unavailable_when_target_unknown() -> None:
    import threading

    stop = threading.Event()
    samples: list[int | None] = []
    _sample_connections(123, stop, None, samples)
    assert samples == []


def test_build_result_connection_gate_fails_when_unavailable() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        connections_measured=False,
        observed_peak_connections=None,
        required_concurrent_connections=200,
    )
    assert result.passed is False
    assert any("connection sampling unavailable" in v for v in result.violations)


def test_build_result_connection_gate_fails_below_required() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        connections_measured=True,
        observed_peak_connections=150.0,
        required_concurrent_connections=200,
    )
    assert result.passed is False
    assert any("observed peak connections" in v for v in result.violations)


def test_build_result_connection_gate_passes_when_reached() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        connections_measured=True,
        observed_peak_connections=200.0,
        required_concurrent_connections=200,
    )
    assert result.passed is True


def test_build_result_records_connection_fields() -> None:
    result = build_result(
        _row(),
        target_rps=100,
        users=200,
        duration_seconds=60,
        locust_exit_code=0,
        max_error_rate=1,
        max_p95_ms=200,
        max_p99_ms=500,
        min_achieved_ratio=0.9,
        scenario="roundtrip",
        custom_metrics=_roundtrip_custom(),
        connections_measured=True,
        observed_peak_connections=180.0,
    )
    assert result.connections_measured is True
    assert result.observed_peak_connections == 180.0
