"""Run Deptry on a package's src tree with shared settings and real metadata."""

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> None:
    """Compose a scan manifest without changing either source configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    project = args.project_dir.resolve()
    package_text = (project / "pyproject.toml").read_text(encoding="utf-8")
    settings_text = args.config.read_text(encoding="utf-8")
    package = tomllib.loads(package_text)
    settings = tomllib.loads(settings_text)
    if not isinstance(package.get("project"), dict):
        parser.error("Package manifest must contain its project declarations")
    if "deptry" in package.get("tool", {}):
        parser.error("Deptry settings belong in the repository configuration")
    if set(settings) != {"tool"} or set(settings["tool"]) != {"deptry"}:
        parser.error("Repository settings must contain only tool.deptry")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    combined = (args.output_dir / "deptry-project.toml").resolve()
    combined.write_text(package_text + "\n" + settings_text, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "deptry", "--config", str(combined), "src"],
        cwd=project,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
