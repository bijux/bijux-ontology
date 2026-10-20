"""Installed project commands retain independently required application evidence."""

import subprocess
from pathlib import Path
from textwrap import dedent


def test_installed_shape_coverage_survives_portable_cli_and_parallel(
    installed_integrations: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, env = installed_integrations
    program = """
        import json
        from pathlib import Path
        import subprocess
        import sys
        from bijux_ontology.workflows.projects.bundles import load_bundle
        from bijux_ontology.workflows.projects.checking import check_project
        from bijux_ontology.workflows.projects.configuration import load_project
        from bijux_ontology.workflows.projects.parallel import checking as parallel
        root = Path("coverage-inputs").resolve()
        root.mkdir()
        (root / "data.ttl").write_text('<urn:record> <urn:title> "Title".')
        (root / "shapes.ttl").write_text(
            '@prefix sh:<http://www.w3.org/ns/shacl#>. <urn:Shape> a sh:NodeShape;'
            'sh:targetNode <urn:record>;sh:property '
            '[sh:path <urn:title>;sh:minCount 1].')
        config = {"version": 2, "rdf": {
            "data": {"path": "data.ttl", "iri": "urn:data"},
            "shapes": {"path": "shapes.ttl", "iri": "urn:shapes"}},
            "checks": [{"name": "records", "kind": "shacl", "document": "data",
                "shapes": "shapes", "coverage": {
                    "pairs": [["urn:Shape", "urn:absent"]]}}]}
        (root / "project.json").write_text(json.dumps(config))
        ordinary = check_project(load_project(root / "project.json"))
        assert not ordinary.verified
        relocated = load_bundle(root).materialize(root.parent / "portable-coverage")
        checked = parallel.check_project_parallel(
            relocated, artifacts=root.parent / "runs")
        codes = [i.code for i in ordinary.issues]
        assert [i.code for i in checked.report.issues] == codes
        command = subprocess.run([
            sys.executable, "-I", "-m", "bijux_ontology", "project",
            str(relocated.directory / "project.json"), "--json"],
            text=True, capture_output=True)
        assert command.returncode == 1, command.stdout + command.stderr
        assert "shacl.coverage_pair_missing" in command.stdout
    """
    result = subprocess.run(
        [str(python), "-I", "-c", dedent(program)],
        cwd=work,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
