"""Finite contradictions and signed dependencies stay distinct from general SAT."""

from dataclasses import replace

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SH, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.shacl.analysis import ShapeAnalysis, analyze_shape_profile
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.profiles.extraction import extract_profile
from bijux_ontology.shacl.profiles.types import ShapeProfile

EX = Namespace("https://example.invalid/shapes/")
_PREFIXES = f"@prefix ex:<{EX}>. @prefix sh:<{SH}>. "


def _profile(text: str, *roots: str) -> ShapeProfile:
    """Use actual dependency-aware extraction independently of any data corpus."""
    graph = Graph().parse(data=_PREFIXES + text, format="turtle")
    profile = extract_profile(prepare_rdf(graph), tuple(EX[root] for root in roots))
    assert profile.structural_report.verified, profile.structural_report.to_dict()
    return profile


def _analysis(text: str, *roots: str) -> ShapeAnalysis:
    """Run the public finite analysis on a real extracted shape closure."""
    return analyze_shape_profile(_profile(text, *roots))


def test_direct_bounds_and_required_enumerations_prove_only_local_conflicts() -> None:
    """Known impossible bounds and enumerations need no sample data to diagnose."""
    for constraints, reason in (
        ("sh:minCount 2; sh:maxCount 1", "count_bounds"),
        ("sh:minCount 1; sh:in ()", "allowed_count"),
        ("sh:hasValue ex:a; sh:in (ex:b)", "fixed_allowed"),
        ("sh:minCount 2; sh:in (ex:a)", "allowed_count"),
    ):
        result = _analysis(f"ex:P sh:path ex:p; {constraints}.", "P")
        assert result.report.outcome == "disagreement"
        assert reason in {item.reason for item in result.local_conflicts}
        assert result.satisfiability == "impossible" and result.root_conflicts
        assert result.report.regime == "finite-static-analysis"
    good = _analysis("ex:P sh:path ex:p; sh:minCount 2; sh:in (ex:a ex:b).", "P")
    assert good.report.verified and good.satisfiability == "not_determined"


def test_fixed_values_count_distinct_rdf_terms_without_inventing_presence() -> None:
    """Two fixed values fit two slots; an optional empty set needs no values."""
    for constraints in (
        "sh:hasValue ex:a, ex:b; sh:maxCount 2",
        "sh:in ()",
        'sh:hasValue "same", "same"^^<http://www.w3.org/2001/XMLSchema#string>; '
        "sh:maxCount 1",
    ):
        result = _analysis(f"ex:P sh:path ex:p; {constraints}.", "P")
        assert result.report.verified and not result.local_conflicts
    exact = Graph().add((EX.P, SH.path, EX.p)).add((EX.P, SH.maxCount, Literal(1)))
    for lexical in ("01", "1"):
        exact.add(
            (EX.P, SH.hasValue, Literal(lexical, datatype=XSD.integer, normalize=False))
        )
    distinct = analyze_shape_profile(extract_profile(prepare_rdf(exact), (EX.P,)))
    assert distinct.satisfiability == "impossible"
    bounded = _analysis(
        "ex:P sh:path ex:p; sh:hasValue ex:a, ex:b; sh:maxCount 1.", "P"
    )
    assert bounded.satisfiability == "impossible"


def test_alternatives_and_optional_invocations_do_not_inherit_sibling_conflicts() -> (
    None
):
    """A bad branch remains conditional; necessary invocations preserve its proof."""
    source = """
      ex:Root sh:or (ex:Bad ex:Good).
      ex:Bad sh:in (). ex:Good sh:nodeKind sh:IRI.
      ex:Required sh:property ex:P. ex:P sh:path ex:p; sh:minCount 2; sh:maxCount 1.
      ex:Optional sh:path ex:q; sh:node ex:Bad.
      ex:Present sh:path ex:q; sh:minCount 1; sh:node ex:Bad.
    """
    alternative = _analysis(source, "Root")
    assert alternative.report.verified and alternative.local_conflicts
    assert not alternative.root_conflicts
    assert _analysis(source, "Required").satisfiability == "impossible"
    assert _analysis(source, "Optional").report.verified
    assert _analysis(source, "Present").satisfiability == "impossible"
    inactive_alternative = _analysis(
        "ex:R sh:or (ex:A ex:B). ex:A sh:in (). ex:B sh:deactivated true; sh:in ().",
        "R",
    )
    assert (
        inactive_alternative.report.verified and not inactive_alternative.root_conflicts
    )
    all_bad = _analysis("ex:R sh:or (ex:A ex:B). ex:A sh:in (). ex:B sh:in ().", "R")
    assert all_bad.satisfiability == "impossible"


def test_signed_components_reuse_deactivation_and_malformed_metadata() -> None:
    """Acyclic reuse completes; recursion has an explicit processor boundary."""
    reused = _analysis(
        "ex:R sh:and (ex:A ex:B). ex:A sh:node ex:C. ex:B sh:node ex:C. "
        "ex:C sh:nodeKind sh:IRI.",
        "R",
    )
    assert reused.report.verified and all(not c.recursive for c in reused.components)
    for parameter, boundary in (
        ("node", "processor_defined"),
        ("not", "negative_recursion_unsupported"),
    ):
        result = _analysis(f"ex:A sh:{parameter} ex:B. ex:B sh:node ex:A.", "A")
        assert result.report.outcome == "unverified"
        assert result.components[0].boundary == boundary
        assert result.satisfiability == "not_determined"
    xone = _analysis("ex:A sh:xone (ex:A ex:B). ex:B sh:nodeKind sh:IRI.", "A")
    assert any(c.boundary == "negative_recursion_unsupported" for c in xone.components)
    inactive = _analysis("ex:A sh:deactivated true; sh:not ex:A; sh:in ().", "A")
    assert inactive.report.verified and inactive.deactivated_shapes == (EX.A,)
    malformed = _analysis('ex:P sh:path ex:p; sh:minCount "2".', "P")
    assert malformed.report.outcome == "unverified" and not malformed.root_conflicts

    negative = _profile("ex:A sh:not ex:B. ex:B sh:in ().", "A")
    oversized = replace(
        negative,
        dependencies=tuple(
            replace(d, parameter=URIRef(str(EX) + "x" * 100_000))
            for d in negative.dependencies
        ),
    )
    assert (
        analyze_shape_profile(oversized, limits=Limits(max_bytes=1000)).report.outcome
        == "exhausted"
    )
    alternatives = _profile(
        "ex:R sh:or (ex:Bad ex:Good). ex:Bad sh:in (). ex:Good sh:nodeKind sh:IRI.", "R"
    )
    cycle = _profile("ex:A sh:not ex:B. ex:B sh:node ex:A.", "A")
    modified = (
        replace(
            negative,
            dependencies=tuple(
                replace(d, parameter=SH.node) for d in negative.dependencies
            ),
        ),
        replace(
            alternatives,
            dependencies=tuple(
                d for d in alternatives.dependencies if d.dependency != EX.Good
            ),
        ),
        replace(
            cycle,
            dependencies=tuple(
                d for d in cycle.dependencies if d.parameter != SH["not"]
            ),
        ),
    )
    anonymous = _profile(
        "ex:R sh:property _:P. _:P sh:path ex:p; sh:minCount 1; sh:node ex:Bad. "
        "ex:Bad sh:in ().",
        "R",
    )
    assert anonymous.source is not None
    child = next(d.dependency for d in anonymous.dependencies if d.shape == EX.R)
    assert isinstance(child, BNode)
    blank_root = extract_profile(anonymous.source, (child,))
    assert analyze_shape_profile(blank_root).satisfiability == "impossible"

    def foreign(node: Node) -> Node:
        return BNode(str(node)) if isinstance(node, BNode) else node

    forged_blanks = (
        replace(blank_root, roots=(BNode(str(child)),)),
        replace(
            anonymous,
            dependencies=tuple(
                replace(d, shape=BNode(str(d.shape)))
                if isinstance(d.shape, BNode)
                else d
                for d in anonymous.dependencies
            ),
        ),
        replace(
            anonymous,
            dependencies=tuple(
                replace(d, dependency=BNode(str(d.dependency)))
                if isinstance(d.dependency, BNode)
                else d
                for d in anonymous.dependencies
            ),
        ),
        replace(
            anonymous,
            dependencies=tuple(
                replace(
                    d,
                    via=tuple(
                        (foreign(a), foreign(b), foreign(c)) for a, b, c in d.via
                    ),
                )
                for d in anonymous.dependencies
            ),
        ),
    )
    for edited in (*modified, *forged_blanks):
        checked = analyze_shape_profile(edited)
        assert checked.report.outcome == "unverified"
        assert checked.satisfiability == "not_determined"
        assert not (
            checked.components or checked.local_conflicts or checked.root_conflicts
        )


def test_required_preparation_and_shared_limits_preserve_completed_evidence() -> None:
    """Incomplete preparation fails; later quota failure keeps an earlier proof."""
    profile = _profile(
        "ex:A sh:path ex:a; sh:minCount 2; sh:maxCount 1. "
        "ex:B sh:path ex:b; sh:minCount 2; sh:maxCount 1.",
        "A",
        "B",
    )
    assert profile.source is not None
    original = set(profile.source.graph())
    result = analyze_shape_profile(profile, budget=Budget(Limits(max_issues=1)))
    assert result.report.outcome == "exhausted"
    assert "shacl.static_incompatibility" in {i.code for i in result.report.issues}
    assert result.root_conflicts and result.satisfiability == "impossible"
    assert set(profile.source.graph()) == original
    stopped = analyze_shape_profile(profile, limits=Limits(max_work=1))
    assert stopped.report.outcome == "exhausted" and not stopped.root_conflicts
    incomplete = replace(
        profile,
        structural_report=Report(
            "shacl.profile",
            0,
            (Issue("profile.unverified", "Unavailable", kind="unverified"),),
            complete=False,
        ),
    )
    result = analyze_shape_profile(incomplete)
    assert result.report.outcome == "unverified" and not result.local_conflicts
    assert "profile.unverified" in {i.code for i in result.report.issues}


def test_supported_proofs_match_actual_maintained_validation_controls() -> None:
    """Real optional backend controls qualify fixed values and empty enumerations."""
    pytest.importorskip("pyshacl")
    for constraints, data, expected in (
        ("sh:hasValue ex:a, ex:b; sh:maxCount 2", "ex:s ex:p ex:a, ex:b.", True),
        ("sh:hasValue ex:a, ex:b; sh:maxCount 1", "ex:s ex:p ex:a, ex:b.", False),
        ("sh:in ()", "", True),
        ("sh:in ()", "ex:s ex:p ex:a.", False),
    ):
        shapes = Graph().parse(
            data=_PREFIXES + f"ex:P sh:path ex:p; sh:targetNode ex:s; {constraints}.",
            format="turtle",
        )
        data_graph = Graph().parse(data=_PREFIXES + data, format="turtle")
        actual = validate_shapes(data_graph, shapes)
        assert actual.conforms is expected
    alternatives = Graph().parse(
        data=_PREFIXES + "ex:R sh:targetNode ex:s; sh:or (ex:Bad ex:Good). "
        "ex:Bad sh:in (). ex:Good sh:nodeKind sh:IRI.",
        format="turtle",
    )
    assert validate_shapes(Graph(), alternatives).conforms is True
    negative = Graph().parse(
        data=_PREFIXES + "ex:R sh:targetNode ex:s; sh:not ex:Bad. ex:Bad sh:in ().",
        format="turtle",
    )
    assert validate_shapes(Graph(), negative).conforms is True
    profile = extract_profile(prepare_rdf(negative), (EX.R,))
    forged = replace(
        profile,
        dependencies=tuple(replace(d, parameter=SH.node) for d in profile.dependencies),
    )
    assert analyze_shape_profile(forged).report.outcome == "unverified"
