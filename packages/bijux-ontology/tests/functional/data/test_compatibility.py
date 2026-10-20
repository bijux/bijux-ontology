"""Actual RDF compatibility remains distinct from exact assertion policies."""

import json

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.data.compatibility import check_data_compatibility
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.datatype_evaluators import DatatypeEvaluator, DatatypeRegistry
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.loading import load_vocabulary

EX = Namespace("urn:compatibility:")


def test_disjoint_types_and_open_consequences_keep_original_witnesses() -> None:
    schema = Graph().add((EX.A, OWL.disjointWith, EX.B))
    schema.add((EX.p, RDFS.domain, EX.B))
    schema.add((EX.p, RDFS.range, EX.B))
    schema.add((EX.Child, RDFS.subClassOf, EX.A))
    data = Graph().add((EX.s, EX.p, EX.o))
    open_report = check_data_compatibility(data, schema)
    assert open_report.verified
    strict = check_data_compatibility(data, schema, asserted_types="required")
    assert strict.children[0].verified
    assert strict.children[1].regime == "application-validation"
    assert len(strict.children[1].issues) == 2
    data.add((EX.s, RDF.type, EX.Child))
    inferred = check_data_compatibility(data, schema)
    assert not inferred.verified
    issue = next(i for i in inferred.issues if i.code == "data.disjoint_conflict")
    evidence = json.loads(issue.evidence_json or "{}")
    assert [list(term_key(t)) for t in (EX.A, OWL.disjointWith, EX.B)] in evidence[
        "ontology"
    ]
    assert [list(term_key(t)) for t in (EX.Child, RDFS.subClassOf, EX.A)] in evidence[
        "ontology"
    ]
    assert [list(term_key(t)) for t in (EX.s, EX.p, EX.o)] in evidence["data"]
    assert check_data_compatibility(data, schema, mode="asserted").verified
    data.add((EX.s, RDF.type, EX.B))
    assert not check_data_compatibility(data, schema, mode="asserted").verified
    # Required exact assertions cannot borrow an inherited or inferred type.
    strict = check_data_compatibility(data, schema, asserted_types="required")
    assert strict.children[1].issues[0].location == str(EX.o)


def test_inherited_inverse_properties_and_unsupported_siblings_are_separate() -> None:
    schema = Graph().add((EX.p, RDFS.subPropertyOf, EX.q))
    schema.add((EX.q, OWL.inverseOf, EX.inverse))
    schema.add((EX.inverse, RDFS.domain, EX.B))
    schema.add((EX.A, OWL.disjointWith, EX.B))
    data = Graph().add((EX.s, EX.p, EX.o)).add((EX.o, RDF.type, EX.A))
    assert check_data_compatibility(data, schema).outcome == "disagreement"
    assert check_data_compatibility(data, schema, mode="asserted").verified
    schema.add((EX.A, RDFS.subClassOf, BNode()))
    report = check_data_compatibility(data, schema)
    assert any(i.code == "data.disjoint_conflict" for i in report.issues)
    assert any(i.code == "data.expression_unverified" for i in report.issues)
    assert not report.complete
    anonymous_range = Graph().add((EX.p, RDFS.range, BNode()))
    bad_literal = Graph().add(
        (EX.s, EX.p, Literal("1e3", datatype=XSD.decimal, normalize=False))
    )
    partial = check_data_compatibility(bad_literal, anonymous_range)
    assert any(i.code == "literal.lexical_invalid" for i in partial.issues)
    assert any(i.code == "data.expression_unverified" for i in partial.issues)
    assert partial.outcome == "disagreement" and not partial.complete
    schema = Graph().add((EX.p, RDFS.domain, BNode()))
    assert check_data_compatibility(data, schema).outcome == "unverified"
    group, head = BNode(), BNode()
    malformed = Graph().add((group, RDF.type, OWL.AllDisjointClasses))
    malformed.add((group, OWL.members, head)).add((head, RDF.first, EX.A))
    assert (
        check_data_compatibility(Graph().add((EX.s, RDF.type, EX.A)), malformed).outcome
        == "unverified"
    )


def test_actual_builtin_range_membership_uses_owl_value_identity() -> None:
    schema = Graph().add((EX.p, RDFS.range, XSD.integer))
    for text, datatype, expected in (
        ("1.0", XSD.decimal, True),
        ("1.5", XSD.decimal, False),
        ("01", XSD.integer, True),
        ("1", XSD.double, False),
    ):
        value = Literal(text, datatype=datatype, normalize=False)
        identity = term_key(value)
        data = Graph().add((EX.s, EX.p, value))
        report = check_data_compatibility(data, schema)
        assert report.verified is expected, report.to_dict()
        assert term_key(value) == identity
        if not expected:
            issue = next(i for i in report.issues if i.code == "data.datatype_conflict")
            assert list(identity) in json.loads(issue.evidence_json or "{}")["data"][0]
    schema = Graph().add((EX.p, RDFS.range, XSD.positiveInteger))
    assert check_data_compatibility(
        Graph().add(
            (EX.s, EX.p, Literal("1.0", datatype=XSD.decimal, normalize=False))
        ),
        schema,
    ).verified
    negative = check_data_compatibility(
        Graph().add((EX.s, EX.p, Literal("-1", datatype=XSD.integer, normalize=False))),
        schema,
    )
    assert any(i.code == "data.datatype_conflict" for i in negative.issues)
    single = check_data_compatibility(
        Graph().add((EX.s, EX.p, Literal("-1", datatype=XSD.integer, normalize=False))),
        schema,
        limits=Limits(max_issues=1),
    )
    assert [i.code for i in single.issues] == ["data.datatype_conflict"]
    rdf = check_data_compatibility(
        Graph().add(
            (EX.s, EX.p, Literal("1.0", datatype=XSD.decimal, normalize=False))
        ),
        schema,
        regime="rdf",
    )
    assert rdf.outcome == "unverified"
    resources = Graph().add((EX.s, EX.p, EX.o))
    assert check_data_compatibility(resources, schema).outcome == "unverified"
    for datatype in (EX.custom, RDFS.Literal):
        custom_range = Graph().add((EX.p, RDFS.range, datatype))
        if datatype == EX.custom:
            custom_range.add((datatype, RDF.type, RDFS.Datatype))
        assert check_data_compatibility(resources, custom_range).outcome == "unverified"
    registered = DatatypeRegistry((DatatypeEvaluator(str(EX.registered)),))
    assert (
        check_data_compatibility(
            resources,
            Graph().add((EX.p, RDFS.range, EX.registered)),
            datatype_registry=registered,
        ).outcome
        == "unverified"
    )
    assert check_data_compatibility(
        resources, Graph().add((EX.p, RDFS.range, EX.OrdinaryClass))
    ).verified
    schema.add((EX.p, RDF.type, OWL.DatatypeProperty))
    assert any(
        i.code == "data.property_conflict"
        for i in check_data_compatibility(resources, schema).issues
    )


def test_graph_and_source_selection_exclude_unselected_statements() -> None:
    data = Dataset()
    data.graph(EX.safe).add((EX.s, RDF.type, EX.A))
    data.graph(EX.poison).add((EX.s, RDF.type, EX.B))
    schema = Graph().add((EX.A, OWL.disjointWith, EX.B))
    with pytest.raises(InputError, match="Dataset requires"):
        check_data_compatibility(data, schema)
    assert check_data_compatibility(data, schema, graph_name=EX.safe).verified
    assert not check_data_compatibility(data, schema, union=True).verified
    data.graph(BNode()).add((EX.s, RDF.type, EX.A)).add((EX.s, RDF.type, EX.B))
    owned = prepare_rdf(data)
    blank_graph = next(
        g.identifier
        for g in owned.dataset().graphs()
        if isinstance(g.identifier, BNode)
    )
    scoped = check_data_compatibility(owned, schema, graph_name=blank_graph)
    proof = next(i for i in scoped.issues if i.code == "data.disjoint_conflict")
    assert json.loads(proof.evidence_json or "{}")["graph_selection"] == {
        "kind": "named",
        "term": list(term_key(blank_graph)),
    }
    selected = prepare_rdf(RdfSource(data, graph_name=str(EX.safe)))
    assert check_data_compatibility(selected, schema).verified
    with pytest.raises(InputError, match="conflicts"):
        check_data_compatibility(selected, schema, union=True)
    actual = Graph().add((EX.s, RDF.type, EX.A)).add((EX.s, RDF.type, EX.B))
    view = load_vocabulary(Graph().add((EX.A, RDF.type, OWL.Class)), lookup=(schema,))
    assert check_data_compatibility(actual, view).verified
    assert not check_data_compatibility(
        actual, view, scopes=("local", "lookup")
    ).verified
    # Independent ontology sources must not join an anonymous expression by label.
    local = Graph().add((EX.p, RDFS.domain, BNode("shared")))
    extra = Graph().add((BNode("shared"), OWL.equivalentClass, EX.B))
    view = load_vocabulary(local, lookup=(extra,))
    assert (
        check_data_compatibility(
            Graph().add((EX.s, EX.p, EX.o)), view, scopes=("local", "lookup")
        ).outcome
        == "unverified"
    )


def test_custom_capabilities_and_stopped_work_do_not_invent_membership() -> None:
    value = Literal("12", datatype=EX.custom, normalize=False)
    data = Graph().add((EX.s, EX.p, value))
    schema = Graph().add((EX.p, RDFS.range, XSD.integer))
    validity_only = DatatypeRegistry(
        (DatatypeEvaluator(str(EX.custom), (lambda v: True,)),)
    )
    unknown = check_data_compatibility(
        data, schema, datatype_registry=validity_only, limits=Limits(max_issues=1)
    )
    assert unknown.outcome == "unverified"
    assert [issue.code for issue in unknown.issues] == ["data.datatype_unverified"]
    registry = DatatypeRegistry(
        (
            DatatypeEvaluator(
                str(EX.custom),
                (lambda v: True,),
                relation=lambda required, regime: (
                    "subset" if required == str(XSD.integer) else "unknown"
                ),
            ),
        )
    )
    assert check_data_compatibility(data, schema, datatype_registry=registry).verified
    rejected = DatatypeRegistry(
        (DatatypeEvaluator(str(EX.custom), (lambda v: False,)),)
    )
    one_finding = check_data_compatibility(
        data, Graph(), datatype_registry=rejected, limits=Limits(max_issues=1)
    )
    assert one_finding.outcome == "disagreement"
    assert [i.code for i in one_finding.issues] == ["literal.lexical_invalid"]
    state = {"valid": True}
    mutable = DatatypeRegistry(
        (
            DatatypeEvaluator(
                str(EX.custom),
                (lambda v: state["valid"],),
                relation=lambda required, regime: "subset",
            ),
        )
    )
    assert check_data_compatibility(data, schema, datatype_registry=mutable).verified
    state["valid"] = False
    assert any(
        i.code == "literal.lexical_invalid"
        for i in check_data_compatibility(
            data, schema, datatype_registry=mutable
        ).issues
    )
    schema = Graph().add((EX.A, OWL.disjointWith, EX.B))
    data = Graph().add((EX.s, RDF.type, EX.A)).add((EX.s, RDF.type, EX.B))
    # The repeated operation shares the same allowance, rather than resetting it.
    budget = Budget(Limits(max_work=10000))
    reports = [check_data_compatibility(data, schema, budget=budget) for _ in range(10)]
    assert any(i.code == "data.disjoint_conflict" for i in reports[0].issues)
    assert reports[-1].outcome == "exhausted" and not reports[-1].complete
    data.add((EX.s, EX.p, Literal("1.0", datatype=XSD.decimal, normalize=False)))
    schema.add((EX.p, RDFS.range, XSD.integer))
    partial = check_data_compatibility(data, schema, limits=Limits(max_resources=2))
    assert partial.complete and partial.outcome == "disagreement"
    assert any(i.code == "data.disjoint_conflict" for i in partial.issues)
    assert not any(i.code == "data.datatype_conflict" for i in partial.issues)
    large = Graph().add((EX.s, EX.p, Literal("x" * 100000)))
    report = check_data_compatibility(
        prepare_rdf(large), schema, limits=Limits(max_bytes=1000)
    )
    assert report.outcome == "exhausted" and not report.complete
    assert all(len(i.location) < 1000 for i in report.issues)


def test_exact_decimal_integer_subtypes_keep_original_terms_and_invalids() -> None:
    """Qualified numeric meaning preserves signs, extrema and lexical evidence."""
    for text, required, member in (
        ("+001.000", XSD.integer, True),
        ("1.0001", XSD.integer, False),
        ("-.000", XSD.nonNegativeInteger, True),
        ("-0.0", XSD.positiveInteger, False),
        ("+0.0", XSD.nonPositiveInteger, True),
        ("-0.0", XSD.negativeInteger, False),
        ("-1.000", XSD.negativeInteger, True),
        ("127.000", XSD.byte, True),
        ("128.000", XSD.byte, False),
        ("-128.000", XSD.byte, True),
        ("-129.000", XSD.byte, False),
        ("32767.0", XSD.short, True),
        ("32768.0", XSD.short, False),
        ("2147483647.0", XSD.int, True),
        ("2147483648.0", XSD.int, False),
        ("-9223372036854775808.0", XSD.long, True),
        ("9223372036854775808.0", XSD.long, False),
        ("255.0", XSD.unsignedByte, True),
        ("256.0", XSD.unsignedByte, False),
        ("65535.0", XSD.unsignedShort, True),
        ("65536.0", XSD.unsignedShort, False),
        ("4294967295.0", XSD.unsignedInt, True),
        ("4294967296.0", XSD.unsignedInt, False),
        ("18446744073709551615.0", XSD.unsignedLong, True),
        ("18446744073709551616.0", XSD.unsignedLong, False),
    ):
        value = Literal(text, datatype=XSD.decimal, normalize=False)
        identity = term_key(value)
        report = check_data_compatibility(
            Graph().add((EX.s, EX.p, value)),
            Graph().add((EX.p, RDFS.range, required)),
            limits=Limits(max_issues=1),
        )
        assert report.complete and report.verified is member, report.to_dict()
        assert term_key(value) == identity
        if not member:
            assert [issue.code for issue in report.issues] == ["data.datatype_conflict"]
    schema = Graph().add((EX.p, RDFS.range, XSD.integer))
    for text in ("1e3", "NaN", "INF", "1 0"):
        value = Literal(text, datatype=XSD.decimal, normalize=False)
        invalid = check_data_compatibility(Graph().add((EX.s, EX.p, value)), schema)
        assert not invalid.verified and any(
            issue.code == "literal.lexical_invalid" for issue in invalid.issues
        )
    oversized = check_data_compatibility(
        Graph().add(
            (
                EX.s,
                EX.p,
                Literal("1" * 4097 + ".0", datatype=XSD.decimal, normalize=False),
            )
        ),
        schema,
    )
    assert oversized.outcome == "unverified" and any(
        issue.code == "literal.lexical_unverified" for issue in oversized.issues
    )


def test_bulk_decimal_membership_keeps_shared_resources_and_paid_findings() -> None:
    """Numeric membership spends bounded work, not a native store per sample."""
    schema = Graph().add((EX.p, RDFS.range, XSD.integer))
    data = Graph()
    for index in range(3000):
        data.add(
            (
                EX.s,
                EX.p,
                Literal(f"{index}.0", datatype=XSD.decimal, normalize=False),
            )
        )
    shared = Budget()
    report = check_data_compatibility(data, schema, budget=shared)
    assert report.verified and report.checked == 9000
    assert shared._counts["resources"] == 2
    schema.add((EX.A, OWL.disjointWith, EX.B))
    data.add((EX.contradiction, RDF.type, EX.A))
    data.add((EX.contradiction, RDF.type, EX.B))
    for index in range(3000, 4000):
        data.add(
            (
                EX.s,
                EX.p,
                Literal(f"{index}.0", datatype=XSD.decimal, normalize=False),
            )
        )
    stopped = check_data_compatibility(data, schema)
    assert stopped.outcome == "exhausted" and not stopped.complete
    assert any(issue.code == "data.disjoint_conflict" for issue in stopped.issues)
    assert any(
        issue.code == "execution.limit_exceeded" and issue.kind == "exhausted"
        for issue in stopped.issues
    )
