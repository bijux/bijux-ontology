"""Exercise the real non-editable wheel through isolated API and CLI callers."""

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_installed_api_and_independent_example(
    installed: tuple[Path, Path, dict[str, str]],
    distribution: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, env = installed
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import json, bijux_ontology; from pathlib import Path; "
            "from rdflib import Graph; "
            "from bijux_ontology.interfaces import api; "
            "assert bijux_ontology.__all__ == api.__all__; "
            "assert all(getattr(bijux_ontology, name) is getattr(api, name) "
            "for name in api.__all__); "
            "g = Graph().parse('ontology.ttl', format='turtle'); "
            "r = bijux_ontology.check_vocabulary(g, "
            "[bijux_ontology.TermDeclaration('https://example.org/Image')]); "
            "required = bijux_ontology.check_contract(["
            "bijux_ontology.ContractObligation('vocabulary', lambda: r)]); "
            "assert required.accepted and required.require_verified().verified; "
            "assert Path(bijux_ontology.__file__).is_relative_to("
            "Path('env').resolve()); "
            "print(json.dumps(r.to_dict()))",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["verified"] is True
    _, source, _ = distribution
    for example in sorted((source / "examples").glob("*.py")):
        subprocess.run(
            [str(python), "-I", str(example)],
            cwd=work,
            env=env,
            check=True,
            capture_output=True,
        )


@pytest.mark.parametrize(
    ("inventory", "expected_code"),
    [
        (json.dumps(["https://example.org/Image"]), 0),
        (json.dumps(["https://example.org/Missing"]), 1),
        ("{", 2),
        ("[" * 1100 + "0" + "]" * 1100, 2),
        ("[" + "9" * 5000 + "]", 2),
    ],
)
def test_installed_cli_vocabulary_outcomes(
    installed: tuple[Path, Path, dict[str, str]],
    inventory: str,
    expected_code: int,
) -> None:
    python, work, env = installed
    (work / "inventory.json").write_text(inventory, encoding="utf-8")
    result = subprocess.run(
        [
            str(
                python.parent
                / ("bijux-ontology.exe" if os.name == "nt" else "bijux-ontology")
            ),
            "vocabulary",
            "ontology.ttl",
            "inventory.json",
            "--json",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_code, result.stderr
    report = json.loads(result.stdout)
    assert report["exit_code"] == expected_code
    if expected_code:
        assert report["issues"]


@pytest.mark.parametrize("action", ["models", "context"])
def test_installed_cli_routes_real_model_and_context_checks(
    installed: tuple[Path, Path, dict[str, str]],
    action: str,
) -> None:
    python, work, env = installed
    if action == "models":
        (work / "declarations.json").write_text(
            json.dumps(
                [{"identifier": "image", "types": ["https://example.org/Image"]}]
            ),
            encoding="utf-8",
        )
        inputs = ["ontology.ttl", "declarations.json"]
    else:
        (work / "document.json").write_text(
            json.dumps(
                {"@context": {"name": "https://example.org/name"}, "name": "image"}
            ),
            encoding="utf-8",
        )
        (work / "expectations.json").write_text(
            json.dumps(
                [
                    {
                        "path": ["name"],
                        "predicate": "https://example.org/name",
                        "value_kind": "literal",
                    }
                ]
            ),
            encoding="utf-8",
        )
        inputs = ["document.json", "expectations.json"]
    result = subprocess.run(
        [
            str(
                python.parent
                / ("bijux-ontology.exe" if os.name == "nt" else "bijux-ontology")
            ),
            action,
            *inputs,
            "--json",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout)["verified"] is True


def test_installed_module_inspection_and_upgrade_work_without_extras(
    installed: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, env = installed
    inventory = work / "upgrade-inventory.json"
    inventory.write_text(json.dumps(["https://example.org/Image"]), encoding="utf-8")
    after = work / "after.ttl"
    before = work / "ontology.ttl"
    after.write_bytes(before.read_bytes())

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(python), "-I", "-m", "bijux_ontology", *arguments, "--json"],
            cwd=work,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    help_result = run("project", "--help")
    assert help_result.returncode == 0, help_result.stderr
    start = help_result.stdout.index('{"version"')
    configuration, _ = json.JSONDecoder().raw_decode(help_result.stdout[start:])
    project = work / "help-checks.json"
    project.write_text(json.dumps(configuration), encoding="utf-8")
    checked = run("project", str(project))
    assert checked.returncode == 0, checked.stderr or checked.stdout
    assert json.loads(checked.stdout)["verified"]
    inspected = run("inspect", str(before), "--term", "https://example.org/Image")
    assert inspected.returncode == 0, inspected.stderr
    assert "https://example.org/Image" in inspected.stdout
    args = ("upgrade", str(before), str(after), "--inventory", str(inventory))
    stable = run(*args)
    assert stable.returncode == 0, stable.stderr or stable.stdout
    assert json.loads(stable.stdout)["data"]["contract_verdict"] == "agreement"
    after.write_text(
        "<https://example.org/Other> a <http://www.w3.org/2002/07/owl#Class> .",
        encoding="utf-8",
    )
    changed = run(*args)
    assert changed.returncode == 1, changed.stderr or changed.stdout
    result = json.loads(changed.stdout)
    assert result["data"]["contract_verdict"] == "demonstrated-break"
    assert result["data"]["affected"]
