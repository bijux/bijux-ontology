"""Native supplied ShExJ validation and required explicit binding controls."""

import json
import time
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.shex import ShexBinding, ShexSchema, validate_shex
from bijux_ontology.shex.types import ShexResult

pytest.importorskip("pyrudof")
E = Namespace("urn:shex:")
CONTEXT = "http://www.w3.org/ns/shex.jsonld"


def constraint(
    predicate: URIRef, value: object = None, **fields: object
) -> dict[str, object]:
    """Keep fixture declarations in the actual standard native JSON dialect."""
    result = {"type": "TripleConstraint", "predicate": str(predicate), **fields}
    if value is not None:
        result["valueExpr"] = value
    return result


def schema(
    expression: object, *, iri: str = "urn:schema", imports: tuple[str, ...] = ()
) -> ShexSchema:
    """Give each supplied authored shape a named declaration and logical document."""
    return ShexSchema(
        iri,
        json.dumps(
            {
                "@context": CONTEXT,
                "type": "Schema",
                "imports": list(imports),
                "shapes": [
                    {"type": "ShapeDecl", "id": str(E.S), "shapeExpr": expression}
                ],
            }
        ),
    )


def shape(expression: object, **fields: object) -> dict[str, object]:
    """Supply standard native structural constraints without an adapter DSL."""
    return {"type": "Shape", "expression": expression, **fields}


def check(
    graph: Graph | PreparedSource,
    supplied: ShexSchema,
    directory: Path,
    *,
    imports: dict[str, ShexSchema] | None = None,
    graph_name: GraphSelection | None = None,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> ShexResult:
    """Use the same explicit real RDF node/shape obligation in positive controls."""
    return validate_shex(
        graph,
        supplied,
        bindings=(ShexBinding(E.A, E.S),),
        artifacts=directory,
        imports=imports,
        graph_name=graph_name,
        budget=budget,
        cancel=cancel,
    )


def test_recursive_native_results_and_changed_descendant(tmp_path: Path) -> None:
    supplied = schema(
        shape(
            {
                "type": "EachOf",
                "expressions": [
                    constraint(
                        E.name, {"type": "NodeConstraint", "datatype": str(XSD.string)}
                    ),
                    constraint(E.knows, str(E.S), min=0, max=-1),
                ],
            }
        )
    )
    graph = (
        Graph()
        .add((E.A, E.name, Literal("A")))
        .add((E.B, E.name, Literal("B")))
        .add((E.A, E.knows, E.B))
        .add((E.B, E.knows, E.A))
    )
    original = set(graph)
    good = check(graph, supplied, tmp_path)
    assert good.report.verified and good.backend_version == "pyrudof 0.3.20"
    assert cast(dict[str, object], good.obligations[0].native)["status"] == "conformant"
    assert set(graph) == original
    graph.remove((E.B, E.name, None))
    bad = check(graph, supplied, tmp_path)
    assert bad.report.outcome == "disagreement"
    assert bad.obligations[0].status == "not_matched"
    assert cast(dict[str, object], bad.obligations[0].native)["reason"]
    exported = cast(dict[str, object], good.obligations[0].native)
    exported["status"] = "changed"
    assert cast(dict[str, object], good.obligations[0].native)["status"] == "conformant"


def test_repeated_exact_terms_native_datatypes_and_capacity(tmp_path: Path) -> None:
    supplied = schema(
        shape(
            constraint(
                E.p,
                {"type": "NodeConstraint", "datatype": str(XSD.integer)},
                min=2,
                max=3,
            )
        )
    )
    first = Literal("01", datatype=XSD.integer, normalize=False)
    second = Literal("1", datatype=XSD.integer, normalize=False)
    graph = Graph().add((E.A, E.p, first)).add((E.A, E.p, second))
    assert check(graph, supplied, tmp_path).report.verified
    graph.remove((E.A, E.p, second))
    assert check(graph, supplied, tmp_path).report.outcome == "disagreement"
    scalar = schema(
        shape(constraint(E.p, {"type": "NodeConstraint", "datatype": str(XSD.integer)}))
    )
    for value in (Literal("bad", datatype=XSD.integer, normalize=False), Literal("1")):
        assert (
            check(Graph().add((E.A, E.p, value)), scalar, tmp_path).report.outcome
            == "disagreement"
        )
    huge = Literal(str(2**127), datatype=XSD.integer, normalize=False)
    result = check(Graph().add((E.A, E.p, huge)), scalar, tmp_path)
    assert (
        result.report.outcome == "unverified" and result.obligations[0].native is None
    )

    whitespace = Literal(" 1 ", datatype=XSD.integer, normalize=False)
    limited = check(Graph().add((E.A, E.p, whitespace)), scalar, tmp_path)
    assert limited.report.outcome == "unverified"
    assert limited.obligations[0].native is None


def test_nested_operation_adopts_real_paid_native_findings(tmp_path: Path) -> None:
    """Inherited receipt checks see the actual shared counters through wrappers."""
    supplied = schema(
        shape(constraint(E.p, {"type": "NodeConstraint", "datatype": str(XSD.integer)}))
    )
    graph = Graph().add((E.A, E.p, Literal(1))).add((E.B, E.p, Literal("wrong")))
    budget = Budget()
    first = Operation(None, budget, 30, None)
    nested = Operation(None, first, 30, None)
    result = validate_shex(
        graph,
        supplied,
        bindings=(ShexBinding(E.A, E.S), ShexBinding(E.B, E.S)),
        artifacts=tmp_path,
        budget=nested,
    )
    assert result.report.complete and result.report.outcome == "disagreement"
    assert tuple(item.status for item in result.obligations) == (
        "matched",
        "not_matched",
    )
    assert any(issue.kind == "finding" for issue in result.report.issues)
    assert all(
        first.remaining(kind) == nested.remaining(kind) == budget.remaining(kind)
        for kind in ("bytes", "work", "issues", "states")
    )


def test_supplied_import_closure_and_unknown_features(tmp_path: Path) -> None:
    child_iri = "https://example.invalid/child.shexj"
    root = schema(shape(constraint(E.knows, str(E.Child))), imports=(child_iri,))
    child_data = json.loads(
        schema(
            shape(
                constraint(
                    E.name, {"type": "NodeConstraint", "datatype": str(XSD.string)}
                )
            ),
            iri=child_iri,
        ).content
    )
    child_data["shapes"][0]["id"] = str(E.Child)
    child = ShexSchema(child_iri, json.dumps(child_data))
    graph = Graph().add((E.A, E.knows, E.B)).add((E.B, E.name, Literal("B")))
    assert check(graph, root, tmp_path, imports={child_iri: child}).report.verified
    missing = check(graph, root, tmp_path)
    assert not missing.report.verified and missing.obligations[0].native is None
    for expression in (
        {"type": "ShapeExternal"},
        {"type": "ShapeNot", "shapeExpr": str(E.S)},
        {"type": "NodeConstraint", "datatype": str(XSD.decimal)},
        {"type": "NodeConstraint", "pattern": ".*"},
    ):
        result = check(graph, schema(expression), tmp_path)
        assert (
            result.report.outcome == "unverified"
            and result.obligations[0].native is None
        )
    duplicate = json.loads(child.content)
    duplicate["shapes"][0]["id"] = str(E.S)
    assert (
        check(
            graph,
            root,
            tmp_path,
            imports={child_iri: ShexSchema(child_iri, json.dumps(duplicate))},
        ).report.outcome
        == "input_error"
    )
    assert (
        validate_shex(graph, child, artifacts=tmp_path).report.outcome == "input_error"
    )


def test_explicit_scope_and_unsupported_focus_do_not_autoselect(tmp_path: Path) -> None:
    supplied = schema(
        shape(constraint(E.p, {"type": "NodeConstraint", "nodeKind": "iri"}))
    )
    dataset = Dataset()
    dataset.graph(E.good).add((E.A, E.p, E.B))
    dataset.graph(E.bad).add((E.C, E.p, E.D))
    prepared = prepare_rdf(dataset)
    assert check(prepared, supplied, tmp_path, graph_name=E.good).report.verified
    assert (
        check(prepared, supplied, tmp_path, graph_name=E.bad).report.outcome
        == "disagreement"
    )
    unknown = validate_shex(
        prepared,
        supplied,
        bindings=(ShexBinding(BNode("A"), E.S), ShexBinding(Literal("A"), E.S)),
        graph_name=E.good,
        artifacts=tmp_path,
    )
    assert not unknown.report.verified and all(
        item.status == "unknown" and item.native is None for item in unknown.obligations
    )
    assert check(prepared, supplied, tmp_path).report.outcome == "input_error"


def test_native_alternative_inverse_closed_and_extra(tmp_path: Path) -> None:
    iri = {"type": "NodeConstraint", "nodeKind": "iri"}
    alternate = schema(
        shape(
            {
                "type": "OneOf",
                "expressions": [constraint(E.p, iri), constraint(E.q, iri)],
            }
        )
    )
    assert check(Graph().add((E.A, E.q, E.B)), alternate, tmp_path).report.verified
    assert check(Graph(), alternate, tmp_path).report.outcome == "disagreement"
    inverse = schema(shape(constraint(E.p, iri, inverse=True)))
    assert check(Graph().add((E.B, E.p, E.A)), inverse, tmp_path).report.verified
    assert (
        check(Graph().add((E.A, E.p, E.B)), inverse, tmp_path).report.outcome
        == "disagreement"
    )
    closed = schema(shape(constraint(E.p, iri), closed=True))
    graph = Graph().add((E.A, E.p, E.B))
    assert check(graph, closed, tmp_path).report.verified
    graph.add((E.A, E.q, E.B))
    assert check(graph, closed, tmp_path).report.outcome == "disagreement"
    string = {"type": "NodeConstraint", "datatype": str(XSD.string)}
    extra = schema(shape(constraint(E.p, string), extra=[str(E.p)]))
    graph = Graph().add((E.A, E.p, Literal("name"))).add((E.A, E.p, E.B))
    assert check(graph, extra, tmp_path).report.verified
    assert (
        check(graph, schema(shape(constraint(E.p, string))), tmp_path).report.outcome
        == "disagreement"
    )


def test_shared_limits_and_cancelled_paid_prefix_recover(tmp_path: Path) -> None:
    supplied = schema(
        shape(constraint(E.p, {"type": "NodeConstraint", "nodeKind": "iri"}))
    )
    graph = Graph().add((E.A, E.p, E.B))
    mixed = validate_shex(
        graph,
        supplied,
        bindings=(ShexBinding(E.Missing, E.S), ShexBinding(E.A, E.S)),
        artifacts=tmp_path,
    )
    assert mixed.report.outcome == "disagreement" and mixed.report.checked == 2
    assert tuple(item.status for item in mixed.obligations) == (
        "not_matched",
        "matched",
    )
    budget = Budget(Limits(max_work=1))
    assert check(graph, supplied, tmp_path, budget=budget).report.outcome == "exhausted"
    cancellation = Event()
    cancellation.set()
    assert (
        check(graph, supplied, tmp_path, cancel=cancellation).report.outcome
        == "exhausted"
    )
    cancellation.clear()
    limits = Limits(
        max_work=100_000_000,
        max_states=16_000_000,
        max_total_bytes=500_000_000,
        max_issues=20_000,
    )
    bindings = (ShexBinding(E.Missing, E.S),) + (ShexBinding(E.A, E.S),) * 5000
    observed = Event()
    stop = Event()

    def cancel_after_completed() -> None:
        """Interrupt only after an actual native mismatch was durably retained."""
        deadline = time.monotonic() + 20
        while not stop.is_set() and time.monotonic() < deadline:
            if any(
                path.stat().st_size
                for path in tmp_path.rglob("completed.jsonl")
                if path.exists()
            ):
                observed.set()
                cancellation.set()
                return
            stop.wait(0.002)

    watcher = Thread(target=cancel_after_completed)
    watcher.start()
    try:
        result = validate_shex(
            graph,
            supplied,
            bindings=bindings,
            limits=limits,
            budget=Operation(limits, Operation(limits, None, 30, None), 30, None),
            cancel=cancellation,
            artifacts=tmp_path,
        )
    finally:
        stop.set()
        watcher.join()
    assert observed.is_set() and result.report.outcome == "exhausted"
    assert result.obligations[0].status == "not_matched"
    assert (
        cast(dict[str, object], result.obligations[0].native)["status"]
        == "nonconformant"
    )
    assert any(item.status == "unknown" for item in result.obligations)
    assert check(graph, supplied, tmp_path).report.verified
