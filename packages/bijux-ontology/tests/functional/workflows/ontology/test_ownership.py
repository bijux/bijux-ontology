"""Finite ownership policies preserve legal reuse and locate protected drift."""

from dataclasses import replace

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.facts import FactPredicate, FactSelection
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.ontology.ownership import OwnerClaim, check_ownership

EX = Namespace("https://ownership.example/")


def _view(graph: Graph, name: URIRef | None = None) -> VocabularyView:
    return inspect_vocabulary(
        load_vocabulary(graph, graph_name=name), scopes=("local",)
    )


def _selection(*rules: FactPredicate) -> FactSelection:
    return FactSelection(
        rules or (FactPredicate(str(RDFS.subClassOf)),),
        entities=(str(EX.Term),),
        positions=("subject",),
    )


def test_editorial_reuse_and_located_protected_redefinition() -> None:
    owner = _view(Graph().add((EX.Term, RDFS.subClassOf, EX.Parent)))
    claim = OwnerClaim("upstream", owner, namespace=str(EX))
    graph = Graph().add((EX.Term, RDFS.label, Literal("application label")))
    assert check_ownership(
        _view(graph), _selection(), (claim,), publisher="application"
    ).report.verified
    graph.add((EX.Term, RDFS.subClassOf, EX.Parent))
    result = check_ownership(
        _view(graph), _selection(), (claim,), publisher="application"
    )
    assert result.report.verified and result.evidence[0].comparison is not None
    assert result.evidence[0].current[0].source_scope == "local"
    assert result.evidence[0].authoritative[0].statement == (
        EX.Term,
        RDFS.subClassOf,
        EX.Parent,
    )
    assert not result.evidence[0].comparison.differences
    graph.add((EX.Term, RDFS.subClassOf, EX.Other))
    failed = check_ownership(
        _view(graph), _selection(), (claim,), publisher="application"
    )
    assert failed.report.outcome == "disagreement"
    issues = [
        i for i in failed.report.issues if i.code == "ontology.definition_redefined"
    ]
    assert len(issues) == 1 and issues[0].location == str(EX.Term)
    assert issues[0].details is not None and issues[0].details.source == "local"
    assert check_ownership(
        _view(graph), _selection(), (claim,), publisher="upstream"
    ).report.verified


def test_explicit_conflicts_specificity_and_legal_punning_are_distinct() -> None:
    source = _view(Graph().add((EX.Term, RDFS.subClassOf, EX.Parent)))
    broad = OwnerClaim("upstream", source, namespace="https://ownership.example/")
    narrow = OwnerClaim("application", source, term=str(EX.Term))
    assert check_ownership(
        source, _selection(), (broad, narrow), publisher="application"
    ).report.verified
    namespace_owner = OwnerClaim("application", source, namespace=str(EX) + "T")
    assert check_ownership(
        source, _selection(), (broad, namespace_owner), publisher="application"
    ).report.verified
    conflict = replace(narrow, owner="another")
    result = check_ownership(
        source, _selection(), (broad, narrow, conflict), publisher="application"
    )
    assert [i.code for i in result.report.issues] == ["ontology.owner_conflict"]
    graph = Graph().add((EX.Term, RDF.type, OWL.Class))
    graph.add((EX.Term, RDF.type, EX.Class))
    view = _view(graph)
    claims = (
        OwnerClaim("class-owner", view, term=str(EX.Term), role="class"),
        OwnerClaim("individual-owner", view, term=str(EX.Term), role="individual"),
    )
    punning = check_ownership(
        view, _selection(FactPredicate(str(RDF.type))), claims, publisher="application"
    )
    assert punning.report.verified
    assert {e.role for e in punning.evidence} == {"class", "individual"}


def test_exact_literal_content_and_original_named_graphs_remain_distinct() -> None:
    owner_graph = Dataset()
    owner_graph.graph(EX.ownerGraph).add(
        (EX.Term, EX.definition, Literal("01", datatype=XSD.integer, normalize=False))
    )
    current_graph = Dataset()
    current_graph.graph(EX.currentGraph).add(
        (EX.Term, EX.definition, Literal("01", datatype=XSD.integer, normalize=False))
    )
    rule = FactPredicate(str(EX.definition), subject_role="class")
    claim = OwnerClaim("upstream", _view(owner_graph, EX.ownerGraph), term=str(EX.Term))
    result = check_ownership(
        _view(current_graph, EX.currentGraph),
        _selection(rule),
        (claim,),
        publisher="application",
    )
    assert result.report.verified
    assert result.evidence[0].current[0].graph.name == EX.currentGraph
    assert result.evidence[0].authoritative[0].graph.name == EX.ownerGraph
    current_graph.graph(EX.currentGraph).set(
        (EX.Term, EX.definition, Literal("1", datatype=XSD.integer, normalize=False))
    )
    assert (
        check_ownership(
            _view(current_graph, EX.currentGraph),
            _selection(rule),
            (claim,),
            publisher="application",
        ).report.outcome
        == "disagreement"
    )


def test_anonymous_missing_ownership_empty_selection_and_real_budget_cannot_pass() -> (
    None
):
    graph = Graph().add((EX.Term, RDFS.subClassOf, BNode()))
    view = _view(graph)
    claim = OwnerClaim("upstream", view, term=str(EX.Term))
    unknown = check_ownership(view, _selection(), (claim,), publisher="application")
    assert not unknown.report.verified and unknown.report.outcome == "unverified"
    assert unknown.evidence[0].current
    plain = _view(Graph().add((EX.Term, RDFS.subClassOf, EX.Parent)))
    assert (
        check_ownership(plain, _selection(), (), publisher="application").report.outcome
        == "unverified"
    )
    assert (
        check_ownership(
            _view(Graph()),
            _selection(),
            (replace(claim, source=_view(Graph()), role="class"),),
            publisher="application",
        ).report.outcome
        == "unverified"
    )
    budget = Budget(Limits(max_work=40))
    exhausted = check_ownership(
        plain,
        _selection(),
        (replace(claim, source=plain),),
        publisher="application",
        budget=budget,
    )
    assert (
        exhausted.report.outcome == "exhausted" and budget._counts.get("work", 0) <= 40
    )


def test_authoritative_import_scope_and_unselected_term_are_not_guessed() -> None:
    support = Graph().add((EX.Term, RDFS.subClassOf, EX.Parent))
    support.add((EX.Unselected, RDFS.subClassOf, Literal("not a class")))
    application = Graph().add((EX.AppOntology, OWL.imports, EX.Support))
    application.add((EX.Term, RDFS.label, Literal("allowed editorial label")))
    vocabulary = load_vocabulary(application, imports={str(EX.Support): support})
    imported = inspect_vocabulary(vocabulary, scopes=("imports",))
    local = inspect_vocabulary(vocabulary, scopes=("local",))
    claim = OwnerClaim("upstream", imported, term=str(EX.Term))
    result = check_ownership(local, _selection(), (claim,), publisher="application")
    assert result.report.verified
    assert not result.evidence[0].current
    assert result.evidence[0].authoritative[0].source_scope == "import[0]"
    application.add((EX.Term, RDFS.subClassOf, EX.Parent))
    duplicates = inspect_vocabulary(
        load_vocabulary(application, imports={str(EX.Support): support}),
        scopes=("local", "imports"),
    )
    repeated = check_ownership(
        duplicates,
        replace(_selection(), scopes=("local", "imports")),
        (claim,),
        publisher="application",
    )
    assert repeated.report.verified
    assert {fact.source_scope for fact in repeated.evidence[0].current} == {
        "local",
        "import[0]",
    }
    application.add((EX.Term, RDFS.subClassOf, EX.Other))
    local = inspect_vocabulary(
        load_vocabulary(application, imports={str(EX.Support): support}),
        scopes=("local",),
    )
    assert (
        check_ownership(
            local, _selection(), (claim,), publisher="application"
        ).report.outcome
        == "disagreement"
    )


def test_unqualified_scope_is_unknown_but_malformed_metadata_is_input_error() -> None:
    graph = Graph().add((EX.Term, RDFS.subClassOf, EX.Parent))
    source = _view(graph)
    claim = OwnerClaim("upstream", source, term=str(EX.Term))
    local = _view(graph)
    prepared = source._vocabulary._source_handles[0][1].prepared
    prepared.graph_fidelity = "nonempty-only"
    result = check_ownership(local, _selection(), (claim,), publisher="application")
    assert result.report.outcome == "unverified"
    prepared.union = 0  # type: ignore[assignment]
    malformed = check_ownership(local, _selection(), (claim,), publisher="application")
    assert malformed.report.outcome == "input_error"


def test_owner_evidence_cannot_supply_missing_current_subject_coverage() -> None:
    owner = _view(Graph().add((EX.Term, RDFS.subClassOf, EX.Parent)))
    claim = OwnerClaim("upstream", owner, term=str(EX.Term))
    for graph in (Graph(), Graph().add((EX.App, RDFS.subClassOf, EX.Term))):
        result = check_ownership(
            _view(graph), _selection(), (claim,), publisher="application"
        )
        assert result.report.outcome == "unverified" and not result.evidence
    editorial = Graph().add((EX.Term, RDFS.label, Literal("allowed label")))
    assert check_ownership(
        _view(editorial), _selection(), (claim,), publisher="application"
    ).report.verified
    lookup = inspect_vocabulary(
        load_vocabulary(Graph(), lookup=(editorial,)), scopes=("local",)
    )
    assert (
        check_ownership(
            lookup, _selection(), (claim,), publisher="application"
        ).report.outcome
        == "unverified"
    )
    dataset = Dataset()
    dataset.graph(EX.presentGraph).add((EX.Term, RDFS.label, Literal("excluded graph")))
    view = _view(dataset, EX.presentGraph)
    from bijux_ontology.vocabulary.facts import FactGraph

    excluded = replace(_selection(), graphs=(FactGraph("named", EX.otherGraph),))
    included = replace(_selection(), graphs=(FactGraph("named", EX.presentGraph),))
    assert check_ownership(
        view, included, (claim,), publisher="application"
    ).report.verified
    assert (
        check_ownership(
            view, excluded, (claim,), publisher="application"
        ).report.outcome
        == "unverified"
    )
