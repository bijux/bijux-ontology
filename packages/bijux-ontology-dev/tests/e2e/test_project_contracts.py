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
        from bijux_ontology.execution.limits import Limits
        from bijux_ontology.workflows.projects.parallel import checking as parallel
        root = Path("coverage-inputs").resolve()
        root.mkdir()
        (root / "data.ttl").write_text('<urn:record> <urn:title> "Title".')
        (root / "shapes.ttl").write_text(
            '@prefix sh:<http://www.w3.org/ns/shacl#>. <urn:Shape> a sh:NodeShape;'
            'sh:targetNode <urn:record>;sh:property '
            '[sh:path <urn:title>;sh:minCount 1].')
        config = {"version": 2, "limits": {
            "max_work": 10000000, "max_states": 1000000,
            "max_total_bytes": 200000000}, "rdf": {
            "data": {"path": "data.ttl", "iri": "urn:data"},
            "shapes": {"path": "shapes.ttl", "iri": "urn:shapes"}},
            "checks": [{"name": "records", "kind": "shacl", "document": "data",
                "shapes": "shapes", "coverage": {
                    "pairs": [["urn:Shape", "urn:absent"]]}}]}
        (root / "vocabulary.ttl").write_text(
            '<urn:Record> a <http://www.w3.org/2002/07/owl#Class>.'
            '<urn:title> a <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property>.')
        (root / "data.ttl").write_text(
            '<urn:record> a <urn:Record>; <urn:title> "Title".')
        models = [{"identifier": "Record", "types": ["urn:Record"], "fields": [{
            "name": "title", "predicate": "urn:title", "emission": "guaranteed",
            "values": [{"kind": "literal",
                        "datatype": "http://www.w3.org/2001/XMLSchema#string"}]}]}]
        (root / "models.json").write_text(json.dumps(models))
        (root / "output.json").write_text(json.dumps({
            "@context": {"title": "urn:title"}, "@type": "urn:Record",
            "title": "Title"}))
        (root / "usages.json").write_text('[{"model":"Record","path":[]}]')
        (root / "roots.json").write_text(
            '[{"model":"Record","subject":"urn:record"}]')
        (root / "expectations.json").write_text(
            '[{"path":["title"],"predicate":"urn:title"}]')
        config["rdf"]["vocabulary"] = {
            "path": "vocabulary.ttl", "iri": "urn:vocabulary"}
        config["checks"] = [
            {"name": "models", "kind": "models", "source": "vocabulary",
             "declarations": "models.json"},
            {"name": "context", "kind": "context", "document": "output.json",
             "expectations": "expectations.json"},
            {"name": "output", "kind": "emission", "source": "vocabulary",
             "document": "output.json", "declarations": "models.json",
             "usages": "usages.json", "depends_on": ["models"]},
            {"name": "native", "kind": "rdf-emission", "document": "data",
             "declarations": "models.json", "usages": "roots.json",
             "depends_on": ["models"]},
            *config["checks"]]
        (root / "project.json").write_text(json.dumps(config))
        ordinary = check_project(load_project(root / "project.json"))
        assert not ordinary.verified
        relocated = load_bundle(root, limits=Limits(**config["limits"])).materialize(
            root.parent / "portable-coverage")
        checked = parallel.check_project_parallel(
            relocated, artifacts=root.parent / "runs")
        codes = [i.code for i in ordinary.issues]
        assert [i.code for i in checked.report.issues] == codes, (
            checked.report.to_dict())
        command = subprocess.run([
            sys.executable, "-I", "-m", "bijux_ontology", "project",
            str(relocated.directory / "project.json"), "--json",
            "--execution", "parallel", "--workers", "3",
            "--execution-artifacts", str(root.parent / "cli-runs")],
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


def test_installed_core_project_checks_native_rdf_without_optional_consumers(
    installed: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, env = installed
    program = """
        import json
        from pathlib import Path
        import subprocess
        import sys
        import importlib.util
        assert importlib.util.find_spec("pyld") is None
        assert importlib.util.find_spec("ckan") is None
        root = Path("rdf-project").resolve()
        root.mkdir()
        (root / "output.ttl").write_text(
            '<urn:record> a <urn:Record>; <urn:title> "Produced".')
        models = [{"identifier": "Record", "types": ["urn:Record"], "fields": [{
            "name": "title", "predicate": "urn:title", "emission": "guaranteed",
            "values": [{"kind": "literal",
                        "datatype": "http://www.w3.org/2001/XMLSchema#string"}]}]}]
        (root / "models.json").write_text(json.dumps(models))
        (root / "bindings.json").write_text(json.dumps([
            {"model": "Record", "subject": "urn:record"}]))
        (root / "project.json").write_text(json.dumps({
            "version": 2, "rdf": {"output": {"path": "output.ttl"}},
            "checks": [{"name": "records", "kind": "rdf-emission",
                "document": "output", "declarations": "models.json",
                "usages": "bindings.json"}]}))
        command = [sys.executable, "-I", "-m", "bijux_ontology", "project",
                   str(root / "project.json"), "--json"]
        result = subprocess.run(command, text=True, capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
        (root / "output.ttl").write_text('<urn:record> a <urn:Record>.')
        result = subprocess.run(command, text=True, capture_output=True)
        assert result.returncode == 1, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert not report["verified"]
        assert any(i["location"] == "usages/0.title" for i in report["issues"])
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
