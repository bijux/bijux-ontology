"""Opt-in exact lexical policies and checked attachment-preserving repairs."""

import threading
import unicodedata
from dataclasses import replace
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, SKOS, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report, combine_reports
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.migrations import MigrationCheck
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_term, term_key
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.skos.labels import check_label_collisions
from bijux_ontology.vocabulary.annotations import (
    AnnotationRequirement,
    LanguageRequirement,
    check_annotation_coverage,
)
from bijux_ontology.vocabulary.annotations.graphs import (
    AnnotationStatement,
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.annotations.hygiene import (
    AnnotationLiteralRepair,
    HygieneCheck,
    HygieneRule,
    UnicodeRequirement,
    check_annotation_hygiene,
    repair_annotation_hygiene,
)
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary

E = Namespace("urn:hygiene:")
OLD = Literal("Cell ", lang="en")
NEW = Literal("Cell", lang="en")
ROOT = AnnotationStatement(E.concept, SKOS.prefLabel, OLD)


def _attach(
    graph: Graph, node: BNode, statement: AnnotationStatement, kind: URIRef
) -> None:
    """Use actual ordinary OWL mapping assertions and preserve their exact terms."""
    for predicate, value in (
        (RDF.type, kind),
        (OWL.annotatedSource, statement.subject),
        (OWL.annotatedProperty, statement.predicate),
        (OWL.annotatedTarget, statement.value),
    ):
        graph.add((node, predicate, value))


def _graph() -> Graph:
    """A label has two distinct reifiers and one child-qualified literal."""
    graph = (
        Graph()
        .add((E.concept, RDF.type, SKOS.Concept))
        .add((E.scheme, RDF.type, SKOS.ConceptScheme))
    )
    graph.add((E.concept, SKOS.inScheme, E.scheme)).add(
        (E.concept, SKOS.prefLabel, OLD)
    )
    graph.add((E.concept, SKOS.prefLabel, Literal("Cellule", lang="fr")))
    graph.add((E.note, RDF.type, OWL.AnnotationProperty)).add(
        (E.reviewer, RDF.type, OWL.AnnotationProperty)
    )
    for name in ("first", "second"):
        node = BNode(name)
        _attach(graph, node, ROOT, OWL.Axiom)
        graph.add((node, E.note, Literal("note ", lang="en")))
    _attach(
        graph,
        BNode("child"),
        AnnotationStatement(BNode("first"), E.note, Literal("note ", lang="en")),
        OWL.Annotation,
    )
    graph.add((BNode("child"), E.reviewer, Literal("Alice")))
    graph.add((E.unselected, E.note, literal_term("01", datatype=str(XSD.integer))))
    return graph


def _checked(source: PreparedSource, *, graph: URIRef | None = None) -> HygieneCheck:
    """Only the selected root label is subject to the trim policy."""
    selected = inspect_annotation_graph(
        source, (ROOT,), graph=graph, annotation_predicates=(SKOS.prefLabel,)
    )
    return check_annotation_hygiene(
        selected, (HygieneRule(SKOS.prefLabel, edge_whitespace=True),)
    )


def _publication(source: PreparedSource) -> Report:
    """Actual explicit collision, language and native SKOS integrity contracts."""
    graph = source.graph(E.g if source.is_dataset else None)
    view = inspect_vocabulary(graph)
    return combine_reports(
        "publication",
        check_label_collisions(
            view, schemes=(str(E.scheme),), roles=(str(SKOS.prefLabel),)
        ),
        check_annotation_coverage(
            view,
            (
                AnnotationRequirement(
                    str(SKOS.prefLabel),
                    languages=(LanguageRequirement("en"), LanguageRequirement("fr")),
                ),
            ),
            entities=(str(E.concept),),
        ).report,
        check_skos(view, concepts=(str(E.concept),), profile="core-integrity"),
    )


def _repair(checked: HygieneCheck, value: Literal = NEW) -> AnnotationLiteralRepair:
    """Select the actual original witness, never a re-created blank path label."""
    return AnnotationLiteralRepair(checked.findings[0].path, value)


def test_opt_in_spans_multiline_scripts_languages_and_unicode_versions() -> None:
    """Original spans are codepoints; legal multiline and script characters survive."""
    value = Literal("Al\tpha ", lang="en")
    definition = Literal("Line one\nLine two\u200d", lang="ar")
    untagged = Literal("untagged ")
    graph = (
        Graph()
        .add((E.concept, SKOS.prefLabel, value))
        .add((E.concept, SKOS.definition, definition))
        .add((E.other, SKOS.prefLabel, untagged))
    )
    roots = (
        AnnotationStatement(E.concept, SKOS.prefLabel, value),
        AnnotationStatement(E.concept, SKOS.definition, definition),
        AnnotationStatement(E.other, SKOS.prefLabel, untagged),
    )
    selected = inspect_annotation_graph(
        prepare_rdf(graph),
        roots,
        annotation_predicates=(SKOS.prefLabel, SKOS.definition),
    )
    result = check_annotation_hygiene(
        selected,
        (
            HygieneRule(
                SKOS.prefLabel,
                languages=("en",),
                edge_whitespace=True,
                forbidden_characters=("\t",),
            ),
            HygieneRule(SKOS.definition),
        ),
    )
    assert result.report.complete and not result.report.verified
    assert {
        (finding.kind, finding.start, finding.end) for finding in result.findings
    } == {("edge_whitespace", 6, 7), ("forbidden_character", 2, 3)}
    assert all(
        finding.path.statement.value is value or finding.path.statement.value == value
        for finding in result.findings
    )
    assert check_annotation_hygiene(
        selected, (HygieneRule(SKOS.definition),)
    ).report.verified
    assert (
        check_annotation_hygiene(
            selected,
            (HygieneRule(SKOS.prefLabel, languages=(ABSENT,), edge_whitespace=True),),
        )
        .findings[0]
        .path.statement.value
        == untagged
    )
    excluded = HygieneRule(
        SKOS.prefLabel, edge_whitespace=True, excluded_statements=(roots[0], roots[2])
    )
    assert not check_annotation_hygiene(selected, (excluded,)).report.verified
    astral = Literal("😀\t ", lang="en")
    astral_read = inspect_annotation_graph(
        prepare_rdf(Graph().add((E.concept, SKOS.prefLabel, astral))),
        (AnnotationStatement(E.concept, SKOS.prefLabel, astral),),
        annotation_predicates=(SKOS.prefLabel,),
    )
    astral_check = check_annotation_hygiene(
        astral_read, (HygieneRule(SKOS.prefLabel, forbidden_characters=("\t",)),)
    )
    assert [(finding.start, finding.end) for finding in astral_check.findings] == [
        (1, 2)
    ]
    composed = Literal("e\u0301", lang="fr")
    normalized = inspect_annotation_graph(
        prepare_rdf(Graph().add((E.concept, SKOS.prefLabel, composed))),
        (AnnotationStatement(E.concept, SKOS.prefLabel, composed),),
        annotation_predicates=(SKOS.prefLabel,),
    )
    rule = HygieneRule(
        SKOS.prefLabel,
        normalization=UnicodeRequirement("NFC", unicodedata.unidata_version),
    )
    actual = check_annotation_hygiene(normalized, (rule,))
    assert actual.unicode_version == unicodedata.unidata_version
    assert [(f.start, f.end) for f in actual.findings] == [(0, 2)]
    unavailable = check_annotation_hygiene(
        normalized, (replace(rule, normalization=UnicodeRequirement("NFC", "0.0")),)
    )
    assert unavailable.report.outcome == "unverified"
    assert check_annotation_hygiene(
        selected,
        (
            HygieneRule(
                SKOS.definition,
                forbidden_categories=("Cf",),
                allowed_characters=("\u200d",),
            ),
        ),
    ).report.verified


def test_direct_repair_preserves_targets_children_and_portable_export() -> None:
    """The actual label and both targets change, with nested qualifiers unchanged."""
    source = prepare_rdf(_graph())
    before = set(source.graph())
    checked = _checked(source)
    assert checked.report.complete and checked.findings
    result = repair_annotation_hygiene(
        checked,
        (_repair(checked),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert (
        result.report.verified
        and result.source is not None
        and result.patch is not None
    )
    actual = result.source.graph()
    assert set(source.graph()) == before
    assert (E.concept, SKOS.prefLabel, Literal("Cell", lang="en")) in actual
    assert (
        len(list(actual.subjects(OWL.annotatedTarget, Literal("Cell", lang="en")))) == 2
    )
    assert not list(actual.subjects(OWL.annotatedTarget, OLD))
    assert len(list(actual.subjects(E.note, Literal("note ", lang="en")))) == 2
    assert [term_key(value) for value in actual.objects(None, E.reviewer)] == [
        term_key(Literal("Alice"))
    ]
    assert str(actual.value(E.unselected, E.note)) == "01"
    assert len(result.patch.additions) == len(result.patch.removals) == 3
    assert not result.before[0].verified and all(
        report.verified for report in result.after
    )
    assert export_migration(result, format="nt").content is None
    exported = export_migration(result, accept=True, format="nt")
    assert exported.content is not None
    reloaded = Graph().parse(data=exported.content, format="nt")
    assert [term_key(value) for value in reloaded.objects(None, E.reviewer)] == [
        term_key(Literal("Alice"))
    ]


def test_child_qualified_literal_repair_preserves_its_reviewer_and_owned_paths() -> (
    None
):
    """A child-qualified nonleaf literal can be repaired without deleting its body."""
    source = prepare_rdf(_graph())
    selected = inspect_annotation_graph(
        source, (ROOT,), annotation_predicates=(SKOS.prefLabel,)
    )
    checked = check_annotation_hygiene(
        selected, (HygieneRule(E.note, roles=("axiom",), edge_whitespace=True),)
    )
    target = next(
        f.path
        for f in checked.findings
        if any(a.annotated == f.path.statement for a in selected.attachments)
    )
    result = repair_annotation_hygiene(
        checked,
        (AnnotationLiteralRepair(target, Literal("note", lang="en")),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    # The second distinct annotation still violates the selected policy.
    assert result.source is not None and not result.report.verified
    graph = result.source.graph()
    assert [term_key(value) for value in graph.objects(None, E.reviewer)] == [
        term_key(Literal("Alice"))
    ]
    assert (
        len(list(graph.subjects(OWL.annotatedTarget, Literal("note", lang="en")))) == 1
    )
    assert export_migration(result, accept=True, format="turtle").content is None
    single = prepare_rdf(_graph().remove((BNode("second"), E.note, None)))
    single_read = inspect_annotation_graph(
        single, (ROOT,), annotation_predicates=(SKOS.prefLabel,)
    )
    single_check = check_annotation_hygiene(
        single_read, (HygieneRule(E.note, roles=("axiom",), edge_whitespace=True),)
    )
    repaired = repair_annotation_hygiene(
        single_check,
        (
            AnnotationLiteralRepair(
                single_check.findings[0].path, Literal("note", lang="en")
            ),
        ),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert repaired.report.verified and repaired.source is not None
    assert (
        len(
            list(
                repaired.source.graph().subjects(
                    OWL.annotatedTarget, Literal("note", lang="en")
                )
            )
        )
        == 1
    )
    excluded = next(f.path.statement for f in checked.findings if f.path != target)
    scoped = check_annotation_hygiene(
        selected,
        (
            HygieneRule(
                E.note,
                roles=("axiom",),
                edge_whitespace=True,
                excluded_statements=(excluded,),
            ),
        ),
    )
    unknown = repair_annotation_hygiene(
        scoped,
        (AnnotationLiteralRepair(target, Literal("note", lang="en")),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert unknown.source is not None and unknown.report.outcome == "unverified"
    assert not any(
        issue.code == "annotations.hygiene_violation"
        for report in unknown.after
        for issue in report
    )
    foreign = replace(
        target,
        attachments=tuple(
            BNode(str(node)) if isinstance(node, BNode) else node
            for node in target.attachments
        ),
    )
    rejected = repair_annotation_hygiene(
        checked,
        (AnnotationLiteralRepair(foreign, Literal("note", lang="en")),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert rejected.source is None and not rejected.report.verified


def test_publication_collision_collapse_language_and_sharing_block_acceptance() -> None:
    """Actual after contracts prevent collisions; absent scopes stay unknown."""
    graph = (
        _graph()
        .add((E.other, RDF.type, SKOS.Concept))
        .add((E.other, SKOS.inScheme, E.scheme))
        .add((E.other, SKOS.prefLabel, Literal("Cell", lang="en")))
    )
    source = prepare_rdf(graph)
    checked = _checked(source)
    failed = repair_annotation_hygiene(
        checked,
        (_repair(checked),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert failed.source is not None and not failed.report.verified
    assert any(
        issue.code == "skos.policy.label_collision"
        for report in failed.after
        for issue in report
    )
    assert export_migration(failed, accept=True, format="turtle").content is None
    collapsed = _graph().add((E.concept, SKOS.prefLabel, Literal("Cell", lang="en")))
    checked = _checked(prepare_rdf(collapsed))
    assert (
        repair_annotation_hygiene(
            checked,
            (_repair(checked),),
            after_checks=(MigrationCheck("publication", _publication),),
        ).source
        is None
    )
    plain = _checked(prepare_rdf(_graph()))
    assert (
        repair_annotation_hygiene(
            plain, (_repair(plain),), after_checks=()
        ).report.outcome
        == "unverified"
    )
    missing_language = _checked(
        prepare_rdf(
            _graph().remove((E.concept, SKOS.prefLabel, Literal("Cellule", lang="fr")))
        )
    )
    language_rejected = repair_annotation_hygiene(
        missing_language,
        (_repair(missing_language),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert (
        language_rejected.source is not None and not language_rejected.report.verified
    )
    assert (
        export_migration(language_rejected, accept=True, format="turtle").content
        is None
    )
    changed_language = repair_annotation_hygiene(
        plain,
        (_repair(plain, Literal("Cell", lang="de")),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert changed_language.source is None
    shared = _graph().add((E.other, E.reference, BNode("first")))
    checked = _checked(prepare_rdf(shared))
    assert (
        repair_annotation_hygiene(
            checked,
            (_repair(checked),),
            after_checks=(MigrationCheck("publication", _publication),),
        ).source
        is None
    )


def test_physical_scope_exact_fidelity_and_bounded_partial_truth() -> None:
    """Hidden graphs stay unchanged; required incomplete tails cannot pass."""
    dataset = Dataset()
    for triple in _graph():
        dataset.graph(E.g).add(triple)
        if not isinstance(triple[0], BNode) and not isinstance(triple[2], BNode):
            dataset.graph(E.h).add(triple)
    source = prepare_rdf(dataset)
    checked = _checked(source, graph=E.g)
    result = repair_annotation_hygiene(
        checked,
        (_repair(checked),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert result.report.verified and result.source is not None
    assert (E.concept, SKOS.prefLabel, OLD) in result.source.graph(E.h)
    assert (
        E.concept,
        SKOS.prefLabel,
        Literal("Cell", lang="en"),
    ) in result.source.graph(E.g)
    assert term_key(result.source.graph(E.h).value(E.unselected, E.note)) == term_key(
        literal_term("01", datatype=str(XSD.integer))
    )
    stopped = threading.Event()
    stopped.set()
    assert (
        check_annotation_hygiene(
            checked.graph, checked.rules, cancel=stopped
        ).report.outcome
        == "exhausted"
    )
    assert (
        repair_annotation_hygiene(
            checked,
            (_repair(checked),),
            after_checks=(MigrationCheck("publication", _publication),),
            cancel=stopped,
        ).report.outcome
        == "exhausted"
    )
    huge = HygieneRule(
        SKOS.prefLabel,
        excluded_statements=(
            AnnotationStatement(E.concept, SKOS.prefLabel, Literal("x" * 10000)),
        ),
    )
    assert (
        check_annotation_hygiene(
            checked.graph, (huge,), limits=Limits(max_bytes=1000)
        ).report.outcome
        == "exhausted"
    )
    quota = Budget(Limits(max_issues=1))
    quota.consume("issues")
    result_check = check_annotation_hygiene(checked.graph, checked.rules, budget=quota)
    assert result_check.report.outcome == "exhausted"
    for invalid in (None, "not-rules", iter(())):
        with pytest.raises(InputError):
            check_annotation_hygiene(
                checked.graph, cast(tuple[HygieneRule, ...], invalid)
            )
