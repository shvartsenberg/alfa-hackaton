"""Unit tests for performance report parsing and thresholds."""

from __future__ import annotations

import csv
from pathlib import Path

from scripts.run_performance import build_result, parse_targets, read_aggregate


def test_parse_targets_validates_positive_values() -> None:
    assert parse_targets("100,500,1000") == [100.0, 500.0, 1000.0]


def test_read_aggregate_does_not_sum_detail_rows(tmp_path: Path) -> None:
    path = tmp_path / "stats_stats.csv"
    fieldnames = [
        "Type",
        "Name",
        "Request Count",
        "Failure Count",
        "Requests/s",
        "50%",
        "95%",
        "99%",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "Type": "POST",
                "Name": "POST /process [mask]",
                "Request Count": "100",
                "Failure Count": "0",
                "Requests/s": "100",
            }
        )
        writer.writerow(
            {
                "Type": "",
                "Name": "Aggregated",
                "Request Count": "100",
                "Failure Count": "0",
                "Requests/s": "100",
                "50%": "5",
                "95%": "10",
                "99%": "20",
            }
        )

    aggregate = read_aggregate(path)
    assert aggregate["Request Count"] == "100"
    assert aggregate["Requests/s"] == "100"


def test_build_result_fails_when_target_rps_is_not_reached() -> None:
    row = {
        "Request Count": "1000",
        "Failure Count": "0",
        "Requests/s": "700",
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
    )
    assert result.passed is False
    assert any("achieved ratio" in violation for violation in result.violations)
