"""Generated Python preserves exact RDF declarations and explicit naming intent."""

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.interfaces.adapters.vocabulary_bindings import (
    generate_vocabulary_bindings,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary

EX = Namespace("https://example.org/")


def _import(source: str, directory: Path) -> dict[str, object]:
    path = directory / "consumer_terms.py"
    path.write_text(source)
    script = """
import importlib.util, json, sys
from bijux_ontology.interfaces.adapters.namespaces import namespace_inventory
spec = importlib.util.spec_from_file_location("consumer_terms", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
inventory = namespace_inventory({}, registries={"consumer": module.INVENTORY})
try:
    module.TERMS["Unknown"]
    raise AssertionError("Unknown name expanded to an IRI")
except KeyError:
    pass
assert not hasattr(module, "Unknown")
try:
    module.TERMS["injected"] = "https://example.org/injected"
    raise AssertionError("Mutable registry")
except TypeError:
    pass
print(json.dumps({
    term.location: [term.iri, list(term.kinds)]
    for term in inventory.declarations
}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(path)],
        cwd=directory,
        text=True,
        capture_output=True,
        check=True,
    )
    return cast(dict[str, object], json.loads(result.stdout))


def test_mixed_declarations_exact_inventory_and_safe_documentation(
    tmp_path: Path,
) -> None:
    quoted = EX["quoted'name"]
    graph = Graph()
    for iri, kind in (
        (EX.Thing, OWL.Class),
        (EX.related, OWL.ObjectProperty),
        (quoted, SKOS.Concept),
    ):
        graph.add((iri, RDF.type, kind))
    graph.add(
        (quoted, RDFS.label, Literal("quote '\" and newline\n__import__('os').abort()"))
    )
    result = generate_vocabulary_bindings(
        inspect_vocabulary(graph),
        [str(EX.Thing), str(EX.related), str(quoted)],
        names={str(quoted): "Concept"},
        aliases={"ThingAlias": str(EX.Thing)},
        annotations=[str(RDFS.label)],
    )
    assert result.report.verified and result.source is not None
    assert _import(result.source, tmp_path) == {
        "consumer.Thing": [str(EX.Thing), [str(OWL.Class)]],
        "consumer.ThingAlias": [str(EX.Thing), [str(OWL.Class)]],
        "consumer.related": [str(EX.related), [str(OWL.ObjectProperty)]],
        "consumer.Concept": [str(quoted), [str(SKOS.Concept)]],
    }
    assert "newline" in result.bindings[-1].documentation[0]


def test_names_collisions_keywords_reserved_and_python_normalization(
    tmp_path: Path,
) -> None:
    iris = [
        str(EX[name]) for name in ("a-b", "a_b", "class", "1name", "K", "K", "TERMS")
    ]
    graph = Graph()
    for iri in iris:
        graph.add((URIRef(iri), RDF.type, OWL.Class))
    view = inspect_vocabulary(graph)
    result = generate_vocabulary_bindings(view, iris, invalid_names="normalize")
    assert result.report.outcome == "input_error" and not result.report.verified
    assert [(binding.name, binding.iri) for binding in result.bindings] == [
        ("class_", iris[2]),
        ("_1name", iris[3]),
    ]
    assert result.source is not None
    assert set(_import(result.source, tmp_path)) == {
        "consumer.class_",
        "consumer._1name",
    }
    assert len(result.report.issues) == 5
    strict = generate_vocabulary_bindings(view, [iris[2]])
    assert strict.source is not None and not strict.bindings
    assert _import(strict.source, tmp_path) == {}
    reserved = generate_vocabulary_bindings(
        view, [iris[0]], names={iris[0]: "MyTerm"}, reserved=["MyTerm"]
    )
    assert not reserved.report.verified and not reserved.bindings


def test_selected_membership_invalid_overrides_and_exhaustion_remain_honest() -> None:
    graph = Graph().add((EX.Valid, RDF.type, OWL.Class))
    view = inspect_vocabulary(graph)
    absent = generate_vocabulary_bindings(view, [str(EX.Unknown)])
    assert absent.report.outcome == "input_error" and not absent.bindings
    bad = generate_vocabulary_bindings(
        view, [str(EX.Valid)], aliases={"Alias": str(EX.Unknown)}
    )
    assert bad.source is None and not bad.report.verified
    empty = generate_vocabulary_bindings(view, [])
    assert not empty.report.verified
    stopped = generate_vocabulary_bindings(
        view, [str(EX.Valid)], limits=Limits(max_bytes=30)
    )
    assert stopped.source is None and stopped.report.outcome == "exhausted"
    finding_then_stop = generate_vocabulary_bindings(
        view,
        [str(EX.Unknown), str(EX.Valid)],
        limits=Limits(max_work=200),
    )
    assert not finding_then_stop.report.verified
    assert any(
        issue.code == "vocabulary.binding_name_unresolved"
        for issue in finding_then_stop.report.issues
    )
    assert any(
        issue.code == "execution.limit_exceeded"
        for issue in finding_then_stop.report.issues
    )

    with pytest.raises(InputError, match="conflict"):
        generate_vocabulary_bindings(
            view,
            [str(EX.Valid)],
            limits=Limits(max_bytes=30),
            budget=Budget(),
        )
