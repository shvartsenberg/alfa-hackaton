"""Build or validate a deterministic, secret-free submission ZIP."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {
    ".git",
    ".kilo",
    ".idea",
    ".vscode",
    ".venv",
    ".deps",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    "htmlcov",
    "node_modules",
    "target",
    "build",
    "dist",
    "out",
    "bin",
    "obj",
    "artifacts",
    ".egg-info",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".zip"}
REQUIRED_FILES = {
    "README.md",
    "pyproject.toml",
    "app/main.py",
    "configs/consumers/default.yaml",
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    "tests/conftest.py",
    "tests/unit/test_masking.py",
    "scripts/package_submission.py",
    "docs/architecture.md",
}
NORMALIZED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def is_secret_environment_file(name: str) -> bool:
    """Exclude every .env variant except the documented example file."""
    return name.startswith(".env") and name != ".env.example"


def is_pytest_temp_dir(name: str) -> bool:
    """Return True for pytest temporary directories.

    Covers the conventional ``.pytest_cache``/``.pytest-*`` names and the
    mangled directories pytest can create inside the project root when a
    ``--basetemp`` path is mis-expanded on Windows (e.g. a ``~`` in a short path
    or backslashes stripped), such as ``...pytest-basetemp`` or
    ``...pytest-temp``. Any path component whose name contains ``.pytest`` or
    ``pytest-temp``/``pytest-basetemp`` is treated as a pytest temp dir and must
    never be packaged.
    """
    lowered = name.lower()
    return (
        lowered.startswith(".pytest")
        or lowered.startswith("pytest-")
        or "pytest-basetemp" in lowered
        or "pytest-temp" in lowered
        or ".pytest" in lowered
    )


def is_excluded_relative(path: Path) -> bool:
    return (
        is_secret_environment_file(path.name)
        or path.suffix.lower() in EXCLUDED_SUFFIXES
        or any(
            part in EXCLUDED_DIRS
            or part.endswith(".egg-info")
            or is_pytest_temp_dir(part)
            for part in path.parts
        )
    )


def collect_files(root: Path, excluded_paths: set[Path] | None = None) -> list[Path]:
    root = root.resolve()
    excluded = {path.resolve() for path in (excluded_paths or set())}
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            directory
            for directory in dirnames
            if directory not in EXCLUDED_DIRS
            and not directory.endswith(".egg-info")
            and not is_pytest_temp_dir(directory)
        )
        for filename in sorted(filenames):
            raw_path = Path(dirpath) / filename
            if raw_path.is_symlink():
                continue
            path = raw_path.resolve()
            if path in excluded:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if is_excluded_relative(relative):
                continue
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _unsafe_archive_name(name: str) -> bool:
    normalized = PurePosixPath(name.replace("\\", "/"))
    has_drive = bool(normalized.parts and normalized.parts[0].endswith(":"))
    return (
        normalized.is_absolute()
        or has_drive
        or ".." in normalized.parts
        or not normalized.parts
    )


def validate_zip(output_path: Path) -> None:
    """Reject corrupt, unsafe, secret-bearing, or incomplete archives."""
    with zipfile.ZipFile(output_path, "r") as archive:
        bad = archive.testzip()
        if bad is not None:
            raise RuntimeError(f"corrupt entry in archive: {bad}")
        names = archive.namelist()

    forbidden = []
    for name in names:
        relative = Path(*PurePosixPath(name.replace("\\", "/")).parts)
        if _unsafe_archive_name(name) or is_excluded_relative(relative):
            forbidden.append(name)
    if forbidden:
        raise RuntimeError(f"forbidden files in archive: {sorted(forbidden)}")

    missing = sorted(REQUIRED_FILES.difference(names))
    if missing:
        raise RuntimeError(f"required files missing from archive: {missing}")


def build_zip(output_path: Path, root: Path = PROJECT_ROOT) -> None:
    root = root.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    files = collect_files(root, excluded_paths={output_path})
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in files:
            archive_name = file.relative_to(root).as_posix()
            info = zipfile.ZipInfo(archive_name, date_time=NORMALIZED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, file.read_bytes())
    validate_zip(output_path)
    print(
        f"Created {output_path} with {len(files)} files "
        f"({output_path.stat().st_size} bytes, validated)"
    )


def _dev_deps_available() -> bool:
    """Return True when the dev toolchain (pytest) is importable.

    Used to decide whether the optional pytest subset is runnable. A missing
    dev dependency is an objective, detectable condition; it is the only reason
    the subset is skipped.
    """
    try:
        import pytest  # noqa: F401
    except ImportError:
        return False
    return True


def smoke_validate_zip(output_path: Path) -> None:
    """Unpack the archive, install it, and run a minimal smoke test.

    Verifies that a clean checkout from the ZIP installs, imports, and serves
    the /process contract. The core smoke (install/import/MASK/DEMASK) is
    mandatory. The pytest subset is run when the dev toolchain is available;
    a real test failure is an error, never silently swallowed.
    """
    validate_zip(output_path)
    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = Path(tmp) / "extract"
        extract_dir.mkdir()
        with zipfile.ZipFile(output_path, "r") as archive:
            archive.extractall(extract_dir)

        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", "."],
            cwd=extract_dir,
            check=True,
            capture_output=True,
        )

        smoke = (
            "from fastapi.testclient import TestClient; "
            "from app.main import app; "
            "c = TestClient(app); "
            "r = c.post('/process', json={'payload': 'test@example.com', 'payload_id': 'smoke'}); "
            "assert r.status_code == 200, r.text; "
            "masked = r.json()['result']; "
            "r2 = c.post('/process', json={'payload': masked, 'payload_id': 'smoke'}); "
            "assert r2.status_code == 200 and r2.json()['result'] == 'test@example.com', r2.text; "
            "print('smoke ok')"
        )
        subprocess.run(
            [sys.executable, "-c", smoke],
            cwd=extract_dir,
            check=True,
            capture_output=True,
        )

        # Optional pytest subset. It is skipped only when the dev toolchain is
        # objectively unavailable (pytest not importable). If pytest is present
        # and the subset fails, that is a real failure and aborts the smoke.
        if not _dev_deps_available():
            print("smoke pytest subset skipped: dev toolchain (pytest) not available")
        else:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-e",
                    ".[dev]",
                ],
                cwd=extract_dir,
                check=True,
                capture_output=True,
            )
            # Run the pytest subset with an explicit --basetemp inside the
            # extract dir. This avoids the system-wide pytest temp directory
            # (which can be unwritable or permission-restricted on some hosts)
            # and keeps all smoke artifacts inside the temporary extract dir.
            pytest_basetemp = extract_dir / ".pytest-temp"
            pytest_basetemp.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/integration/test_process.py",
                    "-q",
                    "--basetemp",
                    str(pytest_basetemp),
                ],
                cwd=extract_dir,
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                # PII-free diagnostic: only the exit code. The pytest output is
                # not echoed because it may contain payloads or secrets.
                raise RuntimeError(
                    f"smoke pytest subset failed (exit {proc.returncode})"
                )
            print("smoke pytest subset: ok")
    print(f"Smoke-validated {output_path.resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=PROJECT_ROOT / "submission.zip")
    parser.add_argument(
        "--validate",
        dest="validate_only",
        type=Path,
        help="validate an existing ZIP without rebuilding it",
    )
    parser.add_argument(
        "--smoke",
        dest="smoke_only",
        type=Path,
        help="unpack, install, and smoke-test an existing ZIP",
    )
    args = parser.parse_args()
    if args.smoke_only is not None:
        smoke_validate_zip(args.smoke_only)
        return 0
    if args.validate_only is not None:
        validate_zip(args.validate_only)
        print(f"Validated {args.validate_only.resolve()}")
        return 0
    build_zip(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
