"""Run one complete test group on each configured interpreter with separate receipts."""

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path


def main() -> None:
    """Retain separate proofs while reusing the frozen environment sequentially."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", type=int, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    matrix = json.loads((root / "configs/test-matrix.json").read_text())
    if args.group not in matrix["group"]:
        parser.error("--group must name a configured group")
    output = root / "artifacts/ci/test-groups"
    failed = False
    for runtime in matrix["python"]:
        interpreter = shutil.which(f"python{runtime}")
        if interpreter is None:
            raise SystemExit(f"Configured Python {runtime} is unavailable")
        subprocess.run(
            ["make", "install", f"PYTHON={interpreter}"], cwd=root, check=True
        )
        python = root / "artifacts/root/check-venv/bin/python"
        actual = subprocess.check_output(
            [
                str(python),
                "-I",
                "-B",
                "-c",
                "import sys; print(sys.version.split()[0])",
            ],
            text=True,
        ).strip()
        if not actual.startswith(runtime + "."):
            raise SystemExit(f"Expected Python {runtime}, found {actual}")
        owners = subprocess.check_output(
            ["make", "--no-print-directory", "list-test-packages"],
            cwd=root,
            text=True,
        ).split()
        for package in owners:
            for name in ("junit.xml", "coverage.xml", ".coverage"):
                (root / "artifacts" / package / "test" / name).unlink(missing_ok=True)
        start = time.monotonic()
        result = subprocess.run(
            [
                str(python),
                "-B",
                "-m",
                "bijux_ontology_dev.testing.shards",
                "--group",
                str(args.group),
            ],
            cwd=root,
            check=False,
        )
        destination = output / f"python-{runtime}"
        destination.mkdir(parents=True, exist_ok=True)
        group = output / f"group-{args.group}.json"
        selection = json.loads(group.read_text())
        shutil.copy2(group, destination / group.name)
        packages = {Path(name).parts[1] for name in selection["groups"][args.group - 1]}
        for package in sorted(packages):
            target = destination / package
            target.mkdir(exist_ok=True)
            for name in ("junit.xml", "coverage.xml", ".coverage"):
                source = root / "artifacts" / package / "test" / name
                if source.is_file():
                    shutil.copy2(source, target / name)
                elif result.returncode == 0:
                    raise SystemExit(f"Missing successful test receipt: {source}")
        (destination / "runtime.json").write_text(
            json.dumps(
                {
                    "requested": runtime,
                    "actual": actual,
                    "group": args.group,
                    "exit": result.returncode,
                    "seconds": time.monotonic() - start,
                },
                indent=2,
            )
            + "\n"
        )
        failed = failed or result.returncode != 0
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
