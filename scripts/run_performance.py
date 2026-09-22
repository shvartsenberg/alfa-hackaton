"""Run target-RPS Locust steps and persist machine-readable reports."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCUSTFILE = PROJECT_ROOT / "tests" / "performance" / "locustfile.py"
DEFAULT_TARGETS = "100,500,1000,2000"
SUPPORTED_SCENARIOS = (
    "mask_only",
    "roundtrip",
    "mask_retry",
    "demask_retry",
    "duplicate_payload",
    "large_payload",
    "mixed",
)


@dataclass(slots=True)
class StepResult:
    target_rps: float
    achieved_rps: float
    achieved_ratio: float
    requests: int
    failures: int
    error_rate_percent: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    users: int
    duration_seconds: int
    locust_exit_code: int
    passed: bool
    violations: list[str]


def parse_targets(spec: str) -> list[float]:
    targets = [float(item.strip()) for item in spec.split(",") if item.strip()]
    if not targets or any(target <= 0 for target in targets):
        raise ValueError("targets must be positive comma-separated numbers")
    return targets


def check_health(host: str, timeout_seconds: float = 5.0) -> None:
    url = f"{host.rstrip('/')}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise RuntimeError(f"healthcheck returned HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"healthcheck failed for {url}: {exc}") from exc


def run_step(
    host: str,
    target_rps: float,
    users: int,
    spawn_rate: float,
    duration_seconds: int,
    scenario: str,
    output_dir: Path,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_prefix = output_dir / "stats"
    command = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(LOCUSTFILE),
        "--host",
        host,
        "--headless",
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "-t",
        f"{duration_seconds}s",
        "--csv",
        str(csv_prefix),
        "--csv-full-history",
        "--only-summary",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "LOAD_SCENARIO": scenario,
            "TARGET_RPS": str(target_rps),
            "LOCUST_USERS": str(users),
        }
    )
    completed = subprocess.run(command, env=environment, check=False)
    return completed.returncode


def read_aggregate(stats_path: Path) -> dict[str, str]:
    with stats_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row.get("Name") == "Aggregated":
            return row
    raise RuntimeError(f"Aggregated row not found in {stats_path}")


def _number(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    return float(value) if value else 0.0


def empty_aggregate() -> dict[str, str]:
    """Return a synthetic failed aggregate when Locust produced no CSV."""
    return {
        "Request Count": "0",
        "Failure Count": "0",
        "Requests/s": "0",
        "50%": "0",
        "95%": "0",
        "99%": "0",
    }


def build_result(
    row: dict[str, str],
    *,
    target_rps: float,
    users: int,
    duration_seconds: int,
    locust_exit_code: int,
    max_error_rate: float,
    max_p95_ms: float,
    max_p99_ms: float,
    min_achieved_ratio: float,
) -> StepResult:
    requests = int(_number(row, "Request Count"))
    failures = int(_number(row, "Failure Count"))
    achieved_rps = _number(row, "Requests/s")
    error_rate = failures / requests * 100.0 if requests else 100.0
    ratio = achieved_rps / target_rps if target_rps else 0.0
    p50 = _number(row, "50%")
    p95 = _number(row, "95%")
    p99 = _number(row, "99%")
    violations: list[str] = []
    if locust_exit_code != 0:
        violations.append(f"locust exit code {locust_exit_code}")
    if error_rate > max_error_rate:
        violations.append(f"error rate {error_rate:.2f}% > {max_error_rate:.2f}%")
    if p95 > max_p95_ms:
        violations.append(f"p95 {p95:.1f}ms > {max_p95_ms:.1f}ms")
    if p99 > max_p99_ms:
        violations.append(f"p99 {p99:.1f}ms > {max_p99_ms:.1f}ms")
    if ratio < min_achieved_ratio:
        violations.append(f"achieved ratio {ratio:.3f} < {min_achieved_ratio:.3f}")
    return StepResult(
        target_rps=target_rps,
        achieved_rps=achieved_rps,
        achieved_ratio=ratio,
        requests=requests,
        failures=failures,
        error_rate_percent=error_rate,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        users=users,
        duration_seconds=duration_seconds,
        locust_exit_code=locust_exit_code,
        passed=not violations,
        violations=violations,
    )


def write_reports(output_dir: Path, scenario: str, results: list[StepResult]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scenario": scenario,
        "results": [asdict(result) for result in results],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        f"# Performance report: {scenario}",
        "",
        "| Target RPS | Actual RPS | Ratio | p50 | p95 | p99 | Errors | Result |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for result in results:
        status = "PASS" if result.passed else "FAIL: " + "; ".join(result.violations)
        lines.append(
            f"| {result.target_rps:.0f} | {result.achieved_rps:.1f} | "
            f"{result.achieved_ratio:.2f} | {result.p50_ms:.1f} ms | "
            f"{result.p95_ms:.1f} ms | {result.p99_ms:.1f} ms | "
            f"{result.error_rate_percent:.2f}% | {status} |"
        )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--targets", default=DEFAULT_TARGETS)
    parser.add_argument("--scenario", choices=SUPPORTED_SCENARIOS, default="mixed")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--rps-per-user", type=float, default=10.0)
    parser.add_argument("--spawn-rate", type=float, default=100.0)
    parser.add_argument("--max-error-rate", type=float, default=1.0)
    parser.add_argument("--max-p95-ms", type=float, default=200.0)
    parser.add_argument("--max-p99-ms", type=float, default=500.0)
    parser.add_argument("--min-achieved-ratio", type=float, default=0.9)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "performance"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    args = parser.parse_args()
    if args.duration <= 0 or args.rps_per_user <= 0 or args.spawn_rate <= 0:
        parser.error("duration, rps-per-user, and spawn-rate must be positive")

    targets = parse_targets(args.targets)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    check_health(args.host)
    results: list[StepResult] = []

    for target in targets:
        users = max(1, math.ceil(target / args.rps_per_user))
        step_dir = args.output_dir / f"rps-{target:g}"
        print(
            f"target={target:g} RPS users={users} scenario={args.scenario} "
            f"duration={args.duration}s"
        )
        exit_code = run_step(
            args.host,
            target,
            users,
            min(args.spawn_rate, users),
            args.duration,
            args.scenario,
            step_dir,
        )
        stats_path = step_dir / "stats_stats.csv"
        try:
            aggregate = read_aggregate(stats_path)
        except (OSError, RuntimeError) as exc:
            print(f"Locust statistics unavailable: {exc}", file=sys.stderr)
            aggregate = empty_aggregate()
            if exit_code == 0:
                exit_code = 2
        result = build_result(
            aggregate,
            target_rps=target,
            users=users,
            duration_seconds=args.duration,
            locust_exit_code=exit_code,
            max_error_rate=args.max_error_rate,
            max_p95_ms=args.max_p95_ms,
            max_p99_ms=args.max_p99_ms,
            min_achieved_ratio=args.min_achieved_ratio,
        )
        results.append(result)
        write_reports(args.output_dir, args.scenario, results)
        print(json.dumps(asdict(result), ensure_ascii=False))
        time.sleep(1)

    print(f"Reports: {args.output_dir}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
