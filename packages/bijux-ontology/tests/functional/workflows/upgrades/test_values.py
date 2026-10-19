"""Actual stored controlled values retain exact policy and release proof scope."""

from collections.abc import Sequence
from dataclasses import replace
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, SKOS, XSD

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.skos.subsets.selection import select_concepts
from bijux_ontology.skos.subsets.types import ConceptSubset
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.workflows.upgrades.values import (
    ControlledField,
    ValueReplacement,
    compare_controlled_values,
)

EX = Namespace("urn:controlled:")


def _vocabulary() -> Graph:
    """Declare actual concept roles, scheme membership and authored hierarchy."""
    graph = Graph().add((EX.scheme, RDF.type, SKOS.ConceptScheme))
    for node in (EX.root, EX.used, EX.unused):
        graph.add((node, RDF.type, SKOS.Concept))
        graph.add((node, SKOS.inScheme, EX.scheme))
        graph.add((node, OWL.deprecated, Literal(False)))
    graph.add((EX.used, SKOS.broader, EX.root))
    graph.add((EX.unused, SKOS.broader, EX.root))
    return graph


def _subset(graph: Graph) -> ConceptSubset:
    """Reuse the accepted actual rooted scheme selection rather than a fake list."""
    return select_concepts(
        inspect_vocabulary(graph),
        scheme=str(EX.scheme),
        roots=(str(EX.root),),
        closed=True,
    )


def _enum(subset: ConceptSubset) -> ValueEnumeration:
    """Keep the original required selection and exclusion evidence."""
    return ValueEnumeration(
        subset.values,
        excluded=subset.excluded,
        closed=subset.closed,
        report=subset.report,
    )


def test_root_or_scheme_move_attributes_used_records_without_unused_breaks() -> None:
    old = _vocabulary()
    current = _vocabulary()
    current.remove((EX.used, SKOS.broader, EX.root))
    current.add((EX.used, SKOS.broader, EX.outside))
    previous, following = _subset(old), _subset(current)
    assert previous.report.verified and following.report.verified
    data = prepare_rdf(
        Graph()
        .add((EX.r, EX.choice, EX.used))
        .add((EX.s, EX.choice, EX.root))
        .add((EX.r, RDF.type, EX.Record))
        .add((EX.s, RDF.type, EX.Record))
    )
    field = ControlledField("choice", str(EX.choice), _enum(previous), _enum(following))
    result = compare_controlled_values(data, (field,), (EX.r, EX.s))
    assert result.before.verified and result.after.outcome == "disagreement"
    assert result.demonstrated_break
    assert [(u.record, u.value) for u in result.uses if u.status == "introduced"] == [
        (EX.r, EX.used)
    ]
    assert result.before.children == (previous.report,)
    model_field = FieldDeclaration("choice", str(EX.choice), (ValueDeclaration("iri"),))
    before_model = ModelDeclaration(
        "record", (str(EX.Record),), (previous.bind(model_field),)
    )
    after_model = ModelDeclaration(
        "record", (str(EX.Record),), (following.bind(model_field),)
    )
    usage = (RdfModelUsage("record", EX.r), RdfModelUsage("record", EX.s))
    assert check_rdf_emission(data, (before_model,), usage).verified
    assert check_rdf_emission(data, (after_model,), usage).outcome == "disagreement"
    current = _vocabulary()
    current.remove((EX.unused, SKOS.inScheme, EX.scheme))
    current.add((EX.unused, SKOS.inScheme, EX.other_scheme))
    untouched = compare_controlled_values(
        data, (replace(field, after=_enum(_subset(current))),), (EX.r, EX.s)
    )
    assert untouched.report.verified and not untouched.demonstrated_break
    current.remove((EX.used, SKOS.inScheme, EX.scheme))
    current.add((EX.used, SKOS.inScheme, EX.other_scheme))
    moved = compare_controlled_values(
        data, (replace(field, after=_enum(_subset(current))),), (EX.r,)
    )
    assert moved.demonstrated_break


def test_retirement_follows_explicit_policy_and_replacements_are_advisory() -> None:
    old, current = _vocabulary(), _vocabulary()
    current.set((EX.used, OWL.deprecated, Literal(True)))
    data = prepare_rdf(Graph().add((EX.r, EX.choice, EX.used)))
    before = _enum(_subset(old))
    excluded = _enum(_subset(current))
    field = ControlledField("choice", str(EX.choice), before, excluded)
    result = compare_controlled_values(
        data,
        (field,),
        (EX.r,),
        replacements=(
            ValueReplacement(EX.used, EX.root),
            ValueReplacement(EX.used, EX.absent),
        ),
    )
    assert result.demonstrated_break and result.uses[0].status == "introduced"
    assert [item.admissible for item in result.replacements] == [True, False]
    assert (EX.r, EX.choice, EX.used) in data.graph()
    allowed = select_concepts(
        inspect_vocabulary(current),
        scheme=str(EX.scheme),
        closed=True,
        deprecation="allow",
    )
    assert compare_controlled_values(
        data, (replace(field, after=_enum(allowed)),), (EX.r,)
    ).report.verified
    current.remove((EX.used, OWL.deprecated, None))
    required = select_concepts(
        inspect_vocabulary(current),
        scheme=str(EX.scheme),
        closed=True,
        deprecation="require-active",
    )
    uncertain = compare_controlled_values(
        data, (replace(field, after=_enum(required)),), (EX.r,)
    )
    assert uncertain.report.outcome == "unverified" and not uncertain.demonstrated_break
    assert uncertain.uses[0].after is None


def test_open_or_partial_scope_and_missing_coverage_never_invent_exclusion() -> None:
    known = ValueEnumeration((EX.used,))
    data = prepare_rdf(Graph().add((EX.r, EX.choice, EX.used)))
    partial = Report("membership", 1, requested=2, complete=False)
    for after in (
        ValueEnumeration((), closed=False),
        ValueEnumeration((), report=partial),
    ):
        result = compare_controlled_values(
            data, (ControlledField("choice", str(EX.choice), known, after),), (EX.r,)
        )
        assert result.report.outcome == "unverified" and not result.demonstrated_break
        assert result.uses[0].after is None
        assert not any(i.kind == "finding" for i in result.after.issues)
    result = compare_controlled_values(
        data,
        (
            ControlledField(
                "choice",
                str(EX.choice),
                ValueEnumeration((), closed=False),
                ValueEnumeration(()),
            ),
        ),
        (EX.r,),
    )
    assert result.after.outcome == "disagreement" and not result.demonstrated_break
    field = ControlledField("choice", str(EX.choice), known, known)
    for fields, records in (
        ((), (EX.r,)),
        ((field,), ()),
        ((field,), (EX.missing,)),
        ((field,), (BNode("r"),)),
    ):
        missing = compare_controlled_values(data, fields, records)
        assert missing.report.outcome == "unverified" and not missing.report.verified
    mixed = compare_controlled_values(data, (field,), (EX.r, EX.missing))
    assert not mixed.before.verified and not mixed.after.verified
    with pytest.raises(InputError):
        compare_controlled_values(data, (field,), (EX.r, EX.r))
    with pytest.raises(InputError):
        compare_controlled_values(data, cast(Sequence[ControlledField], None), (EX.r,))


def test_actual_graph_direction_and_exact_literal_identity_survive_ownership() -> None:
    zero_one = Literal("01", datatype=XSD.integer, normalize=False)
    one = Literal("1", datatype=XSD.integer, normalize=False)
    dataset = Dataset()
    dataset.graph(EX.G).add((EX.r, EX.choice, zero_one))
    dataset.graph(EX.G).add((EX.member, EX.inverse, EX.r))
    dataset.graph(EX.H).add((EX.r, EX.choice, EX.hidden))
    data = prepare_rdf(RdfSource(dataset, graph_name=str(EX.G)))
    field = ControlledField(
        URIRef("choice"),
        EX.choice,
        ValueEnumeration((zero_one,)),
        ValueEnumeration((one,)),
    )
    reverse = ControlledField(
        "inverse",
        str(EX.inverse),
        ValueEnumeration((EX.member,)),
        ValueEnumeration((EX.member,)),
        reverse=True,
    )
    result = compare_controlled_values(data, (field, reverse), (EX.r,))
    assert len(result.uses) == 2 and result.demonstrated_break
    assert result.uses[0].graph == EX.G and term_key(result.uses[0].value) == term_key(
        zero_one
    )
    assert result.uses[1].statement == (EX.member, EX.inverse, EX.r)
    assert result.uses[1].before is True and result.uses[1].reverse
    assert len(dataset.graph(EX.H)) == 1 and data.graph_name == str(EX.G)
    dataset.default_graph.add((EX.r, EX.choice, zero_one))
    default = compare_controlled_values(
        prepare_rdf(dataset), (field,), (EX.r,), graph_name="@default"
    )
    assert default.uses[0].graph is None and default.uses[0].graph_kind == "default"
    graph_blank = BNode("graph")
    dataset.graph(graph_blank).add((EX.r, EX.choice, zero_one))
    owned = prepare_rdf(dataset)
    owned_name = next(
        name for name in owned.dataset().graphs() if isinstance(name.identifier, BNode)
    ).identifier
    actual_blank = compare_controlled_values(
        owned, (field,), (EX.r,), graph_name=owned_name
    )
    assert actual_blank.uses[0].graph is owned_name
    foreign = compare_controlled_values(
        owned, (field,), (EX.r,), graph_name=BNode(str(owned_name))
    )
    assert foreign.report.outcome == "unverified" and not foreign.uses
    strings = prepare_rdf(Graph().add((EX.r, EX.choice, Literal("same"))))
    alias = ControlledField(
        "choice",
        str(EX.choice),
        ValueEnumeration((Literal("same"),)),
        ValueEnumeration((Literal("same", datatype=XSD.string),)),
    )
    assert compare_controlled_values(strings, (alias,), (EX.r,)).report.verified


def test_bounded_prefix_reports_and_complete_locations_use_shared_allowance() -> None:
    data = prepare_rdf(
        Graph().add((EX.r, EX.choice, EX.used)).add((EX.s, EX.choice, EX.used))
    )
    field = ControlledField(
        "choice", str(EX.choice), ValueEnumeration((EX.used,)), ValueEnumeration(())
    )
    result = compare_controlled_values(
        data, (field,), (EX.r, EX.s), limits=Limits(max_issues=1)
    )
    assert result.report.outcome == "exhausted" and result.demonstrated_break
    assert any(i.code == "upgrade.value_inadmissible" for i in result.report.issues)
    assert result.uses[0].record == EX.r
    long_record = URIRef("urn:" + "x" * 600)
    long_field = replace(field, identifier="f" * 600)
    large = prepare_rdf(Graph().add((long_record, EX.choice, EX.used)))
    bounded = compare_controlled_values(
        large, (long_field,), (long_record,), limits=Limits(max_bytes=1000)
    )
    assert bounded.report.outcome == "exhausted" and not bounded.uses
    budget = Budget()
    complete = compare_controlled_values(data, (field,), (EX.r, EX.s), budget=budget)
    used = budget.limits.max_work - budget.remaining("work")
    prefixes = []
    paid_prefixes = []
    for cap in range(1, used + 1, max(1, used // 20)):
        cut = compare_controlled_values(
            data, (field,), (EX.r, EX.s), limits=Limits(max_work=cap)
        )
        assert cut.report.outcome in ("exhausted", "disagreement")
        if cut.report.outcome == "exhausted" and cut.uses:
            prefixes.append(cut)
            assert cut.demonstrated_break
            if any(i.code == "upgrade.value_inadmissible" for i in cut.report.issues):
                paid_prefixes.append(cut)
    assert prefixes and paid_prefixes and complete.demonstrated_break
