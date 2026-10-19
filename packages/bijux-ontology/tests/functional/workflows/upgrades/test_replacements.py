"""Approved plans update actual contracts/data together and require real rechecks."""

from dataclasses import replace

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.data.query_rules import QueryRule, run_query_rules
from bijux_ontology.declarations.resource_roles import ResourceRole
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import Report
from bijux_ontology.skos.subsets import select_concepts
from bijux_ontology.vocabulary.facts.types import FactPredicate
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.registry.types import (
    IdentifierRegistry,
    NamespaceContribution,
)
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.upgrades.replacements import (
    ApprovedReplacement,
    ReplacementCheck,
    ReplacementPlan,
    ReplacementSite,
    ReplacementState,
    plan_replacements,
)

E = Namespace("urn:replacement:")
OWNERS = IdentifierRegistry((NamespaceContribution(str(E), "ex", "example"),))


def _vocabulary() -> Vocabulary:
    """Declare selected kinds explicitly rather than borrowing replacement hints."""
    graph = Graph()
    for node in (E.OldClass, E.NewClass, E.OtherClass):
        graph.add((node, RDF.type, OWL.Class))
    for node in (E.old, E.new, E.other):
        graph.add((node, RDF.type, OWL.DatatypeProperty))
        graph.add((node, RDFS.range, XSD.integer))
    return load_vocabulary(graph)


def _model() -> ModelDeclaration:
    """Use RDF presence plus exact literal checks, independent of JSON omission."""
    return ModelDeclaration(
        "record",
        (str(E.OldClass),),
        (
            FieldDeclaration(
                "count",
                str(E.old),
                (ValueDeclaration("literal", datatype=str(XSD.integer)),),
                emission="guaranteed",
            ),
        ),
    )


def _check(state: ReplacementState) -> Report:
    """The ordinary output engine receives this side's actual updated model/data."""
    return check_rdf_emission(
        state.source, state.models, (RdfModelUsage("record", E.record),)
    )


def test_one_to_one_changes_contract_and_actual_data_with_required_consumer_check() -> (
    None
):
    """Native emission and a real maintained ASK consumer both inspect the candidate."""
    graph = (
        Graph()
        .add((E.record, RDF.type, E.OldClass))
        .add((E.record, E.old, Literal("01", datatype=XSD.integer, normalize=False)))
    )
    source = prepare_rdf(graph)
    rules = (
        ApprovedReplacement((E.OldClass,), (E.NewClass,), "class", "example"),
        ApprovedReplacement((E.old,), (E.new,), "datatype_property", "example"),
    )

    def consumer(state: ReplacementState) -> Report:
        """Execute the selected application's actual query obligation."""
        return run_query_rules(
            state.source,
            (
                QueryRule(
                    "new-count",
                    f"ASK {{ <{E.record}> <{E.new}> ?value }}",
                    QueryScope("default"),
                    expected=True,
                ),
            ),
        ).report

    plan = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        rules,
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        before_checks=(ReplacementCheck("output", _check),),
        after_checks=(
            ReplacementCheck("output", _check),
            ReplacementCheck("consumer", consumer),
        ),
    )
    assert plan.report.verified, plan.report
    assert plan.after is not None and plan.candidate.source is not None
    assert plan.after.models[0].types == (str(E.NewClass),)
    assert plan.after.models[0].fields[0].predicate == str(E.new)
    result = plan.candidate.source.graph()
    assert (E.record, RDF.type, E.NewClass) in result
    assert str(next(result.objects(E.record, E.new))) == "01"
    assert (E.record, E.old, None) in graph and (E.record, E.new, None) not in graph
    assert _model().fields[0].predicate == str(E.old)
    assert export_migration(plan.candidate, format="turtle").content is None
    exported = export_migration(plan.candidate, accept=True, format="turtle")
    assert exported.report.verified and exported.content
    bad = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (replace(rules[0], targets=(E.new,)),),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert bad.report.outcome == "disagreement" and bad.candidate.source is None


def test_splits_use_original_discriminators_or_remain_unresolved() -> None:
    """Absent, ambiguous and unapproved answers never become guessed replacements."""
    source = prepare_rdf(
        Graph()
        .add((E.record, RDF.type, E.OldClass))
        .add((E.record, E.old, Literal(1)))
        .add((E.record, E.discriminator, E.Right))
    )

    def choose(site: ReplacementSite) -> URIRef | None:
        """Select from the actual immutable original snapshot, including its subject."""
        if site.quad is not None:
            assert site.quad.value == E.OldClass
        if not any(
            quad.predicate == E.discriminator and quad.value == E.Right
            for quad in site.snapshot.quads
        ):
            return None
        if site.quad is not None:
            assert any(
                q.subject == site.quad.subject
                and q.predicate == E.discriminator
                and q.value == E.Right
                for q in site.snapshot.quads
            )
        return E.NewClass

    rule = ApprovedReplacement(
        (E.OldClass,), (E.NewClass, E.OtherClass), "class", "example", choose=choose
    )
    valid = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (rule,),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert valid.report.verified, valid.report
    missing_graph = source.graph()
    missing_graph.remove((None, E.discriminator, None))
    missing_data = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (rule,),
        ownership=OWNERS,
        data=prepare_rdf(missing_graph),
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert missing_data.report.outcome == "unverified"
    assert missing_data.candidate.source is None
    for decision in (
        None,
        lambda site: None,
        lambda site: (E.NewClass, E.OtherClass),
        lambda site: E.Unapproved,
    ):
        unresolved = plan_replacements(
            _vocabulary(),
            _vocabulary(),
            (replace(rule, choose=decision),),
            ownership=OWNERS,
            data=source,
            models=(_model(),),
            after_checks=(ReplacementCheck("output", _check),),
        )
        assert (
            unresolved.report.outcome == "unverified"
            and unresolved.candidate.source is None
        )
    none = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert none.report.outcome == "unverified" and not none.decisions


def test_explicit_merges_namespace_authority_and_failed_postconditions() -> None:
    """
    Actual joins need approval and failed native contracts prevent candidate acceptance.
    """
    source = prepare_rdf(
        Graph()
        .add((E.record, RDF.type, E.OldClass))
        .add((E.record, E.old, Literal(1)))
        .add((E.record, E.other, Literal(2)))
    )
    rule = ApprovedReplacement(
        (E.old, E.other), (E.new,), "datatype_property", "example"
    )
    for approvals in (
        (rule,),
        (replace(rule, sources=(E.old,)), replace(rule, sources=(E.other,))),
    ):
        unresolved = plan_replacements(
            _vocabulary(),
            _vocabulary(),
            approvals,
            ownership=OWNERS,
            data=source,
            models=(_model(),),
            after_checks=(ReplacementCheck("output", _check),),
        )
        assert (
            unresolved.report.outcome == "unverified"
            and unresolved.candidate.source is None
        )
    merged = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (replace(rule, merge=True),),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert merged.report.verified, merged.report
    assert merged.candidate.source is not None
    assert len(tuple(merged.candidate.source.graph().objects(E.record, E.new))) == 2
    wrong_owner = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (replace(rule, merge=True, owner="someone-else"),),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert wrong_owner.report.outcome == "unverified"

    def wrong_contract(state: ReplacementState) -> Report:
        """A check of the old predicate must fail on the actual changed data."""
        return _check(replace(state, models=(_model(),)))

    rejected = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (replace(rule, merge=True),),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("old-consumer", wrong_contract),),
    )
    assert rejected.report.outcome == "disagreement"
    assert (
        export_migration(rejected.candidate, accept=True, format="turtle").content
        is None
    )
    missing = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (replace(rule, merge=True),),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
    )
    assert missing.report.outcome == "unverified"


def test_punned_roles_owned_blank_and_pinned_physical_scope() -> None:
    """
    Class replacement leaves the separate individual subject and unrelated graph intact.
    """
    dataset = Dataset()
    shared = BNode()
    dataset.graph(E.selected).add((E.OldClass, RDF.type, E.OldClass))
    dataset.graph(E.selected).add((E.record, RDF.type, E.OldClass))
    dataset.graph(E.selected).add((E.record, E.old, Literal(1)))
    dataset.graph(E.selected).add((E.record, E.link, shared))
    dataset.graph(E.selected).add((shared, E.link, shared))
    dataset.graph(E.hidden).add((E.hiddenRecord, RDF.type, E.OldClass))
    dataset.graph(E.empty)
    source = prepare_rdf(RdfSource(dataset, graph_name=str(E.selected)))
    plan = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (ApprovedReplacement((E.OldClass,), (E.NewClass,), "class", "example"),),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert plan.report.verified, plan.report
    assert plan.candidate.source is not None
    graph = plan.candidate.source.graph()
    assert (E.OldClass, RDF.type, E.NewClass) in graph
    child = next(graph.objects(E.record, E.link))
    assert (child, E.link, child) in graph and child is not shared
    assert (E.hiddenRecord, RDF.type, E.OldClass) not in graph
    assert (E.hiddenRecord, RDF.type, E.OldClass) in dataset.graph(E.hidden)
    ambiguous = prepare_rdf(Graph().add((E.record, E.custom, E.OldClass)))
    unresolved = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (ApprovedReplacement((E.OldClass,), (E.NewClass,), "class", "example"),),
        ownership=OWNERS,
        data=ambiguous,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
    )
    assert not unresolved.report.verified and unresolved.candidate.source is None


def test_limits_and_typed_intent_fail_closed() -> None:
    """Bounds and missing callback coverage cannot leave an accepted partial patch."""
    source = prepare_rdf(
        Graph().add((E.record, RDF.type, E.OldClass)).add((E.record, E.old, Literal(1)))
    )
    rule = ApprovedReplacement((E.old,), (E.new,), "datatype_property", "example")
    for work in (1, 100, 1000):
        limited = plan_replacements(
            _vocabulary(),
            _vocabulary(),
            (rule,),
            ownership=OWNERS,
            data=source,
            models=(_model(),),
            after_checks=(ReplacementCheck("output", _check),),
            limits=Limits(max_work=work),
        )
        assert (
            limited.report.outcome == "exhausted" and limited.candidate.source is None
        )
    with pytest.raises(InputError):
        ApprovedReplacement((E.old, E.other), (E.new, E.third), "property", "example")
    oversized_location = replace(
        _model(),
        identifier="m" * 600,
        fields=(replace(_model().fields[0], name="f" * 600),),
    )
    bounded = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (rule,),
        ownership=OWNERS,
        data=source,
        models=(oversized_location,),
        after_checks=(ReplacementCheck("output", _check),),
        limits=Limits(max_bytes=1000),
    )
    assert bounded.report.outcome == "exhausted" and bounded.candidate.source is None
    assert not bounded.decisions and all(not issue.location for issue in bounded.report)
    budget = Budget(Limits(max_work=100000))
    result = plan_replacements(
        _vocabulary(),
        _vocabulary(),
        (rule,),
        ownership=OWNERS,
        data=source,
        models=(_model(),),
        after_checks=(ReplacementCheck("output", _check),),
        budget=budget,
    )
    assert result.report.verified and budget.remaining("work") < budget.limits.max_work


def test_controlled_membership_requires_fresh_selection() -> None:
    """Plain allowed sets change; dynamic membership needs fresh qualification."""
    graph = _vocabulary().local
    graph.add((E.choice, RDF.type, OWL.ObjectProperty))
    graph.add((E.Scheme, RDF.type, SKOS.ConceptScheme))
    for concept in (E.OldConcept, E.NewConcept):
        graph.add((concept, RDF.type, SKOS.Concept))
    graph.add((E.OldConcept, SKOS.inScheme, E.Scheme))
    graph.add((E.ParentConcept, RDF.type, SKOS.Concept))
    vocabulary = load_vocabulary(graph)
    field = FieldDeclaration(
        "choice",
        str(E.choice),
        (ValueDeclaration("iri", role=ResourceRole("concept", punning="allow")),),
        emission="guaranteed",
    )
    subset = select_concepts(
        inspect_vocabulary(vocabulary),
        scheme=str(E.Scheme),
        closed=True,
        deprecation="allow",
    )
    assert subset.report.verified
    source = prepare_rdf(
        Graph()
        .add((E.record, RDF.type, E.OldClass))
        .add((E.record, E.choice, E.OldConcept))
    )
    rule = ApprovedReplacement((E.OldConcept,), (E.NewConcept,), "concept", "example")

    def actual(state: ReplacementState) -> Report:
        """Use ordinary semantic output checks with the actual target vocabulary."""
        return check_rdf_emission(
            state.source,
            state.models,
            (RdfModelUsage("record", E.record),),
            vocabulary=vocabulary,
        )

    dynamic = ModelDeclaration("record", (str(E.OldClass),), (subset.bind(field),))
    blocked = plan_replacements(
        vocabulary,
        vocabulary,
        (rule,),
        ownership=OWNERS,
        data=source,
        models=(dynamic,),
        endpoint_roles=(FactPredicate(str(E.choice), object_role="concept"),),
        after_checks=(ReplacementCheck("allowed", actual),),
    )
    assert not blocked.report.verified and blocked.candidate.source is None
    plain_field = replace(
        field,
        values=(
            replace(
                field.values[0],
                constraints=ValueConstraints(enumeration=(E.OldConcept,)),
            ),
        ),
    )
    plain = replace(dynamic, fields=(plain_field,))
    approved = plan_replacements(
        vocabulary,
        vocabulary,
        (rule,),
        ownership=OWNERS,
        data=source,
        models=(plain,),
        endpoint_roles=(FactPredicate(str(E.choice), object_role="concept"),),
        after_checks=(ReplacementCheck("allowed", actual),),
    )
    assert approved.report.verified, approved.report
    assert approved.after is not None
    constraints = approved.after.models[0].fields[0].values[0].constraints
    assert constraints is not None and constraints.enumeration == (E.NewConcept,)

    skos_data = prepare_rdf(
        Graph()
        .add((E.OldConcept, RDF.type, SKOS.Concept))
        .add((E.OldConcept, SKOS.broader, E.ParentConcept))
        .add((E.OldConcept, RDF.type, E.OldClass))
    )

    def broader_contract(state: ReplacementState) -> Report:
        """The maintained query checks actual standard concept occurrences."""
        return run_query_rules(
            state.source,
            (
                QueryRule(
                    "new-broader",
                    f"ASK {{ <{E.NewConcept}> <{SKOS.broader}> <{E.ParentConcept}> }}",
                    QueryScope("default"),
                    expected=True,
                ),
                QueryRule(
                    "old-individual-type",
                    f"ASK {{ <{E.OldConcept}> a <{E.OldClass}> }}",
                    QueryScope("default"),
                    expected=True,
                ),
            ),
        ).report

    standard = plan_replacements(
        vocabulary,
        vocabulary,
        (rule,),
        ownership=OWNERS,
        data=skos_data,
        after_checks=(ReplacementCheck("standard-concept", broader_contract),),
    )
    assert standard.report.verified, standard.report
    assert standard.candidate.source is not None
    assert (E.NewConcept, RDF.type, SKOS.Concept) in standard.candidate.source.graph()


def test_generic_property_approval_preserves_established_typed_facets() -> None:
    """A passing ASK cannot authorize a different established property kind."""
    graph = (
        Graph()
        .add((E.old, RDF.type, OWL.ObjectProperty))
        .add((E.new, RDF.type, OWL.ObjectProperty))
    )
    before = load_vocabulary(graph)
    source = prepare_rdf(Graph().add((E.record, E.old, E.object)))
    rule = ApprovedReplacement((E.old,), (E.new,), "property", "example")

    def consumer(state: ReplacementState) -> Report:
        """The native query deliberately checks the transformed predicate only."""
        return run_query_rules(
            state.source,
            (
                QueryRule(
                    "actual-link",
                    f"ASK {{ <{E.record}> <{E.new}> <{E.object}> }}",
                    QueryScope("default"),
                    expected=True,
                ),
            ),
        ).report

    def plan(after: Vocabulary, limits: Limits | None = None) -> ReplacementPlan:
        """Use explicit approval and the same actual consumer on each target schema."""
        return plan_replacements(
            before,
            after,
            (rule,),
            ownership=OWNERS,
            data=source,
            after_checks=(ReplacementCheck("native-query", consumer),),
            limits=limits,
        )

    assert plan(before).report.verified
    wrong = load_vocabulary(Graph().add((E.new, RDF.type, OWL.DatatypeProperty)))
    rejected = plan(wrong)
    assert (
        rejected.report.outcome == "disagreement" and rejected.candidate.source is None
    )
    assert any(
        issue.code == "upgrade.replacement_kind_mismatch" for issue in rejected.report
    )
    limited = plan(wrong, Limits(max_issues=1))
    assert any(issue.kind == "finding" for issue in limited.report)
    generic = load_vocabulary(Graph().add((E.new, RDF.type, RDF.Property)))
    assert plan(generic).report.outcome == "unverified"
