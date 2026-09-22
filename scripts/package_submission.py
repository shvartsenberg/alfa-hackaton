"""Create a clean submission ZIP for the hackathon.

Usage:
    python scripts/package_submission.py [output_path]

Excludes VCS dirs, virtualenvs, caches, build artifacts, logs, and other
generated files. Does not include large datasets.
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    ".venv",
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
    ".egg-info",
}

EXCLUDED_SUFFIXES = {".pyc", ".log", ".zip"}


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix in EXCLUDED_SUFFIXES:
                continue
            files.append(path)
    return files


def build_zip(output_path: Path) -> None:
    files = collect_files(PROJECT_ROOT)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            arcname = file.relative_to(PROJECT_ROOT)
            zf.write(file, arcname)
    print(f"Created {output_path} with {len(files)} files")


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "submission.zip"
    build_zip(output)


if __name__ == "__main__":
    main()