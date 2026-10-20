"""Build once from an sdist and install exact locked runtime variants."""

import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest


def _build_distribution(
    package: str, work: Path, env: dict[str, str]
) -> tuple[Path, Path, dict[str, str]]:
    """Build the installed wheel from that owner's unpacked source archive."""
    root = Path(__file__).resolve().parents[4]
    assert (root / "packages" / package / "LICENSE").read_bytes() == (
        root / "LICENSE"
    ).read_bytes()

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
        root / "packages" / package,
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
        unpacked,
    )
    return next((work / "dist").glob("*.whl")), unpacked, env


@pytest.fixture(scope="session")
def distribution(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Build the core independently of every adapter and reasoning distribution."""
    root = Path(__file__).resolve().parents[4]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("MYPYPATH", None)
    env["UV_CACHE_DIR"] = str(root / "artifacts/cache/uv")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return _build_distribution(
        "bijux-ontology", tmp_path_factory.mktemp("installed-package"), env
    )


@pytest.fixture(scope="session")
def adapters_distribution(
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Build the optional adapter wheel independently from its own sdist."""
    result = _build_distribution(
        "bijux-ontology-adapters",
        tmp_path_factory.mktemp("installed-adapters-package"),
        distribution[2],
    )
    with zipfile.ZipFile(result[0]) as wheel:
        names = wheel.namelist()
        assert "bijux_ontology_adapters/py.typed" in names
        assert "bijux_ontology_adapters/obo_graphs/schema.json" in names
        assert "bijux_ontology_adapters/rdfa/worker.cjs" in names
        assert "bijux_ontology_adapters/rdfa/node/package-lock.json" in names
        assert "bijux_ontology_adapters/java/pom.xml" in names
    return result


def _install_distribution(
    distribution: tuple[Path, Path, dict[str, str]],
    work: Path,
    *,
    extras: tuple[str, ...] = (),
    adapters: tuple[Path, Path, dict[str, str]] | None = None,
    adapter_extras: tuple[str, ...] = (),
) -> tuple[Path, Path, dict[str, str]]:
    """Install each exact local wheel with its own frozen dependency selection."""
    wheel, root, env = distribution

    def run(arguments: list[str], cwd: Path = root) -> None:
        subprocess.run(arguments, cwd=cwd, env=env, check=True, capture_output=True)

    selections = [("bijux-ontology", extras)]
    wheels = [wheel]
    if adapters is not None:
        selections.append(("bijux-ontology-adapters", adapter_extras))
        wheels.append(adapters[0])
    else:
        assert not adapter_extras
    constraints = work / "runtime-requirements.txt"
    # Resolve the selected packages together so exclusive extras retain one
    # consistent locked dependency graph across distribution boundaries.
    selected_extras = dict.fromkeys(
        extra for _, values in selections for extra in values
    )
    run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            *(argument for name, _ in selections for argument in ("--package", name)),
            "--no-emit-workspace",
            *(argument for extra in selected_extras for argument in ("--extra", extra)),
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
            *map(str, wheels),
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
def installed_adapters(
    distribution: tuple[Path, Path, dict[str, str]],
    adapters_distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Expose core and adapters without optional framework dependencies."""
    return _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-adapters"),
        adapters=adapters_distribution,
    )


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
    adapters_distribution: tuple[Path, Path, dict[str, str]],
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
        extras=tuple(optional),
        adapters=adapters_distribution,
        adapter_extras=tuple(
            extra
            for extra in tomllib.loads(
                (adapters_distribution[1] / "pyproject.toml").read_text()
            )["project"]["optional-dependencies"]
            if extra != "frictionless"
        ),
    )


@pytest.fixture(scope="session")
def installed_frictionless(
    adapters_distribution: tuple[Path, Path, dict[str, str]],
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Qualify native packages without OAK's conflicting character detector."""
    return _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-frictionless"),
        adapters=adapters_distribution,
        adapter_extras=("frictionless",),
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
    adapters_distribution: tuple[Path, Path, dict[str, str]],
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Expose the OAK integration without its incompatible native package extra."""
    return _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-oak"),
        adapters=adapters_distribution,
        adapter_extras=("oak",),
    )


@pytest.fixture(scope="session", params=("baseline", "alternative"))
def installed_native_versions(
    adapters_distribution: tuple[Path, Path, dict[str, str]],
    request: pytest.FixtureRequest,
    distribution: tuple[Path, Path, dict[str, str]],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, dict[str, str]]:
    """Install each declared API boundary with genuine resolved native dependencies."""
    result = _install_distribution(
        distribution,
        tmp_path_factory.mktemp("installed-native-" + request.param),
        extras=("analysis", "shex"),
        adapters=adapters_distribution,
        adapter_extras=("arrow", "biolink"),
    )
    python, work, environment = result
    releases = (
        (
            "scipy==1.17.0",
            "pyarrow==25.0.1",
            "bmt==1.5.0",
            "linkml-runtime==1.12.0",
            "pyrudof==0.3.21",
        )
        if request.param == "alternative"
        else ()
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "pytest==" + version("pytest"),
            "pytest-timeout==" + version("pytest-timeout"),
            *releases,
        ],
        cwd=work,
        env=environment,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["uv", "pip", "check", "--python", str(python)],
        cwd=work,
        env=environment,
        check=True,
        capture_output=True,
    )
    return result
