"""Selected SKOS integrity conditions stay distinct from authoring policy."""

import sys
from types import FrameType

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, SKOS, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.skos.checking import XL, check_skos
from bijux_ontology.skos.resource_facts import ResourceFacts
from bijux_ontology.skos.xl import (
    describe_xl_label,
    describe_xl_label_from_facts,
    xl_labels,
    xl_labels_from_facts,
)
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/")


def test_preferred_language_uniqueness_and_label_roles_use_rdf_term_identity() -> None:
    graph = Graph().add((EX.A, RDF.type, SKOS.Concept))
    graph.add((EX.A, SKOS.prefLabel, Literal("Name", lang="en")))
    graph.add((EX.A, SKOS.prefLabel, Literal("Namn", lang="sv")))
    graph.add((EX.B, SKOS.prefLabel, Literal("Name", lang="en")))
    assert check_skos(graph).verified
    graph.add((EX.A, SKOS.prefLabel, Literal("Other", lang="EN")))
    graph.add((EX.A, SKOS.altLabel, Literal("Name", lang="en")))
    assert {issue.code for issue in check_skos(graph).issues} == {
        "skos.preferred_label_conflict",
        "skos.label_role_conflict",
    }
    representation = Graph().add((EX.A, SKOS.prefLabel, URIRef(str(EX.name))))
    assert (
        check_skos(representation, rules=("label-integrity",)).outcome == "unverified"
    )
    assert (
        check_skos(representation, rules=("lexical-labels",)).issues[0].code
        == "skos.policy.label_representation"
    )


def test_known_role_conflicts_follow_supported_implications_and_scope() -> None:
    graph = Graph().add((EX.Collection, RDF.type, SKOS.Collection))
    graph.add((EX.A, SKOS.broader, EX.Collection))
    graph.add((EX.Valid, RDF.type, SKOS.Concept))
    assert any(
        issue.code == "skos.resource_role_conflict"
        for issue in check_skos(graph).issues
    )
    assert check_skos(graph, concepts=(str(EX.Valid),)).verified
    xl = Graph().add((EX.Label, RDF.type, XL.Label))
    xl.add((EX.Label, SKOS.inScheme, EX.Scheme))
    # inScheme intentionally has no Concept domain: labels may belong to schemes.
    assert check_skos(xl, rules=("resource-integrity",)).verified


def test_related_hierarchy_paths_and_optional_cycle_policy() -> None:
    graph = Graph()
    graph.add((EX.A, SKOS.broader, EX.B))
    graph.add((EX.C, SKOS.narrower, EX.B))
    graph.add((EX.C, SKOS.related, EX.A))
    report = check_skos(graph)
    issue = next(
        issue
        for issue in report.issues
        if issue.code == "skos.related_hierarchy_conflict"
    )
    assert issue.actual == " -> ".join(map(str, (EX.A, EX.B, EX.C)))
    cyclic = Graph().add((EX.A, SKOS.broader, EX.B))
    cyclic.add((EX.B, SKOS.broader, EX.A))
    assert check_skos(cyclic).verified
    assert {issue.code for issue in check_skos(cyclic, rules=("acyclic",)).issues} == {
        "skos.policy.hierarchy_cycle"
    }


def test_exact_mapping_closure_is_distinct_from_nontransitive_close_matches() -> None:
    graph = Graph().add((EX.A, SKOS.exactMatch, EX.B))
    graph.add((EX.B, SKOS.exactMatch, EX.C))
    graph.add((EX.C, SKOS.narrowMatch, EX.A))
    assert any(
        issue.code == "skos.mapping_conflict" for issue in check_skos(graph).issues
    )
    close = Graph().add((EX.A, SKOS.closeMatch, EX.B))
    close.add((EX.B, SKOS.closeMatch, EX.C))
    close.add((EX.A, SKOS.broadMatch, EX.C))
    assert check_skos(close).verified


def test_ordered_collection_members_need_no_redundant_member_assertions() -> None:
    graph = Graph().add((EX.Collection, RDF.type, SKOS.OrderedCollection))
    head = BNode()
    Collection(graph, head, [EX.A, EX.B])
    graph.add((EX.Collection, SKOS.memberList, head))
    assert check_skos(graph, rules=("collections",)).verified
    incompatible = Graph().add((EX.Collection, RDF.type, SKOS.OrderedCollection))
    incompatible.add((EX.Scheme, RDF.type, SKOS.ConceptScheme))
    invalid_head = BNode()
    Collection(incompatible, invalid_head, [EX.Scheme])
    incompatible.add((EX.Collection, SKOS.memberList, invalid_head))
    assert check_skos(incompatible, rules=("collections",)).issues[0].code == (
        "skos.collection_member_role_conflict"
    )
    graph.add((EX.Collection, SKOS.memberList, BNode()))
    report = check_skos(graph, rules=("collections",))
    assert any(
        issue.code == "skos.policy.member_list_ambiguous" and issue.kind == "unverified"
        for issue in report.issues
    )


def test_publication_languages_schemes_notation_and_deprecation_are_explicit() -> None:
    graph = Graph()
    for node in (EX.A, EX.B):
        graph.add((node, RDF.type, SKOS.Concept))
        graph.add((node, SKOS.inScheme, EX.Scheme))
        graph.add((node, SKOS.prefLabel, Literal("Name", lang="en")))
        graph.add((node, SKOS.notation, Literal("01", datatype=EX.Code)))
    graph.add((EX.A, SKOS.broader, EX.B))
    graph.add((EX.A, OWL.deprecated, Literal(True)))
    assert check_skos(graph).verified
    report = check_skos(
        graph,
        rules=("language-coverage", "notation-unique", "deprecated-active"),
        languages=("sv",),
        language_fallback=("en",),
    )
    assert {issue.code for issue in report.issues} == {
        "skos.policy.notation_collision",
        "skos.policy.deprecated_active",
    }
    missing_language = check_skos(
        graph, rules=("language-coverage",), languages=("sv",)
    )
    assert [issue.code for issue in missing_language.issues] == [
        "skos.policy.language_missing",
        "skos.policy.language_missing",
    ]
    graph.remove((EX.B, SKOS.notation, None))
    graph.add((EX.B, SKOS.notation, Literal("01", datatype=EX.OtherCode)))
    assert check_skos(graph, rules=("notation-unique",)).verified


def test_mapping_inventory_is_never_assumed_closed_and_work_limits_remain_partial() -> (
    None
):
    graph = Graph().add((EX.A, SKOS.exactMatch, EX.External))
    assert check_skos(graph).verified
    assert check_skos(graph, rules=("mapping-targets",)).outcome == "unverified"
    assert (
        check_skos(graph, rules=("mapping-targets",), closed_targets=True).outcome
        == "disagreement"
    )
    supplied = Vocabulary(
        graph, lookup=(Graph().add((EX.External, RDF.type, SKOS.Concept)),)
    )
    assert check_skos(
        supplied,
        scopes=("local", "lookup"),
        rules=("mapping-targets",),
        concepts=(str(EX.A),),
    ).verified
    report = check_skos(graph, limits=Limits(max_work=1))
    assert report.outcome == "exhausted" and not report.complete


def test_xl_projection_retains_label_resource_identity_and_missing_forms() -> None:
    graph = Graph().add((EX.A, RDF.type, SKOS.Concept))
    for label in (EX.Label, EX.OtherLabel):
        graph.add((EX.A, XL.prefLabel, label))
        graph.add((label, XL.literalForm, Literal("Name", lang="en")))
    assert check_skos(graph, rules=("xl", "label-integrity")).verified
    graph.add((EX.Label, EX.reviewedBy, Literal("curator")))
    labels = xl_labels(inspect_vocabulary(graph), EX.A)
    assert {label.resource for label in labels.items} == {EX.Label, EX.OtherLabel}
    assert any(
        edge.predicate == EX.reviewedBy
        for label in labels.items
        for edge in label.metadata
    )
    standalone = Graph().add((EX.Standalone, RDF.type, XL.Label))
    assert check_skos(standalone, rules=("xl",)).outcome == "unverified"
    assert (
        describe_xl_label(inspect_vocabulary(standalone), EX.Standalone).report.outcome
        == "unverified"
    )
    graph.add((EX.A, XL.altLabel, EX.Label))
    assert any(
        issue.code == "skos.xl_role_overlap"
        for issue in check_skos(graph, rules=("xl",)).issues
    )
    graph.remove((EX.A, XL.altLabel, EX.Label))
    assert (EX.Label, OWL.sameAs, EX.OtherLabel) not in graph
    graph.remove((EX.OtherLabel, XL.literalForm, None))
    assert check_skos(graph, rules=("xl",)).outcome == "unverified"
    graph.add((EX.Label, XL.literalForm, Literal("Other", lang="en")))
    assert any(
        issue.code == "skos.xl_form_conflict"
        for issue in check_skos(graph, rules=("xl",)).issues
    )


def test_collection_notation_scope_includes_ordered_members_and_literal_datatypes() -> (
    None
):
    graph = Graph().add((EX.Group, RDF.type, SKOS.OrderedCollection))
    head = BNode()
    Collection(graph, head, [EX.A, EX.B])
    graph.add((EX.Group, SKOS.memberList, head))
    for node in (EX.A, EX.B):
        graph.add((node, RDF.type, SKOS.Concept))
        graph.add((node, SKOS.notation, Literal("01", datatype=EX.Code)))
    assert check_skos(graph, rules=("notation-unique",)).outcome == "unverified"
    collided = check_skos(
        graph, rules=("notation-unique",), notation_collections=[str(EX.Group)]
    )
    assert [issue.code for issue in collided.issues] == [
        "skos.policy.notation_collision"
    ]
    assert collided.issues[0].location == str(EX.Group)
    graph.set((EX.B, SKOS.notation, Literal("01", datatype=EX.OtherCode)))
    assert check_skos(
        graph, rules=("notation-unique",), notation_collections=[str(EX.Group)]
    ).verified
    missing = check_skos(
        graph, rules=("notation-unique",), notation_collections=[str(EX.Missing)]
    )
    assert missing.outcome == "unverified"
    assert len(tuple(graph.objects(EX.Group, SKOS.member))) == 0
    graph.add((EX.Unrelated, RDF.type, SKOS.Concept))
    assert check_skos(
        graph, rules=("notation-required",), notation_collections=[str(EX.Group)]
    ).verified
    graph.remove((EX.A, SKOS.notation, None))
    required = check_skos(
        graph, rules=("notation-required",), notation_collections=[str(EX.Group)]
    )
    assert [(issue.code, issue.location) for issue in required.issues] == [
        ("skos.policy.notation_missing", str(EX.A))
    ]


def test_ordered_membership_reuses_deprecation_states_and_preserves_hints() -> None:
    graph = Graph().add((EX.Group, RDF.type, SKOS.OrderedCollection))
    head = BNode()
    Collection(graph, head, [EX.A])
    graph.add((EX.Group, SKOS.memberList, head))
    graph.add((EX.A, RDF.type, SKOS.Concept))
    graph.add(
        (EX.A, OWL.deprecated, Literal("1", datatype=XSD.boolean, normalize=False))
    )
    graph.add((EX.A, EX.replacedBy, EX.B))
    graph.add((EX.A, EX.replacedBy, EX.C))
    result = check_skos(
        graph, rules=("deprecated-active",), replacement_predicates=[str(EX.replacedBy)]
    )
    assert [issue.code for issue in result.issues] == ["skos.policy.deprecated_active"]
    assert result.issues[0].severity == "warning"
    assert result.issues[0].actual == f"{EX.B}, {EX.C}"
    graph.add((EX.A, OWL.deprecated, Literal(False)))
    conflicting = check_skos(graph, rules=("deprecated-active",))
    assert conflicting.outcome == "unverified"
    assert conflicting.issues[0].code == "skos.policy.deprecation_unverified"


def test_scheme_boundaries_are_opt_in_and_do_not_redefine_valid_skos() -> None:
    graph = Graph().add((EX.A, SKOS.broader, EX.B))
    graph.add((EX.A, SKOS.inScheme, EX.One))
    graph.add((EX.B, SKOS.inScheme, EX.Two))
    assert check_skos(graph).verified
    boundary = check_skos(graph, rules=("same-scheme-hierarchy",))
    assert [issue.code for issue in boundary.issues] == [
        "skos.policy.cross_scheme_hierarchy"
    ]
    graph.add((EX.A, SKOS.inScheme, EX.Two))
    multi = check_skos(graph, rules=("single-scheme",))
    assert [issue.code for issue in multi.issues] == ["skos.policy.multiple_schemes"]
    graph.remove((EX.B, SKOS.inScheme, None))
    missing = check_skos(graph, rules=("scheme-required",))
    assert [(issue.code, issue.location) for issue in missing.issues] == [
        ("skos.policy.scheme_missing", str(EX.B))
    ]


def test_collection_policy_retains_direct_literal_members_and_exact_selection() -> None:
    graph = Graph().add((EX.Group, SKOS.member, Literal("not a resource")))
    graph.add((EX.Valid, SKOS.member, EX.A))
    graph.add((EX.Valid, SKOS.member, BNode()))
    before = frozenset(graph)
    report = check_skos(graph, rules=("collections",))
    assert report.outcome == "disagreement"
    assert [(issue.code, issue.location) for issue in report.issues] == [
        ("skos.policy.collection_member_shape", str(EX.Group))
    ]
    assert check_skos(graph, rules=("collections",), concepts=(str(EX.Valid),)).verified
    assert frozenset(graph) == before


def test_selected_collection_retains_list_checks_without_unrelated_findings() -> None:
    graph = Graph().add((EX.Good, SKOS.member, EX.A))
    missing = BNode()
    graph.add((EX.Bad, SKOS.memberList, missing))
    assert check_skos(graph, concepts=(str(EX.Good),), rules=("collections",)).verified
    for concepts in ((), (str(EX.Bad),)):
        report = check_skos(graph, concepts=concepts, rules=("collections",))
        assert [(issue.code, issue.location) for issue in report.issues] == [
            ("skos.policy.member_list_malformed", str(EX.Bad))
        ]
    graph.add((EX.Selected, SKOS.memberList, missing))
    shared = check_skos(
        graph,
        concepts=(str(EX.Selected),),
        rules=("collections", "deprecated-active"),
    )
    assert [(issue.code, issue.location) for issue in shared.issues] == [
        ("skos.policy.member_list_malformed", str(EX.Selected))
    ]
    graph.add((EX.Selected, SKOS.memberList, RDF.nil))
    assert any(
        issue.code == "skos.policy.member_list_ambiguous"
        for issue in check_skos(
            graph, concepts=(str(EX.Selected),), rules=("collections",)
        ).issues
    )
    graph.set((EX.Selected, SKOS.memberList, Literal("invalid list head")))
    assert [
        issue.code
        for issue in check_skos(
            graph, concepts=(str(EX.Selected),), rules=("collections",)
        ).issues
    ] == ["skos.policy.member_list_malformed"]


def test_notation_collection_members_intersect_exact_and_scheme_selections() -> None:
    graph = Graph()
    head = BNode()
    Collection(graph, head, [EX.A, EX.B])
    graph.add((EX.Group, SKOS.memberList, head))
    graph.add((EX.A, RDF.type, SKOS.Concept))
    graph.add((EX.B, RDF.type, SKOS.Concept))
    graph.add((EX.A, SKOS.inScheme, EX.One))
    graph.add((EX.B, SKOS.inScheme, EX.Two))
    graph.add((EX.A, SKOS.notation, Literal("01")))
    for concepts, schemes in (
        ((str(EX.A),), ()),
        ((), (str(EX.One),)),
        ((str(EX.A), str(EX.B)), (str(EX.One),)),
    ):
        assert check_skos(
            graph,
            concepts=concepts,
            schemes=schemes,
            rules=("notation-required", "notation-unique"),
            notation_collections=(str(EX.Group),),
        ).verified
    for concepts in ((), (str(EX.B),)):
        report = check_skos(
            graph,
            concepts=concepts,
            rules=("notation-required",),
            notation_collections=(str(EX.Group),),
        )
        assert [(issue.code, issue.location) for issue in report.issues] == [
            ("skos.policy.notation_missing", str(EX.B))
        ]
    graph.add((EX.B, SKOS.notation, Literal("01")))
    assert check_skos(
        graph,
        concepts=(str(EX.A),),
        rules=("notation-required", "notation-unique"),
        notation_collections=(str(EX.Group),),
    ).verified
    assert [
        issue.code
        for issue in check_skos(
            graph,
            rules=("notation-unique",),
            notation_collections=(str(EX.Group),),
        ).issues
    ] == ["skos.policy.notation_collision"]


def test_selected_deprecated_member_uses_support_without_selecting_other_members() -> (
    None
):
    graph = Graph()
    head = BNode()
    Collection(graph, head, [EX.A, EX.B])
    graph.add((EX.Group, SKOS.memberList, head))
    graph.add((EX.Bad, SKOS.memberList, BNode()))
    for node in (EX.A, EX.B):
        graph.add((node, RDF.type, SKOS.Concept))
        graph.add((node, OWL.deprecated, Literal(True)))
    report = check_skos(graph, concepts=(str(EX.A),), rules=("deprecated-active",))
    assert [(issue.code, issue.location) for issue in report.issues] == [
        ("skos.policy.deprecated_active", str(EX.A))
    ]


def test_repeated_skos_checks_share_the_supplied_operation_budget() -> None:
    graph = Graph().add((EX.A, RDF.type, SKOS.Concept))
    graph.add((EX.A, SKOS.prefLabel, Literal("Name", lang="en")))
    view = inspect_vocabulary(graph)
    measured = Budget()
    assert check_skos(view, budget=measured).verified
    required_work = measured.limits.max_work - measured.remaining("work")
    shared = Budget(Limits(max_work=required_work))
    assert check_skos(view, budget=shared).verified
    assert check_skos(view, budget=shared).outcome == "exhausted"
    assert check_skos(view).verified
    with pytest.raises(InputError, match="matching limits"):
        check_skos(graph, limits=Limits(max_work=1), budget=Budget())


def _shared_xl_resources() -> Graph:
    """Two actual concept owners retain one label with distinct authored roles."""
    graph = Graph()
    for owner in (EX.A, EX.B):
        graph.add((owner, RDF.type, SKOS.Concept))
        graph.add((owner, XL.prefLabel, EX.Label))
    graph.add((EX.A, XL.altLabel, EX.Label))
    graph.add((EX.Label, RDF.type, XL.Label))
    graph.add((EX.Label, XL.literalForm, Literal("Name", lang="en")))
    return graph


def test_shared_xl_forms_are_read_once_without_merging_roles_or_plain_evidence() -> (
    None
):
    view = inspect_vocabulary(_shared_xl_resources())
    reads = []
    admitted: list[ResourceFacts] = []

    def observe_read(frame: FrameType, event: str, value: object) -> None:
        if (
            event == "return"
            and frame.f_code is ResourceFacts.label_statements.__code__
        ):
            facts = frame.f_locals.get("self")
            if isinstance(facts, ResourceFacts) and not admitted:
                admitted.append(facts)
        if event != "call" or frame.f_code is not VocabularyView.statements.__code__:
            return
        if frame.f_locals.get("node") == EX.Label and not frame.f_locals.get(
            "predicates"
        ):
            reads.append(frame.f_locals.get("budget"))

    budget = Budget()
    previous = sys.getprofile()
    sys.setprofile(observe_read)
    try:
        report = check_skos(view, rules=("xl", "label-integrity"), budget=budget)
    finally:
        sys.setprofile(previous)
    assert reads == [budget]
    assert len(admitted) == 1
    facts = admitted[0]
    foreign = Budget()
    for read in (
        lambda: facts.label_statements(EX.Label, foreign),
        lambda: describe_xl_label_from_facts(facts, EX.Label, budget=foreign),
        lambda: xl_labels_from_facts(facts, EX.A, budget=foreign),
    ):
        with pytest.raises(InputError, match="originating budget"):
            read()
    assert foreign.remaining("work") == foreign.limits.max_work
    assert [issue.code for issue in report.issues] == [
        "skos.xl_role_overlap",
        "skos.label_role_conflict",
    ]
    assert report.complete and not report.verified
    # An independent interpretation never adopts derived plain labels implicitly.
    assert check_skos(view, rules=("label-integrity",)).verified


def test_resource_fact_admission_rejects_native_view_reinitialization() -> None:
    view = inspect_vocabulary(_shared_xl_resources())
    observed: list[object] = []

    def observe_read(frame: FrameType, event: str, value: object) -> None:
        if event != "return" or frame.f_code is not VocabularyView.statements.__code__:
            return
        if (
            frame.f_locals.get("node") != EX.Label
            or frame.f_locals.get("predicates")
            or observed
        ):
            return
        observed.append(value)
        # Reinitialize through the actual native constructor after real admission.
        VocabularyView.__init__(view, Graph().add((EX.Other, RDF.type, SKOS.Concept)))

    previous = sys.getprofile()
    sys.setprofile(observe_read)
    try:
        with pytest.raises(InputError, match="view changed"):
            check_skos(view, rules=("xl", "label-integrity"))
    finally:
        sys.setprofile(previous)
    assert len(observed) == 1


def test_paid_xl_role_finding_survives_later_parent_work_exhaustion() -> None:
    view = inspect_vocabulary(_shared_xl_resources())
    budget = Budget()
    paid: list[Issue] = []

    def observe_paid(frame: FrameType, event: str, value: object) -> None:
        if event != "return" or frame.f_code is not IssueCollector.append.__code__:
            return
        issue = frame.f_locals.get("issue")
        if not isinstance(issue, Issue) or issue.code != "skos.xl_role_overlap" or paid:
            return
        paid.append(issue)
        budget.consume("work", budget.remaining("work"))

    previous = sys.getprofile()
    sys.setprofile(observe_paid)
    try:
        result = check_skos(view, rules=("xl", "label-integrity"), budget=budget)
    finally:
        sys.setprofile(previous)
    assert len(paid) == 1
    assert result.outcome == "exhausted" and not result.complete
    assert any(issue is paid[0] for issue in result.issues)
    assert {issue.code for issue in result.issues} == {
        "skos.xl_role_overlap",
        "execution.limit_exceeded",
    }
