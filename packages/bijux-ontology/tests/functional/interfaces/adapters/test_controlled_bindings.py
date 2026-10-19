"""Actual controlled subsets and record traits reuse existing native consumers."""

import importlib.util
from dataclasses import replace
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, SKOS, XSD

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    EmissionCondition,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled import (
    AllowedDifference,
    AllowedIntersection,
    AllowedUnion,
    BindingCase,
    CompiledBindings,
    compile_allowed,
    compile_bindings,
    select_binding,
)
from bijux_ontology.models.emission.application import application_matches
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclCount,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.skos.subsets.selection import select_concepts
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary

EX = Namespace("https://example.org/controlled/")


def _graph() -> Graph:
    """Supply independent memberships, hierarchy and lifecycle assertions."""
    graph = Graph()
    graph.add((EX.scheme, RDF.type, SKOS.ConceptScheme))
    for node in (EX.Root, EX.A, EX.B, EX.C, EX.D):
        graph.add((node, RDF.type, SKOS.Concept))
        graph.add((node, SKOS.inScheme, EX.scheme))
        graph.add((node, OWL.deprecated, Literal(node == EX.B)))
    for node in (EX.A, EX.B, EX.C):
        graph.add((node, SKOS.broader, EX.Root))
    for collection, members in (
        (EX.retired, (EX.Root, EX.C)),
        (EX.alternative, (EX.C, EX.D)),
    ):
        graph.add((collection, RDF.type, SKOS.Collection))
        for member in members:
            graph.add((collection, SKOS.member, member))
    graph.add((EX.Record, RDF.type, OWL.Class))
    graph.add((EX.choice, RDF.type, OWL.ObjectProperty))
    graph.add((EX.kind, RDF.type, OWL.DatatypeProperty))
    return graph


def _expressions() -> tuple[AllowedDifference, ValueEnumeration]:
    """Real scheme/root/collection selections precede the pure set compiler."""
    view = inspect_vocabulary(_graph())
    rooted = select_concepts(
        view,
        scheme=str(EX.scheme),
        roots=(str(EX.Root),),
        closed=True,
        deprecation="allow",
    )
    active = select_concepts(
        view, scheme=str(EX.scheme), closed=True, deprecation="require-active"
    )
    retired = select_concepts(
        view, collection=str(EX.retired), closed=True, deprecation="allow"
    )
    alternative = select_concepts(
        view, collection=str(EX.alternative), closed=True, deprecation="allow"
    )
    assert all(item.report.verified for item in (rooted, active, retired, alternative))
    return (
        AllowedDifference(AllowedIntersection((rooted, active)), retired),
        compile_allowed(alternative),
    )


def _plan() -> CompiledBindings:
    """Compile one actual source selection and one declared alternative once."""
    rooted, alternative = _expressions()
    return compile_bindings(
        [
            BindingCase("rooted", "rooted", rooted),
            BindingCase("alternative", "alternative", alternative),
        ],
        discriminator=("kind",),
    )


def test_scheme_root_active_intersection_difference_and_explicit_union() -> None:
    expression, alternative = _expressions()
    actual = compile_allowed(expression)
    assert actual.qualified and actual.values == (EX.A,)
    assert set(actual.excluded) == {EX.Root, EX.B, EX.C, EX.D}
    union = compile_allowed(
        AllowedUnion((actual, alternative, ValueEnumeration((EX.extra,))))
    )
    assert union.qualified and set(union.values) == {EX.A, EX.C, EX.D, EX.extra}
    assert any(child.scope == "skos.concept_subset" for child in actual.report.children)  # type: ignore[union-attr]
    lexical = Literal("01", datatype=XSD.integer, normalize=False)
    other = Literal("1", datatype=XSD.integer, normalize=False)
    exact = compile_allowed(
        AllowedDifference(
            ValueEnumeration((lexical, other)), ValueEnumeration((other,))
        )
    )
    assert exact.qualified and exact.values == (lexical,) and exact.values[0] is lexical


def test_open_absence_never_becomes_difference_exclusion_or_closed_shacl_list() -> None:
    left = ValueEnumeration((EX.A, EX.B))
    opened = ValueEnumeration((EX.B,), excluded=(EX.C,), closed=False)
    difference = compile_allowed(AllowedDifference(left, opened))
    assert not difference.qualified and not difference.closed and not difference.values
    assert set(difference.excluded) == {EX.B, EX.C}
    constraints = ValueConstraints(enumeration=difference)
    assert application_matches(constraints, EX.A, budget=Budget()) is None
    assert application_matches(constraints, EX.B, budget=Budget()) is False
    known = compile_allowed(
        AllowedDifference(
            left, ValueEnumeration((EX.B,), excluded=(EX.A,), closed=False)
        )
    )
    assert known.values == (EX.A,) and not known.qualified
    selection = select_binding(
        compile_bindings([BindingCase("open", "open", known)], discriminator=("kind",)),
        {"kind": "open"},
    )
    assert selection.enumeration is not None and not selection.enumeration.qualified
    with pytest.raises(InputError):
        selection.closed_values()
    partial = compile_allowed(AllowedIntersection((left, opened)))
    assert partial.values == (EX.B,) and not partial.qualified


def test_actual_captured_trait_is_required_and_cannot_fall_back_or_be_replaced() -> (
    None
):
    plan = _plan()
    assert plan.report.verified
    original = {"kind": "rooted", "choice": str(EX.A)}
    selected = select_binding(plan, original)
    assert selected.report.verified and selected.case == selected.trait == "rooted"
    assert selected.closed_values() == (EX.A,)
    original["kind"] = "alternative"
    first_copy = selected.document
    assert isinstance(first_copy, dict)
    assert first_copy["kind"] == "rooted"
    first_copy["kind"] = "edited"
    assert selected.document == {"kind": "rooted", "choice": str(EX.A)}
    invalid_cases: tuple[dict[str, object], ...] = (
        {},
        {"kind": ["rooted", "alternative"]},
        {"kind": {}},
        {"kind": 1},
        {"kind": URIRef("rooted")},
    )
    for invalid in invalid_cases:
        result = select_binding(plan, invalid)
        assert result.report.outcome == "unverified" and result.enumeration is None
    no_case = select_binding(plan, {"kind": "ROOTED"})
    assert no_case.report.outcome == "disagreement" and no_case.report.checked == 1
    assert no_case.enumeration is None and not any(
        i.code == "input.empty_scope" for i in no_case.report
    )
    with pytest.raises(InputError):
        replace(plan, discriminator=("choice",))
    with pytest.raises(InputError):
        replace(selected, enumeration=ValueEnumeration((EX.D,)))
    duplicate = compile_bindings(
        [
            BindingCase("first", "x", ValueEnumeration((EX.A,))),
            BindingCase("second", "x", ValueEnumeration((EX.D,))),
        ],
        discriminator=("kind",),
    )
    assert (
        duplicate.report.outcome == "input_error"
        and not select_binding(duplicate, {"kind": "x"}).report.verified
    )
    wrong_kind = compile_bindings(
        [BindingCase("one", cast(str, Literal("x")), ValueEnumeration((EX.A,)))],
        discriminator=("kind",),
    )
    assert wrong_kind.report.outcome == "input_error"
    nested = select_binding(
        compile_bindings(
            [BindingCase("rooted", "rooted", ValueEnumeration((EX.A,)))],
            discriminator=("meta", "a/~"),
        ),
        {"meta": {"a/~": ["rooted", "alternative"]}},
    )
    assert any(i.location == "/meta/a~1~0" for i in nested.report)


def test_same_selected_enumeration_drives_actual_output_rdf_and_shacl() -> None:
    plan = _plan()
    for kind, value, valid in (
        ("rooted", EX.A, True),
        ("rooted", EX.D, False),
        ("alternative", EX.D, True),
    ):
        record = {
            "@context": {"@vocab": str(EX), "choice": {"@type": "@id"}},
            "@id": str(EX.record),
            "@type": "Record",
            "kind": kind,
            "choice": str(value),
        }
        chosen = select_binding(plan, record)
        assert chosen.report.verified and chosen.enumeration is not None
        field = FieldDeclaration(
            "choice",
            str(EX.choice),
            (
                ValueDeclaration(
                    "iri", constraints=ValueConstraints(enumeration=chosen.enumeration)
                ),
            ),
        )
        model = ModelDeclaration(
            "record",
            (str(EX.Record),),
            (
                field,
                FieldDeclaration(
                    "kind",
                    str(EX.kind),
                    (ValueDeclaration("literal", datatype=str(XSD.string)),),
                ),
            ),
            emission_groups=(
                EmissionGroup(
                    "selected_binding",
                    ("choice", "kind"),
                    2,
                    2,
                    (
                        EmissionScenario(
                            "selected",
                            ("choice", "kind"),
                            EmissionCondition("kind", Literal(kind)),
                        ),
                    ),
                    closed=True,
                ),
            ),
        )
        data = (
            Graph()
            .add((EX.record, RDF.type, EX.Record))
            .add((EX.record, EX.choice, value))
            .add((EX.record, EX.kind, Literal(kind)))
        )
        native = check_rdf_emission(
            prepare_source(data), (model,), (RdfModelUsage("record", EX.record),)
        )
        assert native.verified is valid, native.to_dict()
        if importlib.util.find_spec("pyld") is not None:
            output = check_emission(chosen.document, (model,), (ModelUsage("record"),))
            assert output.verified is valid, output.to_dict()
        shapes = compile_shapes(
            (
                ShaclNodeShape(
                    EX.shape,
                    properties=(
                        ShaclShapeRef(EX.property),
                        ShaclShapeRef(EX.trait_property),
                    ),
                    target_nodes=(EX.record,),
                ),
                ShaclPropertyShape(
                    EX.property,
                    EX.choice,
                    (ShaclValue(in_values=chosen.closed_values()), ShaclCount(1, 1)),
                ),
                ShaclPropertyShape(
                    EX.trait_property,
                    EX.kind,
                    (
                        ShaclValue(datatype=str(XSD.string), has_value=Literal(kind)),
                        ShaclCount(1, 1),
                    ),
                ),
            )
        )
        if importlib.util.find_spec("pyshacl") is not None:
            actual = validate_shapes(data, shapes, preflight=True)
            assert actual.report.verified is valid, actual.report.to_dict()
        if kind == "rooted" and valid:
            data.set((EX.record, EX.kind, Literal("alternative")))
            stale = check_rdf_emission(
                prepare_source(data), (model,), (RdfModelUsage("record", EX.record),)
            )
            assert stale.outcome == "disagreement"
            if importlib.util.find_spec("pyld") is not None:
                changed = chosen.document
                assert isinstance(changed, dict)
                changed["kind"] = "alternative"
                assert (
                    check_emission(changed, (model,), (ModelUsage("record"),)).outcome
                    == "disagreement"
                )
            if importlib.util.find_spec("pyshacl") is not None:
                assert not validate_shapes(data, shapes, preflight=True).report.verified


def test_required_reports_prefixes_metadata_bounds_cumulative_budget_and_recovery() -> (
    None
):
    issue = Issue("example.required", "Required source finding")
    source = ValueEnumeration(
        (EX.B,), closed=False, report=Report("required_source", 1, (issue,))
    )
    expression = AllowedDifference(ValueEnumeration((EX.A, EX.B)), source)
    baseline = Budget()
    result = compile_allowed(expression, budget=baseline)
    assert (
        result.report is not None
        and issue in result.report.issues
        and not result.qualified
    )
    work = baseline.limits.max_work - baseline.remaining("work")
    paid = 0
    for cap in range(1, work, max(1, work // 30)):
        budget = Budget(Limits(max_work=cap))
        current = compile_allowed(expression, budget=budget)
        assert current.report is not None and current.report.outcome == "exhausted"
        if id(issue) in budget._retained_issues:
            assert issue in current.report.issues
            paid += 1
    assert paid > 0
    giant = compile_allowed(
        ValueEnumeration((EX.A,), report=Report("x" * 100000, 1)),
        limits=Limits(max_bytes=1000),
    )
    assert giant.report is not None and giant.report.outcome == "exhausted"
    assert all(len(child.scope) < 1000 for child in giant.report.children)
    budget = Budget()
    assert compile_allowed(ValueEnumeration((EX.A,)), budget=budget).qualified
    budget.consume("work", budget.remaining("work"))
    assert not compile_allowed(ValueEnumeration((EX.A,)), budget=budget).qualified
    plan = _plan()
    limited = select_binding(
        plan, {"kind": "x" * 100000}, limits=Limits(max_bytes=1000)
    )
    assert limited.report.outcome == "exhausted" and limited.document is None
    assert select_binding(plan, {"kind": "rooted"}).report.verified
    cases = [
        BindingCase("first", "first", ValueEnumeration((EX.A,))),
        BindingCase("second", "second", ValueEnumeration((EX.D,))),
    ]
    missing_prefix = 0
    for cap in range(1, 1800, 20):
        partial = compile_bindings(
            cases, discriminator=("kind",), budget=Budget(Limits(max_work=cap))
        )
        if len(partial.cases) == 1 and not partial.report.verified:
            selected = select_binding(partial, {"kind": "second"})
            assert not selected.report.verified and selected.enumeration is None
            assert not any(
                issue.code == "vocabulary.binding_no_case" for issue in selected.report
            )
            missing_prefix += 1
    assert missing_prefix > 0
