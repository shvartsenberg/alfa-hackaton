"""Tests for secure and deterministic submission packaging."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.package_submission import build_zip, collect_files, validate_zip


def _minimal_project(root: Path) -> None:
    required = {
        "README.md": "readme",
        "pyproject.toml": "[project]\nname='test'\nversion='0'",
        "app/main.py": "app = None",
        "configs/consumers/default.yaml": "enabled: true",
        "Dockerfile": "FROM python:3.12",
        "docker-compose.yml": "services: {}",
        ".env.example": "MASKING_KEY=",
        "tests/conftest.py": "",
        "tests/unit/test_masking.py": "",
        "scripts/package_submission.py": "",
        "docs/architecture.md": "# architecture",
    }
    for relative, content in required.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_collect_files_excludes_generated_and_secret_files(tmp_path: Path) -> None:
    _minimal_project(tmp_path)
    excluded = (
        tmp_path / ".env",
        tmp_path / ".env.staging",
        tmp_path / "app" / "__pycache__" / "main.pyc",
        tmp_path / "package.egg-info" / "PKG-INFO",
        tmp_path / ".deps" / "pytest" / "__init__.py",
        tmp_path / ".git" / "config",
        tmp_path / "previous.zip",
    )
    for path in excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SAFE=value", encoding="utf-8")

    names = {path.relative_to(tmp_path).as_posix() for path in collect_files(tmp_path)}
    assert ".env.example" in names
    for path in excluded:
        assert path.relative_to(tmp_path).as_posix() not in names


def test_collect_files_excludes_pytest_temp_dirs(tmp_path: Path) -> None:
    _minimal_project(tmp_path)
    # A conventional pytest cache dir and a mangled --basetemp dir (the name
    # pytest can create inside the project root when a Windows short path with
    # a "~" is mis-expanded) must both be excluded from the archive.
    pytest_dirs = (
        tmp_path / ".pytest_cache" / "v" / "cache",
        tmp_path / ".pytest-temp" / "test_x0",
        tmp_path / "UsersSHARCE~1AppDataLocalTempopencodepytest-basetemp" / "test_y0",
        tmp_path / "UsersSharcenbergProjectsPythonalfa-hackaton.pytest-temp" / "test_z0",
    )
    for path in pytest_dirs:
        path.mkdir(parents=True, exist_ok=True)
        (path / "artifact.json").write_text("temp", encoding="utf-8")

    names = {path.relative_to(tmp_path).as_posix() for path in collect_files(tmp_path)}
    for path in pytest_dirs:
        assert path.relative_to(tmp_path).as_posix() not in names
    assert not any("pytest" in name for name in names)


def test_build_zip_is_deterministic_and_safe(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _minimal_project(project)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    build_zip(first, root=project)
    build_zip(second, root=project)
    validate_zip(first)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
    assert names == sorted(names)
    assert all(not name.startswith("/") and ".." not in Path(name).parts for name in names)


@pytest.mark.parametrize(
    "forbidden_name",
    ["../secret.txt", "/absolute.txt", ".env.secret", ".git/config", "x.pyc"],
)
def test_validate_zip_rejects_forbidden_entries(
    tmp_path: Path,
    forbidden_name: str,
) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for required in (
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
        ):
            archive.writestr(required, "safe")
        archive.writestr(forbidden_name, "secret")

    with pytest.raises(RuntimeError, match="forbidden files"):
        validate_zip(archive_path)


def test_validate_zip_rejects_missing_required_files(tmp_path: Path) -> None:
    archive_path = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        # Missing Dockerfile, docker-compose.yml, .env.example, tests, scripts,
        # docs -> must be rejected by the strengthened manifest.
        archive.writestr("README.md", "safe")
        archive.writestr("pyproject.toml", "safe")
        archive.writestr("app/main.py", "safe")
        archive.writestr("configs/consumers/default.yaml", "safe")

    with pytest.raises(RuntimeError, match="required files missing"):
        validate_zip(archive_path)
