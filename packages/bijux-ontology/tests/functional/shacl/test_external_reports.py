"""External native reports retain RDF findings without inventing local proof."""

from pathlib import Path

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, BudgetKind, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.external_reports import (
    ExternalShaclReport,
    ExternalShaclScope,
    aggregate_shacl_reports,
    ingest_external_report,
)

EX = Namespace("https://example.org/external/")
CLAIMS = ExternalShaclScope(
    "urn:data:release", "urn:shapes:release", "targets;inference=none"
)


def failed_report() -> Graph:
    """A standards report with exact lexical numeric evidence and an extension."""
    graph = Graph()
    graph.add((EX.Report, RDF.type, SH.ValidationReport))
    graph.add((EX.Report, SH.conforms, Literal(False)))
    graph.add((EX.Report, SH.result, EX.Result))
    for predicate, value in (
        (RDF.type, SH.ValidationResult),
        (SH.focusNode, EX.C),
        (SH.resultPath, EX.p),
        (SH.value, Literal("01", datatype=XSD.integer, normalize=False)),
        (SH.resultSeverity, SH.Violation),
        (SH.sourceConstraintComponent, SH.DatatypeConstraintComponent),
        (SH.sourceShape, EX.S),
        (SH.resultMessage, Literal("Wrong value", lang="en")),
        (EX.extension, Literal("retained")),
    ):
        graph.add((EX.Result, predicate, value))
    return graph


def positive_report() -> Graph:
    """No result edges when the external producer asserts conformance."""
    return (
        Graph()
        .add((EX.Report, RDF.type, SH.ValidationReport))
        .add((EX.Report, SH.conforms, Literal(True)))
    )


def test_independent_native_nested_report_and_local_aggregate() -> None:
    pyshacl = pytest.importorskip("pyshacl")
    data = Graph().parse(
        data="@prefix e:<https://example.org/external/>. e:C e:p e:D. "
        'e:D e:q e:E. e:E e:r "bad".',
        format="turtle",
    )
    shapes = Graph().parse(
        data="@prefix e:<https://example.org/external/>. "
        "@prefix sh:<http://www.w3.org/ns/shacl#>. "
        "@prefix x:<http://www.w3.org/2001/XMLSchema#>. "
        "e:S a sh:NodeShape; sh:targetNode e:C; sh:property ["
        "sh:path (e:p e:q); sh:node [sh:property [sh:path e:r; "
        "sh:datatype x:integer]]].",
        format="turtle",
    )
    conforms, report_graph, _ = pyshacl.validate(
        data, shacl_graph=shapes, inference="none"
    )
    assert not conforms and isinstance(report_graph, Graph)
    imported = ingest_external_report(report_graph, scope=CLAIMS, backend="PySHACL")
    assert imported.structural_report.verified and imported.conforms is False
    assert imported.findings_complete and len(imported.findings) >= 2
    root = imported.root_findings[0]
    assert root.focus_node == EX.C and root.value == EX.E
    assert root.path_expression is not None and root.path_expression.kind == "sequence"
    assert root.constraint_component == SH.NodeConstraintComponent
    assert (
        root.details
        and root.details[0].constraint_component == SH.DatatypeConstraintComponent
    )
    assert root.details[0].value == Literal("bad")
    assert not imported.report.verified
    healthy_data = data - Graph().add((EX.E, EX.r, Literal("bad")))
    healthy_data.add((EX.E, EX.r, Literal(1)))
    healthy_local = validate_shapes(
        prepare_rdf(healthy_data), prepare_rdf(shapes), preflight=True
    )
    assert healthy_local.report.verified
    external_positive = ingest_external_report(
        positive_report(), scope=CLAIMS, backend="PySHACL"
    )
    mixed_positive = aggregate_shacl_reports((external_positive, healthy_local))
    assert mixed_positive.report.outcome == "unverified"
    local = validate_shapes(prepare_rdf(data), prepare_rdf(shapes), preflight=True)
    assert local.conforms is False
    aggregate = aggregate_shacl_reports((imported, local))
    assert aggregate.results == (imported, local) and not aggregate.report.verified
    assert {i.code for i in aggregate.report.issues} >= {
        "shacl.external_nonconformance",
        "shacl.nonconformance",
        "shacl.external_proof_unverified",
    }


def test_scope_assertions_and_separate_blank_ownership() -> None:
    # Independent raw graphs with identical blank spellings remain distinct scopes.
    raw = Graph().parse(
        data="""
        @prefix sh:<http://www.w3.org/ns/shacl#>.
        @prefix e:<https://example.org/external/>.
        _:report a sh:ValidationReport; sh:conforms false; sh:result _:result.
        _:result a sh:ValidationResult; sh:focusNode _:focus;
          sh:resultSeverity sh:Violation;
          sh:sourceConstraintComponent sh:MinCountConstraintComponent.
    """,
        format="turtle",
    )
    a, b = ingest_external_report(raw), ingest_external_report(raw)
    assert a.findings[0].result_node != b.findings[0].result_node
    assert a.findings[0].focus_node != b.findings[0].focus_node
    aggregate = aggregate_shacl_reports((a, b))
    assert len(aggregate.results) == 2 and len(aggregate.report.children) == 2
    assert not aggregate.report.verified
    positive = ingest_external_report(
        positive_report(), scope=CLAIMS, backend="claimed-engine"
    )
    assert positive.conforms is True and positive.structural_report.verified
    assert positive.report.outcome == "unverified"
    assert "shacl.external_scope_missing" not in {
        i.code for i in positive.report.issues
    }
    missing = ingest_external_report(positive_report())
    assert "shacl.external_scope_missing" in {i.code for i in missing.report.issues}
    assert aggregate_shacl_reports(()).report.outcome == "unverified"
    with pytest.raises(InputError):
        ExternalShaclReport()


def test_structure_contradictions_and_singletons_never_qualify(tmp_path: Path) -> None:
    good = failed_report()
    variants = [
        good + Graph().add((EX.Report, SH.conforms, Literal(True))),
        good + Graph().add((EX.Result, SH.focusNode, EX.Other)),
        good + Graph().add((EX.Result, SH.value, Literal("1", datatype=XSD.integer))),
        good - Graph().add((EX.Result, SH.resultSeverity, SH.Violation)),
        good + Graph().add((EX.Result, SH.sourceConstraintComponent, Literal("wrong"))),
    ]
    for graph in variants:
        result = ingest_external_report(graph)
        assert not result.structural_report.verified and not result.report.verified
        assert result.report.outcome == "input_error"
        assert len(result.report_graph()) == len(graph)
    path = tmp_path / "report.ttl"
    path.write_text(good.serialize(format="turtle"))
    result = ingest_external_report(path)
    assert result.structural_report.verified
    assert str(result.findings[0].value) == "01"
    assert [
        str(value) for value in result.report_graph().objects(EX.Result, EX.extension)
    ] == ["retained"]
    good.remove((None, None, None))
    assert result.report_graph()
    result.report_graph().remove((None, None, None))
    assert result.report_graph()


def test_cycles_unknown_extensions_and_selected_physical_graph() -> None:
    graph = failed_report()
    graph.add((EX.Result, SH.detail, EX.Result))
    cycle = ingest_external_report(graph)
    assert cycle.structural_report.verified and not cycle.findings_complete
    assert not cycle.report.verified
    abstract = failed_report()
    abstract.add((EX.Result, SH.detail, EX.Detail))
    abstract.add((EX.Detail, RDF.type, SH.AbstractResult))
    abstract.add((EX.Detail, EX.extension, Literal("opaque detail")))
    detail = ingest_external_report(abstract)
    assert detail.structural_report.verified
    assert detail.root_findings[0].detail_nodes == (EX.Detail,)
    assert len([i for i in detail.report.issues if i.kind == "finding"]) == 1
    abstract.remove((EX.Detail, RDF.type, SH.AbstractResult))
    extension = ingest_external_report(abstract)
    assert extension.report.outcome == "disagreement"
    assert not extension.structural_report.verified
    assert extension.root_findings[0].detail_nodes == (EX.Detail,)
    graph = failed_report()
    graph.remove((EX.Result, SH.resultPath, EX.p))
    graph.add((EX.Result, SH.resultPath, BNode("path")))
    malformed = ingest_external_report(graph)
    assert not malformed.findings_complete and not malformed.report.verified
    dataset = Dataset()
    for triple in failed_report():
        dataset.graph(EX.G).add(triple)
    for triple in positive_report():
        dataset.graph(EX.H).add(triple)
    selected = ingest_external_report(RdfSource(dataset, graph_name=str(EX.G)))
    assert selected.structural_report.verified and selected.conforms is False
    assert len(selected.report_graph()) == len(failed_report())


def test_shared_budget_paid_prefix_and_missing_native_backend() -> None:
    graph = failed_report()
    tiny = ingest_external_report(positive_report(), limits=Limits(max_work=1))
    assert tiny.report.outcome == "exhausted"
    assert "input.empty_scope" not in {i.code for i in tiny.report.issues}
    baseline = Budget()
    result = ingest_external_report(graph, budget=baseline)
    assert result.report.outcome == "disagreement"
    paid_late = 0
    kinds: tuple[tuple[BudgetKind, str], ...] = (
        ("work", "max_work"),
        ("states", "max_states"),
        ("bytes", "max_total_bytes"),
    )
    for kind, field in kinds:
        total = baseline._counts[kind]
        for cap in sorted({1, total, *range(max(1, total - 8), total)}):
            budget = Budget(Limits(**{field: cap}))
            result = ingest_external_report(graph, budget=budget)
            paid = {
                id(i) for i in budget._retained_issues.values() if i.kind == "finding"
            }
            assert paid <= {id(i) for i in result.report.issues}
            paid_late += bool(paid and result.report.outcome == "exhausted")
    assert paid_late
    budget = Budget(Limits(max_issues=1))
    result = ingest_external_report(graph, budget=budget)
    assert result.report.outcome == "exhausted"
    assert any(i.code == "shacl.external_nonconformance" for i in result.report.issues)
    # Canonical generic Issue evidence uses ASCII escapes, including astral text.
    large = failed_report()
    large.add((EX.Report, SH.result, EX.Z))
    for _, predicate, value in tuple(large.triples((EX.Result, None, None))):
        large.add((EX.Z, predicate, value))
    large.set((EX.Z, SH.resultMessage, Literal("😀" * 2000)))
    bounded = ingest_external_report(large, limits=Limits(max_bytes=20_000))
    assert bounded.report.outcome == "exhausted"
    assert any(i.code == "shacl.external_nonconformance" for i in bounded.report.issues)
    assert all(len(i.evidence_json or "") <= 20_000 for i in bounded.report.issues)
    # Ingestion itself has no maintained SHACL runtime dependency.
    assert ingest_external_report(positive_report()).structural_report.verified
