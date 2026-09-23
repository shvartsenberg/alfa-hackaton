"""Run target-RPS Locust steps and persist machine-readable reports.

Two modes:

- ``--profile steps`` (default): each ``--targets`` value runs as a separate
  headless Locust step and records the standard Locust percentiles plus per-run
  custom metrics (mask/demask counts, HTTP 2xx/429, functional and transport
  errors) written by the locustfile's event listener.
- ``--profile organizer``: a single continuous 480s Locust run driven by a
  LoadTestShape + dynamic wait_time that reproduces the organizers' requested
  load shape (ramp 0->330, steady 330, peak 1000, decay 1000->330, steady 330;
  target average 330.9375 RPS). Incompatible with ``--targets``.

CPU/RAM of the load generator is sampled best-effort when ``psutil`` is
available.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCUSTFILE = PROJECT_ROOT / "tests" / "performance" / "locustfile.py"
# Each target runs as a separate step of --duration with linear user spawn
# within that step only; this is not a single sustained-average profile.
DEFAULT_TARGETS = "100,330,500,1000"
DEFAULT_MAX_USERS = 200
SUPPORTED_SCENARIOS = (
    "mask_only",
    "roundtrip",
    "mask_retry",
    "demask_retry",
    "duplicate_payload",
    "large_payload",
    "mixed",
)
# Organizer continuous profile (single Locust run, no process restart).
ORGANIZER_DURATION = 480
ORGANIZER_TARGET_AVERAGE = 330.9375
ORGANIZER_HARD_MAX_USERS = 200
ORGANIZER_PHASES = (
    {"start": 0, "end": 180, "target_rps": 330, "kind": "ramp_up"},
    {"start": 180, "end": 300, "target_rps": 330, "kind": "steady"},
    {"start": 300, "end": 330, "target_rps": 1000, "kind": "peak"},
    {"start": 330, "end": 360, "target_rps": 330, "kind": "ramp_down"},
    {"start": 360, "end": 480, "target_rps": 330, "kind": "steady"},
)


@dataclass(slots=True)
class StepResult:
    target_rps: float
    achieved_rps: float
    achieved_ratio: float
    requests: int
    failures: int
    error_rate_percent: float
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    users: int
    duration_seconds: int
    mask_requests: int
    demask_requests: int
    http_2xx: int
    http_429: int
    http_422: int
    http_other: int
    functional_errors: int
    transport_errors: int
    custom_metrics_present: bool
    cpu_measured: bool
    memory_measured: bool
    cpu_percent: float | None
    memory_mb: float | None
    locust_exit_code: int
    passed: bool
    violations: list[str] = field(default_factory=list)


def parse_targets(spec: str) -> list[float]:
    targets = [float(item.strip()) for item in spec.split(",") if item.strip()]
    if not targets or any(target <= 0 for target in targets):
        raise ValueError("targets must be positive comma-separated numbers")
    return targets


def check_health(host: str, timeout_seconds: float = 5.0) -> None:
    """Verify the service is ready to accept load.

    Uses ``GET /health/ready`` (readiness), which actually checks dependencies
    and may return 503 when they are unavailable, unlike the always-200
    liveness ``GET /health``. Requires HTTP 200 and a JSON body with
    ``status == "ready"``. The response body is never written into errors.
    """
    url = f"{host.rstrip('/')}/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise RuntimeError(f"readiness returned HTTP {response.status}")
            try:
                body = json.loads(response.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("readiness returned a non-JSON body") from exc
            if not isinstance(body, dict) or body.get("status") != "ready":
                raise RuntimeError("readiness did not report status=ready")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"readiness check failed for {url}: {exc}") from exc


def _sample_process(pid: int, stop: threading.Event, samples: list[tuple[float, float]]) -> bool:
    """Sample CPU% and RSS (MB) of a process until ``stop`` is set.

    Returns ``True`` if at least one sample was recorded, ``False`` otherwise
    (psutil missing, process gone, or no samples captured).
    """
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    while not stop.is_set():
        try:
            cpu = proc.cpu_percent(interval=0.5)
            mem = proc.memory_info().rss / (1024 * 1024)
            samples.append((cpu, mem))
        except psutil.NoSuchProcess:
            break
    return bool(samples)


def run_step(
    host: str,
    target_rps: float,
    users: int,
    spawn_rate: float,
    duration_seconds: int,
    scenario: str,
    output_dir: Path,
) -> tuple[int, float, float, bool, bool]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_prefix = output_dir / "stats"
    custom_metrics_path = output_dir / "custom_metrics.json"
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
    # Static mode: force LOAD_PROFILE=steps and clear any organizer-profile env
    # that might leak from the ambient environment, so a static run can never
    # accidentally activate the organizer shape.
    for key in (
        "ORGANIZER_DURATION",
        "ORGANIZER_MAX_USERS",
        "ORGANIZER_RPS_PER_USER",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "LOAD_SCENARIO": scenario,
            "LOAD_PROFILE": "steps",
            "TARGET_RPS": str(target_rps),
            "LOCUST_USERS": str(users),
            "CUSTOM_METRICS_PATH": str(custom_metrics_path),
        }
    )
    stop = threading.Event()
    samples: list[tuple[float, float]] = []
    process = subprocess.Popen(command, env=environment)
    sampler = threading.Thread(
        target=_sample_process, args=(process.pid, stop, samples), daemon=True
    )
    sampler.start()
    try:
        returncode = process.wait()
    finally:
        stop.set()
        sampler.join(timeout=2.0)
    measured = bool(samples)
    avg_cpu = sum(s[0] for s in samples) / len(samples) if samples else 0.0
    avg_mem = sum(s[1] for s in samples) / len(samples) if samples else 0.0
    return returncode, avg_cpu, avg_mem, measured, measured


def run_organizer_profile(
    host: str,
    scenario: str,
    rps_per_user: float,
    max_users: int,
    output_dir: Path,
) -> tuple[int, float, float, bool, bool]:
    """Run the single continuous organizer profile and return process stats.

    The Locust process is started once; the LoadTestShape in the locustfile
    drives the user count and the dynamic wait_time paces each user so the
    target RPS follows the organizer profile over 480s. Returns the same tuple
    shape as ``run_step`` (exit code, avg CPU, avg RAM, cpu measured, mem
    measured).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_prefix = output_dir / "stats"
    custom_metrics_path = output_dir / "custom_metrics.json"
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
        "1",
        "-r",
        "1",
        "-t",
        f"{ORGANIZER_DURATION}s",
        "--csv",
        str(csv_prefix),
        "--csv-full-history",
        "--only-summary",
    ]
    environment = os.environ.copy()
    # Force the full 480s profile duration in the child env so an ambient
    # ORGANIZER_DURATION (e.g. from a short smoke) cannot leak into a real
    # organizer run and be misreported as a full profile.
    environment.update(
        {
            "LOAD_SCENARIO": scenario,
            "LOAD_PROFILE": "organizer",
            "ORGANIZER_DURATION": str(ORGANIZER_DURATION),
            "ORGANIZER_RPS_PER_USER": str(rps_per_user),
            "ORGANIZER_MAX_USERS": str(max_users),
            "CUSTOM_METRICS_PATH": str(custom_metrics_path),
        }
    )
    stop = threading.Event()
    samples: list[tuple[float, float]] = []
    process = subprocess.Popen(command, env=environment)
    sampler = threading.Thread(
        target=_sample_process, args=(process.pid, stop, samples), daemon=True
    )
    sampler.start()
    try:
        returncode = process.wait()
    finally:
        stop.set()
        sampler.join(timeout=2.0)
    measured = bool(samples)
    avg_cpu = sum(s[0] for s in samples) / len(samples) if samples else 0.0
    avg_mem = sum(s[1] for s in samples) / len(samples) if samples else 0.0
    return returncode, avg_cpu, avg_mem, measured, measured


def read_aggregate(stats_path: Path) -> dict[str, str]:
    with stats_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row.get("Name") == "Aggregated":
            return row
    raise RuntimeError(f"Aggregated row not found in {stats_path}")


def read_custom_metrics(path: Path) -> dict[str, int] | None:
    """Read custom metrics, returning ``None`` when absent or unparseable.

    ``None`` is distinct from an empty dict so callers can tell "no metrics were
    produced" apart from "metrics were produced but all counters are zero".
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {key: int(data[key]) for key in data if isinstance(data.get(key), int)}


def _number(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    return float(value) if value else 0.0


def empty_aggregate() -> dict[str, str]:
    """Return a synthetic failed aggregate when Locust produced no CSV."""
    return {
        "Request Count": "0",
        "Failure Count": "0",
        "Requests/s": "0",
        "Average Response Time": "0",
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
    custom_metrics: dict[str, int] | None = None,
    cpu_percent: float | None = None,
    memory_mb: float | None = None,
    cpu_measured: bool = False,
    memory_measured: bool = False,
    min_duration_for_stable: int = 30,
) -> StepResult:
    custom_metrics_present = custom_metrics is not None
    custom_metrics = custom_metrics or {}
    requests = int(_number(row, "Request Count"))
    failures = int(_number(row, "Failure Count"))
    achieved_rps = _number(row, "Requests/s")
    error_rate = failures / requests * 100.0 if requests else 100.0
    ratio = achieved_rps / target_rps if target_rps else 0.0
    avg = _number(row, "Average Response Time")
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
    if not custom_metrics_present:
        violations.append("custom metrics missing or unparseable")
    else:
        # Required keys must be present; a missing/invalid key is not a zero.
        required = (
            "http_2xx",
            "http_429",
            "http_422",
            "http_other",
            "transport_errors",
            "mask_requests",
            "demask_requests",
            "functional_errors",
        )
        missing = [key for key in required if key not in custom_metrics]
        if missing:
            violations.append(f"custom metrics missing required keys: {missing}")
        # Strict reconciliation of the custom status buckets with the Locust
        # request count. Any mismatch is a FAIL, never silently tolerated.
        total_http = (
            custom_metrics.get("http_2xx", 0)
            + custom_metrics.get("http_429", 0)
            + custom_metrics.get("http_422", 0)
            + custom_metrics.get("http_other", 0)
            + custom_metrics.get("transport_errors", 0)
        )
        if requests and total_http != requests:
            violations.append(
                f"counter mismatch: custom http total {total_http} != "
                f"locust request count {requests}"
            )
        # Every HTTP 200 bumps exactly one kind counter (mask or demask), so
        # mask_requests + demask_requests must equal http_2xx.
        mask_demask = custom_metrics.get("mask_requests", 0) + custom_metrics.get(
            "demask_requests", 0
        )
        if mask_demask != custom_metrics.get("http_2xx", 0):
            violations.append(
                f"counter mismatch: mask+demask {mask_demask} != http_2xx "
                f"{custom_metrics.get('http_2xx', 0)}"
            )
        # Locust's Failure Count must reconcile with the custom functional +
        # transport error counters. Valid 429s and expected 422s are not
        # failures, so in a normal harness run these must match.
        custom_errors = custom_metrics.get("functional_errors", 0) + custom_metrics.get(
            "transport_errors", 0
        )
        if failures != custom_errors:
            violations.append(
                f"counter mismatch: locust failure count {failures} != "
                f"custom functional+transport errors {custom_errors}"
            )
        # A run where every request is a 429 (even with valid Retry-After) and
        # no successful MASK/DEMASK happened is NOT a valid green verdict: it
        # does not prove the service actually works. Require at least one 2xx
        # and at least one successful mask for any scenario.
        if custom_metrics.get("http_2xx", 0) <= 0:
            violations.append("no successful 2xx responses: all requests were 429/errors")
        if custom_metrics.get("mask_requests", 0) <= 0:
            violations.append("no successful mask operations recorded")
    if duration_seconds < min_duration_for_stable:
        violations.append(
            f"duration {duration_seconds}s < {min_duration_for_stable}s: "
            "percentiles are not stable"
        )
    return StepResult(
        target_rps=target_rps,
        achieved_rps=achieved_rps,
        achieved_ratio=ratio,
        requests=requests,
        failures=failures,
        error_rate_percent=error_rate,
        avg_ms=avg,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        users=users,
        duration_seconds=duration_seconds,
        mask_requests=custom_metrics.get("mask_requests", 0),
        demask_requests=custom_metrics.get("demask_requests", 0),
        http_2xx=custom_metrics.get("http_2xx", 0),
        http_429=custom_metrics.get("http_429", 0),
        http_422=custom_metrics.get("http_422", 0),
        http_other=custom_metrics.get("http_other", 0),
        functional_errors=custom_metrics.get("functional_errors", 0),
        transport_errors=custom_metrics.get("transport_errors", 0),
        custom_metrics_present=custom_metrics_present,
        cpu_measured=cpu_measured,
        memory_measured=memory_measured,
        cpu_percent=cpu_percent,
        memory_mb=memory_mb,
        locust_exit_code=locust_exit_code,
        passed=not violations,
        violations=violations,
    )


def write_reports(
    output_dir: Path,
    scenario: str,
    results: list[StepResult],
    profile: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scenario": scenario,
        "results": [asdict(result) for result in results],
    }
    if profile is not None:
        payload["profile"] = profile
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        f"# Performance report: {scenario}",
        "",
        "| Target RPS | Actual RPS | Ratio | avg | p50 | p95 | p99 | "
        "MASK | DEMASK | 2xx | 429 | 422 | other | func-err | t-err | "
        "CPU% | RAM MB | dur | Errors | Result |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for result in results:
        status = "PASS" if result.passed else "FAIL: " + "; ".join(result.violations)
        cpu = f"{result.cpu_percent:.1f}" if result.cpu_percent is not None else "n/a"
        mem = f"{result.memory_mb:.1f}" if result.memory_mb is not None else "n/a"
        lines.append(
            f"| {result.target_rps:.0f} | {result.achieved_rps:.1f} | "
            f"{result.achieved_ratio:.2f} | {result.avg_ms:.1f} ms | "
            f"{result.p50_ms:.1f} ms | {result.p95_ms:.1f} ms | {result.p99_ms:.1f} ms | "
            f"{result.mask_requests} | {result.demask_requests} | {result.http_2xx} | "
            f"{result.http_429} | {result.http_422} | {result.http_other} | "
            f"{result.functional_errors} | {result.transport_errors} | "
            f"{cpu} | {mem} | "
            f"{result.duration_seconds}s | "
            f"{result.error_rate_percent:.2f}% | {status} |"
        )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument(
        "--profile",
        choices=["steps", "organizer"],
        default="steps",
        help="'steps' runs each --targets value as a separate step; "
        "'organizer' runs the single continuous 480s organizer profile "
        "(incompatible with --targets).",
    )
    parser.add_argument("--targets", default=None)
    parser.add_argument("--scenario", choices=SUPPORTED_SCENARIOS, default="mixed")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--min-duration-for-stable", type=int, default=30)
    parser.add_argument("--rps-per-user", type=float, default=10.0)
    parser.add_argument("--spawn-rate", type=float, default=100.0)
    parser.add_argument("--max-users", type=int, default=DEFAULT_MAX_USERS)
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
    if args.max_users <= 0:
        parser.error("max-users must be positive")
    if args.profile == "organizer" and args.targets is not None:
        parser.error("--targets cannot be used with --profile organizer")
    if args.profile == "organizer" and args.max_users > ORGANIZER_HARD_MAX_USERS:
        parser.error(
            f"--max-users must be <= {ORGANIZER_HARD_MAX_USERS} with "
            "--profile organizer"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    check_health(args.host)

    if args.profile == "organizer":
        return _run_organizer(args)

    targets = parse_targets(args.targets or DEFAULT_TARGETS)
    results: list[StepResult] = []

    for target in targets:
        users = min(args.max_users, max(1, math.ceil(target / args.rps_per_user)))
        step_dir = args.output_dir / f"rps-{target:g}"
        print(
            f"target={target:g} RPS users={users} scenario={args.scenario} "
            f"duration={args.duration}s"
        )
        exit_code, avg_cpu, avg_mem, cpu_measured, mem_measured = run_step(
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
        custom_metrics = read_custom_metrics(step_dir / "custom_metrics.json")
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
            custom_metrics=custom_metrics,
            cpu_percent=avg_cpu if cpu_measured else None,
            memory_mb=avg_mem if mem_measured else None,
            cpu_measured=cpu_measured,
            memory_measured=mem_measured,
            min_duration_for_stable=args.min_duration_for_stable,
        )
        results.append(result)
        write_reports(args.output_dir, args.scenario, results)
        print(json.dumps(asdict(result), ensure_ascii=False))
        time.sleep(1)

    print(f"Reports: {args.output_dir}")
    return 0 if all(result.passed for result in results) else 1


def _run_organizer(args: argparse.Namespace) -> int:
    """Run the single continuous organizer profile and persist its report."""
    print(
        f"profile=organizer scenario={args.scenario} "
        f"duration={ORGANIZER_DURATION}s target_average={ORGANIZER_TARGET_AVERAGE:.4f} "
        f"max_users={args.max_users} rps_per_user={args.rps_per_user}"
    )
    exit_code, avg_cpu, avg_mem, cpu_measured, mem_measured = run_organizer_profile(
        args.host,
        args.scenario,
        args.rps_per_user,
        args.max_users,
        args.output_dir,
    )
    stats_path = args.output_dir / "stats_stats.csv"
    try:
        aggregate = read_aggregate(stats_path)
    except (OSError, RuntimeError) as exc:
        print(f"Locust statistics unavailable: {exc}", file=sys.stderr)
        aggregate = empty_aggregate()
        if exit_code == 0:
            exit_code = 2
    custom_metrics = read_custom_metrics(args.output_dir / "custom_metrics.json")
    result = build_result(
        aggregate,
        target_rps=ORGANIZER_TARGET_AVERAGE,
        users=args.max_users,
        duration_seconds=ORGANIZER_DURATION,
        locust_exit_code=exit_code,
        max_error_rate=args.max_error_rate,
        max_p95_ms=args.max_p95_ms,
        max_p99_ms=args.max_p99_ms,
        min_achieved_ratio=args.min_achieved_ratio,
        custom_metrics=custom_metrics,
        cpu_percent=avg_cpu if cpu_measured else None,
        memory_mb=avg_mem if mem_measured else None,
        cpu_measured=cpu_measured,
        memory_measured=mem_measured,
        min_duration_for_stable=args.min_duration_for_stable,
    )
    profile = {
        "name": "organizer",
        "duration_seconds": ORGANIZER_DURATION,
        "target_average_rps": ORGANIZER_TARGET_AVERAGE,
        "max_users": args.max_users,
        "rps_per_user": args.rps_per_user,
        "phases": list(ORGANIZER_PHASES),
        "actual_average_rps": result.achieved_rps,
        "target_vs_actual_delta": result.achieved_rps - ORGANIZER_TARGET_AVERAGE,
    }
    write_reports(args.output_dir, args.scenario, [result], profile=profile)
    print(json.dumps(asdict(result), ensure_ascii=False))
    print(f"Reports: {args.output_dir}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())