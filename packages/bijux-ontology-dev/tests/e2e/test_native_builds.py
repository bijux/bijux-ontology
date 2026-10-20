"""Real native preparation from unpacked distributions and normally installed code."""

import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


@pytest.mark.timeout(900)
def test_installed_builders_qualify_shipped_java_outside_the_checkout(
    distribution: tuple[Path, Path, dict[str, str]],
    installed: tuple[Path, Path, dict[str, str]],
) -> None:
    """Qualify both builders through actual positive and negative native checks."""
    maven, java = (
        os.environ.get("BIJUX_BUILD_MAVEN"),
        os.environ.get("BIJUX_BUILD_JAVA"),
    )
    if not maven or not java:
        pytest.skip("Native source builds require explicit Maven and JDK executables")
    python, work, environment = installed
    root = Path(__file__).resolve().parents[4]
    output = work / "reasoning-distribution"

    def run(
        command: list[str], *, cwd: Path = work
    ) -> subprocess.CompletedProcess[str]:
        """Exercise normal installed commands without source-path injection."""
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=700,
        )

    run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--sdist",
            "--outdir",
            str(output),
        ],
        cwd=root / "packages/bijux-ontology-reasoning",
    )
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        archive.extractall(work / "reasoning-source", filter="data")
    source = next((work / "reasoning-source").iterdir())
    run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--outdir",
            str(output),
        ],
        cwd=source,
    )
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            str(next(output.glob("*.whl"))),
        ]
    )
    run(["uv", "pip", "check", "--python", str(python)])
    dependencies = Path(os.environ.get("BIJUX_BUILD_DEPENDENCIES", str(work / "maven")))
    for package, source_java in (
        ("bijux_ontology.interfaces.adapters.jena.build", distribution[1] / "java"),
        ("bijux_ontology_reasoning.backend.owlapi.build", source / "java"),
    ):
        name = "jena" if "jena" in package else "owlapi"
        before = {
            path.relative_to(source_java): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_java.rglob("*")
            if path.is_file()
        }
        bundle, build = (
            work / (name + "-bundle with space"),
            work / (name + "-build with space"),
        )
        command = [
            str(python),
            "-I",
            "-B",
            "-m",
            package,
            "--source",
            str(source_java),
            "--maven",
            maven,
            "--java",
            java,
            "--build-directory",
            str(build),
            "--dependency-directory",
            str(dependencies),
            "--output",
            str(bundle),
        ]
        completed = run(command)
        receipt = json.loads(completed.stdout)
        manifest = bundle / "manifest.json"
        assert (
            hashlib.sha256(manifest.read_bytes()).hexdigest()
            == receipt["manifest_sha256"]
        )
        assert Path(receipt["bundle"]) == bundle and Path(
            receipt["log"]
        ).is_relative_to(build)
        assert before == {
            path.relative_to(source_java): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_java.rglob("*")
            if path.is_file()
        }
        refused = subprocess.run(
            command,
            cwd=work,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert refused.returncode != 0 and "must not already exist" in refused.stderr
        assert (
            hashlib.sha256(manifest.read_bytes()).hexdigest()
            == receipt["manifest_sha256"]
        )
