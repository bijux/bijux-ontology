"""Shared Deptry settings retain each package's real dependency boundary."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("undeclared", [False, True])
def test_dependency_scan_uses_package_manifest(
    tmp_path: Path, undeclared: bool
) -> None:
    root = Path(__file__).resolve().parents[5]
    package = tmp_path / "package"
    source = package / "src" / "dependency_probe"
    source.mkdir(parents=True)
    manifest = package / "pyproject.toml"
    original = (
        '[project]\nname = "dependency-probe"\nversion = "0.1.0"\ndependencies = []\n'
    )
    manifest.write_text(original, encoding="utf-8")
    (source / "__init__.py").write_text(
        "import rdflib\n" if undeclared else '"""A standard-library-only package."""\n',
        encoding="utf-8",
    )
    output = tmp_path / "evidence"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bijux_ontology_dev.quality.dependencies",
            "--config",
            str(root / "configs/deptry.toml"),
            "--project-dir",
            str(package),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == (1 if undeclared else 0), result.stdout + result.stderr
    if undeclared:
        assert "DEP003" in result.stdout + result.stderr
        assert "rdflib" in result.stdout + result.stderr
    assert manifest.read_text(encoding="utf-8") == original
    assert (output / "deptry-project.toml").is_file()
