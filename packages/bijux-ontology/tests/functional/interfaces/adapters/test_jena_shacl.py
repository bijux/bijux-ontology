"""Actual optional Jena Core/SELECT scope, standards findings and Python parity."""

import hashlib
import json
import os
import shutil
from pathlib import Path
from threading import Event

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.jena import (
    JenaBackend,
    prepare_jena_backend,
)
from bijux_ontology.interfaces.adapters.jena.shacl import validate_jena_shapes
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.shacl.validation.contracts import ShaclConstraintRequirement

EX = Namespace("urn:jena:")
PREFIX = """@prefix ex:<urn:jena:> . @prefix sh:<http://www.w3.org/ns/shacl#> .
@prefix xsd:<http://www.w3.org/2001/XMLSchema#> . """
SHAPES = """ex:S a sh:NodeShape;sh:targetClass ex:Node;
sh:property [sh:path (ex:p ex:q);sh:minCount 1;sh:datatype xsd:string];
sh:sparql [sh:select "PREFIX ex:<urn:jena:> SELECT $this WHERE {
$this ex:age ?v FILTER(?v < 0)}"].""".replace("\n", " ")


def graph(text: str) -> Graph:
    """Use actual maintained parsing for compact supplied fixtures."""
    return Graph().parse(data=PREFIX + text, format="turtle")


@pytest.fixture
def backend(tmp_path: Path) -> JenaBackend:
    """Require an explicit local pinned Jena bundle and Java executable."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in (
            "BIJUX_JENA_JAVA",
            "BIJUX_JENA_BUNDLE",
            "BIJUX_JENA_MANIFEST_SHA256",
        )
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned optional Jena distribution is required")
    return prepare_jena_backend(
        Path(java),
        Path(bundle),
        manifest_sha256=pin,
        artifacts=tmp_path / "native",
    )


def test_actual_core_select_findings_and_selected_python_parity(
    backend: JenaBackend,
) -> None:
    """Both actual engines agree on these supplied positive/negative operations."""
    pytest.importorskip("pyshacl")
    shapes = graph(SHAPES)
    for text, expected in (
        ('ex:a a ex:Node;ex:p [ex:q "ok"];ex:age 2.', True),
        ("ex:a a ex:Node;ex:p [ex:q 7];ex:age -1.", False),
    ):
        data = graph(text)
        original_data, original_shapes = set(data), set(shapes)
        native = validate_jena_shapes(backend, data, shapes, trusted_sparql=True)
        python = validate_shapes(data, shapes, trusted_sparql=True)
        assert native.conforms is python.conforms is expected
        assert native.report.complete and python.report.complete
        assert native.backend.startswith("jena 6.2.0 / Java ")
        assert native.report.verified is expected
        if not expected:
            assert len(native.root_findings) == 2
            assert {finding.constraint_component for finding in native.findings} == {
                SH.DatatypeConstraintComponent,
                SH.SPARQLConstraintComponent,
            }
            assert any(
                finding.path_expression and finding.path_expression.kind == "sequence"
                for finding in native.findings
            )
            assert any(finding.value == Literal(7) for finding in native.findings)
            assert (None, RDF.type, SH.ValidationReport) in native.report_graph()
        assert set(data) == original_data and set(shapes) == original_shapes


def test_class_targets_focus_and_unrelated_failure_do_not_leak(
    backend: JenaBackend,
) -> None:
    """Selected roots preserve nested constraints while unrelated targets stay out."""
    shapes = graph(
        SHAPES + "ex:Other a sh:NodeShape;sh:targetClass ex:OtherClass;"
        "sh:property [sh:path ex:missing;sh:minCount 1]."
    )
    data = graph('ex:a a ex:Node;ex:p [ex:q "ok"];ex:age 2. ex:b a ex:OtherClass.')
    full = validate_jena_shapes(backend, data, shapes, trusted_sparql=True)
    selected = validate_jena_shapes(
        backend,
        data,
        shapes,
        use_shapes=(EX.S,),
        trusted_sparql=True,
        coverage=ShaclCoverageContract(pairs=((str(EX.S), str(EX.a)),)),
    )
    focused = validate_jena_shapes(
        backend,
        data,
        shapes,
        focus_nodes=(EX.a,),
        trusted_sparql=True,
    )
    assert full.conforms is False
    assert selected.report.verified and focused.report.verified
    assert selected.coverage and selected.coverage.report.verified
    assert {(item.shape, item.focus_node) for item in selected.coverage.targets} == {
        (EX.S, EX.a),
    }


def test_required_constraint_coverage_and_targetless_truth_are_separate(
    backend: JenaBackend,
) -> None:
    """Completed root execution is required independently from raw conformance."""
    shapes = graph(
        "ex:P a sh:PropertyShape;sh:path ex:p;sh:minCount 1;sh:targetNode ex:a."
    )
    data = graph('ex:a ex:p "ok".')
    requirement = ShaclConstraintRequirement(
        str(EX.P),
        str(SH.minCount),
        str(SH.MinCountConstraintComponent),
    )
    complete = validate_jena_shapes(
        backend,
        data,
        shapes,
        required_constraints=(requirement,),
        coverage=ShaclCoverageContract(minimum_focus=1),
    )
    assert complete.report.verified and complete.constraints
    assert complete.constraints.verified
    shapes.remove((EX.P, SH.targetNode, None))
    empty = validate_jena_shapes(
        backend, data, shapes, required_constraints=(requirement,)
    )
    assert empty.conforms is True and not empty.report.verified
    assert empty.coverage and not empty.coverage.targets


def test_missing_functions_remote_queries_and_backend_gaps_remain_unknown(
    backend: JenaBackend,
) -> None:
    """Trust cannot turn missing functions or unsupported work into conformance."""
    data = graph('ex:a ex:p "ok".')
    for query in (
        "SELECT $this WHERE {FILTER(<urn:unavailable-function>($this))}",
        "SELECT $this WHERE {SERVICE <http://example.invalid/> {$this ?p ?o}}",
        "SELECT $this FROM <http://example.invalid/> WHERE {$this ?p ?o}",
    ):
        shapes = graph(
            "ex:S a sh:NodeShape;sh:targetNode ex:a;sh:sparql [sh:select "
            + Literal(query).n3()
            + "]."
        )
        result = validate_jena_shapes(backend, data, shapes, trusted_sparql=True)
        assert result.conforms is None and result.report.outcome == "unverified"
    opaque = graph(
        "ex:P a sh:PropertyShape;sh:targetNode ex:a;sh:path ex:p;"
        "sh:datatype <urn:opaque:datatype>."
    )
    opaque_data = Graph().add(
        (
            EX.a,
            EX.p,
            Literal("any", datatype=URIRef("urn:opaque:datatype")),
        )
    )
    unsupported = validate_jena_shapes(backend, opaque_data, opaque)
    assert unsupported.conforms is None and unsupported.report.outcome == "unverified"
    untrusted = validate_jena_shapes(backend, data, graph(SHAPES))
    assert untrusted.conforms is None and not untrusted.report.verified
    preflight = validate_jena_shapes(backend, data, graph(SHAPES), preflight=True)
    assert preflight.conforms is None and preflight.report.outcome == "unverified"
    empty = validate_jena_shapes(backend, data, Graph())
    assert empty.conforms is True and empty.report.outcome == "input_error"


def test_owned_blank_pairs_physical_graph_selection_and_stops(
    backend: JenaBackend,
) -> None:
    """Foreign equal blank labels and unrelated physical graphs establish no scope."""
    source = prepare_source(Graph().add((BNode("same"), EX.p, Literal("ok"))))
    blank = next(source.graph().subjects())
    assert isinstance(blank, BNode)
    shapes = graph("ex:P a sh:PropertyShape;sh:path ex:p;sh:minCount 1.")
    paired = validate_jena_shapes(
        backend,
        source,
        shapes,
        mode="pairs",
        focus_nodes=(blank,),
        use_shapes=(EX.P,),
    )
    assert paired.report.verified and paired.focus_nodes == (blank,)
    foreign = validate_jena_shapes(
        backend,
        source,
        shapes,
        mode="pairs",
        focus_nodes=(BNode(str(blank)),),
        use_shapes=(EX.P,),
    )
    assert foreign.report.outcome == "input_error" and foreign.conforms is None
    dataset = Dataset()
    dataset.graph(EX.G).add((EX.a, EX.p, Literal("ok")))
    dataset.graph(EX.H).add((EX.a, EX.p, Literal(7)))
    selected = prepare_source(RdfSource(dataset, graph_name=str(EX.G)))
    exact = graph(
        "ex:S a sh:NodeShape;sh:targetNode ex:a;"
        "sh:property [sh:path ex:p;sh:datatype xsd:string]."
    )
    assert validate_jena_shapes(backend, selected, exact).report.verified
    budget = Budget(Limits(max_total_bytes=4096))
    exhausted = validate_jena_shapes(backend, selected, exact, budget=budget)
    assert exhausted.conforms is None and exhausted.report.outcome == "exhausted"
    cancel = Event()
    cancel.set()
    stopped = validate_jena_shapes(backend, selected, exact, cancel=cancel)
    assert stopped.conforms is None and stopped.report.outcome == "exhausted"


def test_pinned_classpath_entries_cannot_expand_beyond_sealed_jars(
    backend: JenaBackend, tmp_path: Path
) -> None:
    """Actual Java separators cannot split one sealed component into several."""
    for kind in ("jar", "directory"):
        directory = tmp_path / (
            "directory" + os.pathsep if kind == "directory" else kind
        )
        directory.mkdir()
        manifest = json.loads((backend.directory / "manifest.json").read_text())
        for index, entry in enumerate(manifest["jars"]):
            original = entry["name"]
            if kind == "jar" and index == 0:
                entry["name"] = "sealed" + os.pathsep + original
            shutil.copyfile(backend.directory / original, directory / entry["name"])
        raw = json.dumps(manifest).encode()
        (directory / "manifest.json").write_bytes(raw)
        with pytest.raises(ResourceError, match="classpath|artifact content"):
            prepare_jena_backend(
                backend.java,
                directory,
                manifest_sha256=hashlib.sha256(raw).hexdigest(),
                artifacts=tmp_path / "native",
            )
