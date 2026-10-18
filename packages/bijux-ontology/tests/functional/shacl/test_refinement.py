"""Finite implication proofs accompany actual child/parent backend witnesses."""

import importlib.util
import threading

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import SH, XSD

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.refinement import (
    RefinementContract,
    compare_shape_refinement,
)

EX = Namespace("https://example.invalid/refinement/")
_PREFIX = f"@prefix ex:<{EX}>. @prefix sh:<{SH}>. "


def _contract(body: str, *, target: str = "ex:focus") -> RefinementContract:
    """Use original actual RDF definitions, rather than constructed proof metadata."""
    graph = Graph().parse(
        data=_PREFIX + f"ex:R a sh:NodeShape; sh:targetNode {target}; " + body,
        format="turtle",
    )
    return RefinementContract(prepare_rdf(graph), EX.R)


def _counts(minimum: int, maximum: int | None = None) -> RefinementContract:
    """Declare an ordinary property count in a selected root."""
    extra = "" if maximum is None else f"; sh:maxCount {maximum}"
    return _contract(
        f"sh:property ex:P. ex:P sh:path ex:label; sh:minCount {minimum}{extra}."
    )


def test_replacement_weakening_has_actual_counterexample_and_stronger_proof() -> None:
    """Child omission passes its backend and fails the aligned parent."""
    pytest.importorskip("pyshacl")
    parent = _counts(1)
    weak = compare_shape_refinement(parent, _counts(0))
    assert weak.relation == "weakens" and weak.report.outcome == "disagreement"
    assert weak.witness is not None
    assert weak.child_result is not None and weak.child_result.report.verified
    assert weak.child_result.conforms is True
    assert weak.parent_result is not None and weak.parent_result.conforms is False
    assert not weak.proofs
    stronger = compare_shape_refinement(parent, _counts(2, 2))
    assert stronger.relation == "refines" and stronger.report.verified
    assert stronger.proofs and stronger.witness is not None
    assert stronger.parent_result is not None and stronger.parent_result.conforms
    assert stronger.child_result is not None and stronger.child_result.conforms


def test_effective_inherited_conjunction_and_logical_alternatives() -> None:
    """Adding min zero cannot remove inherited min one in a conjunction."""
    pytest.importorskip("pyshacl")
    child = _contract("""
        sh:and (ex:Inherited ex:Added).
        ex:Inherited sh:property ex:A. ex:A sh:path ex:label; sh:minCount 1.
        ex:Added sh:property ex:B. ex:B sh:path ex:label; sh:minCount 0.
    """)
    result = compare_shape_refinement(_counts(1), child)
    assert result.relation == "refines" and result.report.verified
    parent = _contract("""
        sh:or (ex:One ex:Two).
        ex:One sh:property ex:A. ex:A sh:path ex:label; sh:minCount 1.
        ex:Two sh:property ex:B. ex:B sh:path ex:label; sh:minCount 2.
    """)
    stronger = compare_shape_refinement(parent, _counts(3))
    assert stronger.report.verified and stronger.proofs
    # Both original targets must conform; a passing selected pair cannot witness
    # compatibility when its outgoing edge violates another declared target.
    global_child = _contract(
        "sh:targetNode ex:other; sh:property ex:Incoming, ex:Outgoing. "
        "ex:Incoming sh:path [sh:inversePath ex:edge]; sh:hasValue ex:other. "
        "ex:Outgoing sh:path ex:edge; sh:maxCount 0."
    )
    global_parent = _contract(
        "sh:targetNode ex:other; sh:property ex:Required. "
        "ex:Required sh:path ex:label; sh:minCount 1."
    )
    scope = compare_shape_refinement(global_parent, global_child)
    assert scope.relation == "unknown" and not scope.report.verified
    # A deactivated alternative is a genuine unconstrained branch.
    inactive = _contract(
        "sh:deactivated true; sh:property ex:P. ex:P sh:path ex:label; sh:minCount 3."
    )
    assert compare_shape_refinement(_counts(1), inactive).relation == "weakens"


def test_exact_enumerations_fixed_values_datatypes_and_node_kinds() -> None:
    """Finite membership keeps lexical 01 distinct from integer 1."""
    pytest.importorskip("pyshacl")
    parent = _contract(
        "sh:property ex:P. ex:P sh:path ex:label; sh:minCount 1; "
        'sh:in ("a" "b"); sh:nodeKind sh:Literal; '
        "sh:datatype <http://www.w3.org/2001/XMLSchema#string>."
    )
    child = _contract(
        "sh:property ex:P. ex:P sh:path ex:label; sh:minCount 1; "
        'sh:in ("a"); sh:hasValue "a"; sh:nodeKind sh:Literal; '
        "sh:datatype <http://www.w3.org/2001/XMLSchema#string>."
    )
    assert compare_shape_refinement(parent, child).report.verified

    # Build exact literal terms directly; the caller parser must not normalize them.
    def lexical(text: str) -> RefinementContract:
        """Retain exact original fixed and enumeration terms."""
        graph = Graph().parse(
            data=_PREFIX + "ex:R sh:targetNode ex:focus; sh:property ex:P. "
            "ex:P sh:path ex:label; sh:minCount 1.",
            format="turtle",
        )
        graph.add(
            (EX.P, SH.hasValue, Literal(text, datatype=XSD.integer, normalize=False))
        )
        return RefinementContract(prepare_rdf(graph), EX.R)

    distinct = compare_shape_refinement(lexical("01"), lexical("1"))
    assert distinct.relation == "weakens" and not distinct.proofs
    for text in ("١", " 1 "):
        invalid = lexical(text)
        result = compare_shape_refinement(invalid, invalid)
        assert result.relation == "unknown" and not result.report.verified
    for text in ("+1", "01"):
        qualified = lexical(text)
        assert compare_shape_refinement(qualified, qualified).report.verified
    mismatch = compare_shape_refinement(
        _counts(1),
        _contract(
            "sh:property ex:P. ex:P sh:path ex:label; sh:minCount 1; sh:datatype <http://www.w3.org/2001/XMLSchema#boolean>."
        ),
    )
    assert mismatch.report.verified


def test_scope_recursion_unsupported_and_original_ownership_remain_required() -> None:
    """No sample or equal blank label can override a different contract scope."""
    first = _counts(1)
    other = _contract(
        "sh:property ex:P. ex:P sh:path ex:label; sh:minCount 2.", target="ex:other"
    )
    assert compare_shape_refinement(first, other).relation == "unknown"
    named = RefinementContract(first.source, first.root, EX.named)
    assert compare_shape_refinement(first, named).report.outcome == "unverified"
    recursive = _contract("sh:node ex:R.")
    assert compare_shape_refinement(first, recursive).relation == "unknown"
    sparql = _contract('sh:sparql [sh:select "SELECT $this WHERE {}"].')
    assert compare_shape_refinement(first, sparql).report.outcome == "unverified"
    graph = Graph().parse(
        data=_PREFIX
        + "ex:R sh:targetNode ex:focus; sh:property [sh:path ex:label; sh:minCount 2].",
        format="turtle",
    )
    owned = prepare_rdf(graph)
    blank = next(
        node
        for node in owned.graph().objects(EX.R, SH.property)
        if isinstance(node, BNode)
    )
    foreign = RefinementContract(owned, BNode(str(blank)))
    assert compare_shape_refinement(first, foreign).report.outcome == "unverified"
    data = Dataset()
    data.graph(EX.shapes).parse(
        data=_PREFIX + "ex:R sh:targetNode ex:focus; sh:property ex:P. "
        "ex:P sh:path ex:label; sh:minCount 2.",
        format="turtle",
    )
    data.graph(EX.hidden).parse(
        data=_PREFIX + "ex:R sh:targetNode ex:other.", format="turtle"
    )
    selected = RefinementContract(
        prepare_rdf(RdfSource(data, graph_name=str(EX.shapes))), EX.R
    )
    if importlib.util.find_spec("pyshacl") is not None:
        assert compare_shape_refinement(first, selected).report.verified


def test_bounds_cancel_and_missing_backend_never_turn_samples_into_proof() -> None:
    """Proof preparation and actual witnesses share finite controlled resources."""
    first, second = _counts(1), _counts(2)
    exhausted = compare_shape_refinement(first, second, limits=Limits(max_work=1))
    assert exhausted.report.outcome == "exhausted" and exhausted.relation == "unknown"
    stopped = threading.Event()
    stopped.set()
    assert (
        compare_shape_refinement(first, second, cancel=stopped).report.outcome
        == "exhausted"
    )
    budget = Budget(Limits(max_work=1))
    budget.consume("work")
    assert (
        compare_shape_refinement(first, second, budget=budget).report.outcome
        == "exhausted"
    )
    # A child with a supported but incompatible conjunction cannot be certified
    # merely because the implication rules happen to find a sufficient bound.
    impossible = _contract(
        "sh:property ex:P. ex:P sh:path ex:label; sh:minCount 2; sh:maxCount 1."
    )
    result = compare_shape_refinement(first, impossible)
    assert result.relation == "unknown" and not result.report.verified
    if importlib.util.find_spec("pyshacl") is None:
        unavailable = compare_shape_refinement(first, second)
        assert unavailable.relation == "unknown" and not unavailable.report.verified
        assert unavailable.proofs and unavailable.child_result is not None
