"""Exact native OBO role/qualifier records without merging lexical values."""

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.obo import read_obo_records
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/obo/")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
DEFINITION = URIRef("http://purl.obolibrary.org/obo/IAO_0000115")


def annotate(
    graph: Graph, subject: URIRef, predicate: URIRef, target: Literal, xref: str
) -> BNode:
    """Supply the standard native annotation assertion and its reification."""
    node = BNode()
    for edge in (
        (subject, predicate, target),
        (node, RDF.type, OWL.Axiom),
        (node, OWL.annotatedSource, subject),
        (node, OWL.annotatedProperty, predicate),
        (node, OWL.annotatedTarget, target),
        (node, OIO.hasDbXref, Literal(xref)),
    ):
        graph.add(edge)
    return node


def test_synonym_roles_equal_text_and_definition_qualifiers_remain_separate() -> None:
    graph = Graph()
    exact = annotate(
        graph, EX.Term, OIO.hasExactSynonym, Literal("same", lang="en"), "PMID:1"
    )
    broad = annotate(
        graph, EX.Term, OIO.hasBroadSynonym, Literal("same", lang="en"), "PMID:2"
    )
    graph.add((exact, OIO.hasSynonymType, EX.ShortForm))
    graph.add((broad, EX.curator, EX.Curator))
    annotate(graph, EX.Term, DEFINITION, Literal("definition", lang="en"), "PMID:3")
    annotate(graph, EX.Term, DEFINITION, Literal("definition", lang="de"), "PMID:4")
    result = read_obo_records(inspect_vocabulary(graph), str(EX.Term))
    assert result.report.verified and len(result.records) == 4
    synonyms = {
        record.role: record for record in result.records if record.role != "definition"
    }
    assert synonyms["exact"].literal == synonyms["broad"].literal
    assert synonyms["exact"].language == "en"
    assert [
        edge.object for edge in synonyms["exact"].attachments[0].cross_references
    ] == [Literal("PMID:1")]
    assert [
        edge.object for edge in synonyms["broad"].attachments[0].cross_references
    ] == [Literal("PMID:2")]
    assert synonyms["exact"].attachments[0].synonym_types[0].object == EX.ShortForm
    assert synonyms["broad"].attachments[0].opaque[0].object == EX.Curator
    definitions = {
        record.language: record.attachments[0]
        for record in result.records
        if record.role == "definition"
    }
    assert definitions["en"].cross_references[0].object == Literal("PMID:3")
    assert definitions["de"].cross_references[0].object == Literal("PMID:4")


def test_attachment_boundaries_source_scope_and_original_opaque_edges_are_owned() -> (
    None
):
    local, imported = Graph(), Graph()
    annotate(local, EX.Term, DEFINITION, Literal("same"), "LOCAL:1")
    annotate(local, EX.Term, DEFINITION, Literal("same"), "LOCAL:2")
    opaque = annotate(imported, EX.Term, DEFINITION, Literal("same"), "IMPORT:1")
    imported.add((opaque, EX.unsupported, BNode()))
    source = Vocabulary(local, imports=(imported,))
    view = inspect_vocabulary(source)
    local.remove((None, None, None))
    imported.remove((None, None, None))
    result = read_obo_records(view, str(EX.Term))
    assert result.report.verified and len(result.records) == 2
    by_scope = {record.assertion.scope: record for record in result.records}
    assert len(by_scope["local"].attachments) == 2
    assert len(by_scope["import[0]"].attachments) == 1
    assert isinstance(by_scope["import[0]"].attachments[0].opaque[0].object, BNode)
    assert all(
        edge.scope == attachment.scope
        for attachment in result.attachments
        for edge in attachment.statements
    )
    local_only = read_obo_records(
        inspect_vocabulary(source, scopes=("local",)), str(EX.Term)
    )
    assert len(local_only.records) == 1 and len(local_only.attachments) == 2
    # A named reification node's anchor in one source cannot borrow the base or
    # its target/qualifiers from another independently selected source.
    local, imported = Graph(), Graph()
    local.add((EX.Term, DEFINITION, Literal("selected")))
    imported.add((EX.Axiom, RDF.type, OWL.Axiom))
    imported.add((EX.Axiom, OWL.annotatedSource, EX.Term))
    imported.add((EX.Axiom, OWL.annotatedProperty, DEFINITION))
    imported.add((EX.Axiom, OWL.annotatedTarget, Literal("selected")))
    mixed = read_obo_records(
        inspect_vocabulary(Vocabulary(local, imports=(imported,))), str(EX.Term)
    )
    assert any(issue.code == "obo.base_missing" for issue in mixed.report.issues)
    assert not mixed.records[0].attachments
    # A shared named reification identifier can describe a different selected
    # entity in another source; that group is outside the first entity's scope.
    local, imported = Graph(), Graph()
    for graph, entity, text in (
        (local, EX.Term, "first"),
        (imported, EX.Other, "second"),
    ):
        for edge in (
            (entity, DEFINITION, Literal(text)),
            (EX.Axiom, RDF.type, OWL.Axiom),
            (EX.Axiom, OWL.annotatedSource, entity),
            (EX.Axiom, OWL.annotatedProperty, DEFINITION),
            (EX.Axiom, OWL.annotatedTarget, Literal(text)),
        ):
            graph.add(edge)
    shared = inspect_vocabulary(Vocabulary(local, imports=(imported,)))
    for entity, scope in ((EX.Term, "local"), (EX.Other, "import[0]")):
        separate = read_obo_records(shared, str(entity))
        assert separate.report.verified and len(separate.attachments) == 1
        assert separate.attachments[0].scope == scope


def test_malformed_ambiguous_and_unrelated_attachments_never_attach_by_text() -> None:
    graph = Graph()
    malformed = annotate(graph, EX.Term, DEFINITION, Literal("first"), "X:1")
    graph.add((EX.Term, DEFINITION, Literal("second")))
    graph.add((malformed, OWL.annotatedTarget, Literal("second")))
    absent = annotate(graph, EX.Term, DEFINITION, Literal("absent"), "X:2")
    graph.remove((EX.Term, DEFINITION, Literal("absent")))
    graph.add((EX.Term, OIO.hasRelatedSynonym, EX.NotLiteral))
    annotate(graph, EX.Other, DEFINITION, Literal("first"), "OTHER:1")
    annotate(graph, EX.Term, RDFS.label, Literal("ordinary"), "LABEL:1")
    result = read_obo_records(inspect_vocabulary(graph), str(EX.Term))
    codes = {issue.code for issue in result.report.issues}
    assert result.report.outcome == "disagreement"
    assert {"obo.attachment_shape", "obo.base_missing", "obo.value_kind"} <= codes
    assert all(not record.attachments for record in result.records)
    assert len(result.attachments) == 2
    assert any(
        attachment.node != absent
        and len(
            [
                edge
                for edge in attachment.statements
                if edge.predicate == OWL.annotatedTarget
            ]
        )
        == 2
        for attachment in result.attachments
    )
    ordinary = read_obo_records(
        inspect_vocabulary(Graph().add((EX.Term, SKOS.prefLabel, Literal("ordinary")))),
        str(EX.Term),
    )
    assert ordinary.report.outcome == "not_applicable" and not ordinary.report.issues


def test_partial_reads_keep_known_records_and_do_not_invent_absent_bases() -> None:
    graph = Graph()
    annotate(graph, EX.Term, DEFINITION, Literal("first"), "X:1")
    annotate(graph, EX.Term, OIO.hasExactSynonym, Literal("same"), "X:2")
    view = inspect_vocabulary(graph)
    incomplete = []
    for cap in range(1, 160):
        result = read_obo_records(
            view, str(EX.Term), budget=Budget(Limits(max_work=cap))
        )
        assert not any(
            issue.code == "obo.base_missing" for issue in result.report.issues
        )
        if not result.report.complete:
            incomplete.append(result)
            assert result.report.outcome == "exhausted"
    assert incomplete and any(result.records for result in incomplete)
    assert any(result.attachments for result in incomplete)
    assert read_obo_records(view, str(EX.Term)).report.verified
