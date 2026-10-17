"""Build once from an sdist and install exact locked runtime variants."""

import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def distribution(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    root = Path(__file__).resolve().parents[4]
    assert (root / "packages/bijux-ontology/LICENSE").read_bytes() == (
        root / "LICENSE"
    ).read_bytes()
    work = tmp_path_factory.mktemp("installed-package")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("MYPYPATH", None)
    env["UV_CACHE_DIR"] = str(root / "artifacts/cache/uv")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"

    def run(arguments: list[str], cwd: Path = root) -> None:
        subprocess.run(arguments, cwd=cwd, env=env, check=True, capture_output=True)

    run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(work / "dist"),
        ],
        cwd=root / "packages/bijux-ontology",
    )
    archive = next((work / "dist").glob("*.tar.gz"))
    source = work / "source"
    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
        assert len(names) == len(set(names)), "Source archive contains duplicate paths"
        bundle.extractall(source, filter="data")
    unpacked = next(source.iterdir())
    for target in re.findall(
        r"\]\(([^)]+)\)", (unpacked / "README.md").read_text(encoding="utf-8")
    ):
        if not target.startswith(("https:", "http:", "#")):
            assert (unpacked / target.split("#", 1)[0]).is_file(), target
    run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(work / "dist"),
        ],
        cwd=unpacked,
    )
    wheel = next((work / "dist").glob("*.whl"))
    return wheel, unpacked, env


def _install_distribution(
    distribution: tuple[Path, Path, dict[str, str]],
    work: Path,
    *,
    extras: bool,
) -> tuple[Path, Path, dict[str, str]]:
    """Install locked dependencies separately from the local project wheel."""
    wheel, root, env = distribution

    def run(arguments: list[str], cwd: Path = root) -> None:
        subprocess.run(arguments, cwd=cwd, env=env, check=True, capture_output=True)

    constraints = work / "runtime-requirements.txt"
    run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--package",
            "bijux-ontology",
            "--no-emit-workspace",
            *(["--all-extras"] if extras else []),
            "--format",
            "requirements-txt",
            "--output-file",
            str(constraints),
        ],
        cwd=Path(__file__).resolve().parents[4],
    )
    run(["uv", "venv", "--python", sys.executable, str(work / "env")])
    scripts = work / "env" / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    run(
        [
            "uv",
            "pip",
            "sync",
            "--require-hashes",
            "--python",
            str(python),
            str(constraints),
        ]
    )
    run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(python),
            str(wheel),
        ]
    )
    run(["uv", "pip", "check", "--python", str(python)])
    (work / "ontology.ttl").write_text(
        "@prefix ex: <https://example.org/> . "
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
        "ex:Image a owl:Class .",
        encoding="utf-8",
    )
    return python, work, env


@pytest.fixture(scope="session")
def installed(
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Expose the installed core-only wheel, outside every source checkout."""
    return _install_distribution(
        distribution, tmp_path_factory.mktemp("installed-core"), extras=False
    )


@pytest.fixture(scope="session")
def installed_all_extras(
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Expose the same wheel with all shipped integrations installed."""
    return _install_distribution(
        distribution, tmp_path_factory.mktemp("installed-extras"), extras=True
    )
