"""Partition the canonical Make test inventory without filtering test cases."""

import argparse
import json
import math
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def test_files(root: Path) -> tuple[str, ...]:
    """Discover every test file owned by the canonical Make test packages."""
    result = subprocess.run(
        ["make", "--no-print-directory", "list-test-packages"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"}
        },
    )
    packages = result.stdout.split()
    files: list[str] = []
    for package in packages:
        directory = root / "packages" / package / "tests"
        if not directory.is_dir():
            raise ValueError(f"Test package has no test directory: {package}")
        files.extend(
            path.relative_to(root).as_posix()
            for path in set(directory.rglob("test_*.py"))
            | set(directory.rglob("*_test.py"))
        )
    return tuple(sorted(files))


def partition(
    files: Sequence[str], weights: Mapping[str, float], count: int = 5
) -> tuple[tuple[str, ...], ...]:
    """Assign the longest estimated files first, including every new file."""
    if count < 1 or len(files) < count or len(set(files)) != len(files):
        raise ValueError("Test groups require distinct files and nonempty groups")
    if any(not math.isfinite(value) or value <= 0 for value in weights.values()):
        raise ValueError("Test duration estimates must be finite and positive")
    groups: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for path in sorted(files, key=lambda path: (-weights.get(path, 1.0), path)):
        index = min(range(count), key=lambda index: (totals[index], index))
        groups[index].append(path)
        totals[index] += weights.get(path, 1.0)
    return tuple(tuple(sorted(group)) for group in groups)


def read_weights(path: Path) -> dict[str, float]:
    """Read duration hints only; estimates never decide whether a test runs."""
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected a test-file duration mapping")
    result = {}
    for key, value in data.items():
        if not isinstance(key, str) or type(value) not in (int, float):
            raise ValueError("Expected test-file paths and numeric durations")
        result[key] = float(value)
    return result


def main() -> None:
    """Run one complete group through each package's existing Make test gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    root = args.repo_root.resolve()
    matrix = json.loads((root / "configs/test-matrix.json").read_text())
    group_ids = matrix["group"]
    if group_ids != list(range(1, len(group_ids) + 1)) or args.group not in group_ids:
        parser.error("--group must name one contiguous configured group")
    files = test_files(root)
    weights = read_weights(root / "configs/test-groups.json")
    groups = partition(files, weights, len(group_ids))
    selected = groups[args.group - 1]
    output = root / "artifacts/ci/test-groups"
    output.mkdir(parents=True, exist_ok=True)
    (output / f"group-{args.group}.json").write_text(
        json.dumps(
            {
                "inventory": len(files),
                "groups": groups,
                "selected": args.group,
                "workers": args.workers,
                "python": sys.version.split()[0],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    packages: dict[str, list[str]] = {}
    for name in sorted(selected, key=lambda name: (-weights.get(name, 1.0), name)):
        path = Path(name)
        packages.setdefault(path.parts[1], []).append(
            path.relative_to(Path("packages") / path.parts[1]).as_posix()
        )
    failed = False
    for package, paths in sorted(packages.items()):
        result = subprocess.run(
            [
                "make",
                "test",
                f"PACKAGE={package}",
                f"TEST_PATHS={' '.join(paths)}",
                f"PYTHON={sys.executable}",
                f"PYTEST_ADDOPTS_EXTRA=-n {args.workers} --dist=load --maxschedchunk=1 "
                "--max-worker-restart=0",
            ],
            cwd=root,
            check=False,
        )
        failed = failed or result.returncode != 0
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
