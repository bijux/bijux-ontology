"""Build once from an sdist and install exact locked runtime variants."""

import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
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
    extras: tuple[str, ...] = (),
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
            *(argument for extra in extras for argument in ("--extra", extra)),
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
    target = str(wheel) + ("[" + ",".join(extras) + "]" if extras else "")
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
            "--python",
            str(python),
            target,
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
        distribution, tmp_path_factory.mktemp("installed-core")
    )


@pytest.fixture(scope="session")
def installed_jsonld(
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Expose the wheel with its JSON-LD capability and no framework extras."""
    return _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-jsonld"),
        extras=("jsonld",),
    )


@pytest.fixture(scope="session")
def installed_integrations(
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Expose the compatible integration set; native package readers are separate."""
    _, source, _ = distribution
    optional = tomllib.loads((source / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    return _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-integrations"),
        extras=tuple(extra for extra in optional if extra != "frictionless"),
    )


@pytest.fixture(scope="session")
def installed_frictionless(
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Qualify native packages without OAK's conflicting character detector."""
    return _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-frictionless"),
        extras=("frictionless",),
    )


@pytest.fixture(scope="session")
def installed_reasoning(
    installed: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Build the optional typed distribution from its sdist beside exact core."""
    python, _, env = installed
    work = tmp_path_factory.mktemp("installed-reasoning")
    root = Path(__file__).resolve().parents[4]

    def run(arguments: list[str], cwd: Path) -> None:
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
        root / "packages/bijux-ontology-reasoning",
    )
    with tarfile.open(next((work / "dist").glob("*.tar.gz"))) as archive:
        assert any(name.endswith("/py.typed") for name in archive.getnames())
        archive.extractall(work / "source", filter="data")
    source = next((work / "source").iterdir())
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
        source,
    )
    wheel = next((work / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as wheel_archive:
        assert "bijux_ontology_reasoning/py.typed" in wheel_archive.namelist()
    run(
        ["uv", "pip", "install", "--offline", "--python", str(python), str(wheel)], work
    )
    run(["uv", "pip", "check", "--python", str(python)], work)
    return python, work, env


@pytest.fixture(scope="session")
def installed_oak(
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Expose the OAK integration without its incompatible native package extra."""
    return _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-oak"),
        extras=("oak",),
    )
