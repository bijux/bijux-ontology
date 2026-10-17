"""Unordered RDF inventories have stable operational diagnostic order."""

import json
import os
import subprocess
import sys


def test_named_ontology_diagnostics_are_independent_of_python_hash_seed() -> None:
    program = """
import json
from rdflib import Graph, Namespace, Literal
from rdflib.namespace import RDF, RDFS, OWL
from bijux_ontology.workflows.ontology.checking import check_ontology
ex = Namespace("https://example.org/")
graph = Graph()
for number in range(10):
    graph.add((ex[f"Class{number}"], RDF.type, OWL.Class))
report = check_ontology(graph, families=("metadata",),
                        required_annotations=[str(RDFS.label)])
structural = Graph()
for number in range(8):
    node = ex[f"Property{number}"]
    structural.add((node, RDF.type, OWL.ObjectProperty))
    structural.add((node, RDF.type, OWL.DatatypeProperty))
    structural.add((node, RDFS.range, Literal("not a class")))
    structural.add((ex[f"Ontology{number}"], OWL.versionIRI, Literal("bad version")))
    structural.add((ex[f"Restriction{number}"], RDF.type, OWL.Restriction))
checks = check_ontology(
    structural, regime="owl-dl",
    families=("declarations", "headers", "references", "expressions"))
print(json.dumps({"metadata": [issue.location for issue in report.issues],
                  "structure": [(issue.code, issue.location, issue.actual)
                                for issue in checks.issues]}))
"""
    results = [
        subprocess.run(
            [sys.executable, "-c", program],
            check=True,
            capture_output=True,
            text=True,
            env=os.environ | {"PYTHONHASHSEED": seed},
            timeout=10,
        ).stdout
        for seed in ("1", "2")
    ]
    assert results[0] == results[1]
    assert len(json.loads(results[0])["metadata"]) == 10
    assert json.loads(results[0])["structure"]
