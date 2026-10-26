"""Current GO assertions retain signed meaning, exact joins and native consumers."""

import io
import json
from dataclasses import replace
from threading import Event

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.registry.types import (
    IdentifierRegistry,
    NamespaceContribution,
)
from bijux_ontology_adapters.go.annotations import (
    GoAnnotationDocument,
    GoAnnotationPolicy,
    GoAnnotations,
    GoGeneProductDocument,
    annotations_to_rdf,
    check_go_annotations,
    read_go_annotations,
    write_go_annotations,
)

_SOURCE = "urn:go-consumer:annotations"
_GPI = "urn:go-consumer:entities"
_SIGNED = "urn:bijux:ontology:go:assertion:"


def _header(kind: str) -> str:
    """Declare versions and required producer metadata independently of parsing."""
    version = "2.2" if kind == "gaf" else "2.0"
    return (
        f"!{kind}-version: {version}\n"
        "!generated-by: UniProt\n!date-generated: 2026-10-19\n"
    )


def _gaf(*, negative: bool = True, term: str = "GO:0003674") -> GoAnnotationDocument:
    """Supply an ordinary controlled assertion with both grouping operators."""
    fields = (
        "UniProtKB",
        "P12345",
        "GENE",
        "NOT|enables" if negative else "enables",
        term,
        "PMID:123|GO_REF:0000001",
        "IPI",
        "UniProtKB:Q1,UniProtKB:Q2|UniProtKB:Q3",
        "F",
        'protein "name"',
        "synonym",
        "protein",
        "taxon:9606|taxon:10090",
        "20261019",
        "UniProt",
        "part_of(CL:0000001),occurs_in(UBERON:0000001)|part_of(CL:0000002)",
        "",
    )
    return GoAnnotationDocument(
        _SOURCE, _header("gaf") + "\t".join(fields) + "\n", "gaf"
    )


def _gpi(
    *, subject: str = "UniProtKB:P12345", duplicate: bool = False
) -> GoGeneProductDocument:
    """Supply the exact GPAD companion entity, not a replacement annotation."""
    fields = (
        subject,
        "GENE",
        "protein",
        "synonym",
        "PR:000000001",
        "NCBITaxon:9606",
        "",
        "",
        "",
        "",
        "",
    )
    row = "\t".join(fields) + "\n"
    return GoGeneProductDocument(_GPI, _header("gpi") + row * (2 if duplicate else 1))


def _gpad() -> GoAnnotationDocument:
    """Keep different evidence and interacting taxa on separate same-subject rows."""
    rows = []
    for negative, evidence, taxon in (
        (True, "ECO:0000353", "NCBITaxon:10090"),
        (False, "ECO:0000314", "NCBITaxon:7227"),
    ):
        fields = (
            "UniProtKB:P12345",
            "NOT" if negative else "",
            "RO:0002327",
            "GO:0003674",
            "PMID:123",
            evidence,
            "UniProtKB:Q1,UniProtKB:Q2|UniProtKB:Q3",
            taxon,
            "2026-10-19",
            "UniProt",
            "BFO:0000050(CL:0000001),RO:0001025(UBERON:0000001)|BFO:0000050(CL:0000002)",
            "id=GOA:123|contributor-id=orcid:0000-0000-0000-0001",
        )
        rows.append("\t".join(fields))
    return GoAnnotationDocument(
        _SOURCE, _header("gpad") + "\n".join(rows) + "\n", "gpad"
    )


def _registry() -> IdentifierRegistry:
    """Supply explicit application prefixes; no network registry is consulted."""
    return IdentifierRegistry(
        tuple(
            NamespaceContribution(
                "http://purl.obolibrary.org/obo/" + prefix + "_", prefix, "consumer"
            )
            for prefix in ("GO", "RO", "ECO", "CL", "UBERON", "BFO", "NCBITaxon")
        )
        + (NamespaceContribution("urn:uniprot:", "UniProtKB", "consumer"),)
    )


def test_signed_gaf_roundtrip_preserves_groups_without_asserting_positive_edges() -> (
    None
):
    document = _gaf()
    positive_row = _gaf(negative=False).content.splitlines()[-1]
    document = replace(document, content=document.content + positive_row + "\n")
    parsed = read_go_annotations(document)
    assert parsed.report.verified and len(parsed.annotations) == 2
    negative, positive = parsed.annotations
    assert negative.negated and not positive.negated
    assert negative.evidence.with_from == (
        ("UniProtKB:Q1", "UniProtKB:Q2"),
        ("UniProtKB:Q3",),
    )
    assert tuple(len(group) for group in negative.extensions) == (2, 1)
    assert negative.entity_taxon == "taxon:9606" and negative.interacting_taxa == (
        "taxon:10090",
    )
    output = write_go_annotations(parsed)
    assert output.report.verified and output.content == document.content
    assert (
        read_go_annotations(replace(document, content=output.content)).annotations
        == parsed.annotations
    )
    exported = annotations_to_rdf(
        parsed, _registry(), relations={"enables": "RO:0002327"}
    )
    assert exported.report.verified and exported.rdf is not None
    graph = exported.rdf.graph()
    subjects = tuple(graph.subjects(RDF.type, RDF.Statement))
    assert len(subjects) == 2
    assert sorted(
        str(value) for value in graph.objects(None, URIRef(_SIGNED + "negated"))
    ) == ["false", "true"]
    for node in subjects:
        assert (
            graph.value(node, RDF.subject),
            graph.value(node, RDF.predicate),
            graph.value(node, RDF.object),
        ) not in graph
        assert (
            json.loads(str(graph.value(node, URIRef(_SIGNED + "payload"))))["fields"][
                15
            ]
            == negative.fields[15]
        )
    assert not tuple(graph.subjects(RDF.type, OWL.NegativePropertyAssertion))
    assert not annotations_to_rdf(parsed, _registry()).report.verified


def test_gpad_gpi_exact_join_keeps_evidence_taxon_and_signed_assertions() -> None:
    document = _gpad()
    parsed = read_go_annotations(document, gpi=_gpi())
    assert (
        parsed.report.verified
        and parsed.join is not None
        and parsed.join.report.verified
    )
    assert [item.negated for item in parsed.annotations] == [True, False]
    assert [item.evidence.code for item in parsed.annotations] == [
        "ECO:0000353",
        "ECO:0000314",
    ]
    assert [item.interacting_taxa for item in parsed.annotations] == [
        ("NCBITaxon:10090",),
        ("NCBITaxon:7227",),
    ]
    assert all(
        item.entity is parsed.gene_products[0] and item.entity_taxon == "NCBITaxon:9606"
        for item in parsed.annotations
    )
    output = write_go_annotations(parsed)
    assert output.report.verified and output.content == document.content
    repeated = read_go_annotations(
        replace(document, content=output.content), gpi=_gpi()
    )
    assert repeated.annotations == parsed.annotations
    assert annotations_to_rdf(parsed, _registry()).report.verified
    view_graph = Graph()
    prefixes = _registry()
    for identifier in (
        "GO:0003674",
        "ECO:0000353",
        "ECO:0000314",
        "NCBITaxon:9606",
        "NCBITaxon:10090",
        "NCBITaxon:7227",
        "CL:0000001",
        "CL:0000002",
        "UBERON:0000001",
    ):
        view_graph.add((URIRef(prefixes.expand_curie(identifier)), RDF.type, OWL.Class))
    for identifier in ("RO:0002327", "BFO:0000050", "RO:0001025"):
        view_graph.add(
            (URIRef(prefixes.expand_curie(identifier)), RDF.type, OWL.ObjectProperty)
        )
    policy = GoAnnotationPolicy(
        roles=(
            "term",
            "relation",
            "evidence",
            "taxon",
            "extension_term",
            "extension_relation",
        )
    )
    assert check_go_annotations(
        parsed, VocabularyView(view_graph), prefixes, policy=policy
    ).report.verified
    evidence_iri = URIRef(prefixes.expand_curie("ECO:0000314"))
    view_graph.set((evidence_iri, RDF.type, OWL.ObjectProperty))
    wrong_role = check_go_annotations(
        parsed, VocabularyView(view_graph), prefixes, policy=policy
    )
    assert "go.reference_kind_mismatch" in {
        issue.code for issue in wrong_role.report.issues
    }


def test_invalid_grouping_versions_and_missing_or_ambiguous_gpi_are_distinct() -> None:
    document = _gpad()
    for content in (
        document.content.replace("2.0", "1.2", 1),
        document.content.replace("BFO:0000050(CL:0000001)", "BFO:0000050()", 1),
        document.content.replace("|BFO:0000050", "||BFO:0000050", 1),
    ):
        result = read_go_annotations(replace(document, content=content), gpi=_gpi())
        assert not result.report.verified
        assert any(
            issue.details is not None
            and issue.details.failure is not None
            and issue.details.failure.span is not None
            for issue in result.report.issues
        )
    absent = read_go_annotations(document)
    assert not absent.report.verified and absent.annotations
    assert any(issue.kind == "unverified" for issue in absent.report.issues)
    assert not any(issue.kind == "input_error" for issue in absent.report.issues)
    missing = read_go_annotations(document, gpi=_gpi(subject="UniProtKB:OTHER"))
    ambiguous = read_go_annotations(document, gpi=_gpi(duplicate=True))
    assert not missing.report.verified and missing.join is not None
    assert {resolution.status for resolution in missing.join.resolutions} == {"missing"}
    assert not ambiguous.report.verified and ambiguous.join is not None
    assert {resolution.status for resolution in ambiguous.join.resolutions} == {
        "ambiguous"
    }
    assert not write_go_annotations(missing).report.verified
    assert any(
        issue.code == "go.gpi_subject_missing"
        and issue.details is not None
        and issue.details.failure is not None
        and issue.details.failure.span is not None
        and issue.details.failure.span.start_line == 4
        for issue in missing.report.issues
    )
    empty = read_go_annotations(
        document, gpi=GoGeneProductDocument(_GPI, _header("gpi"))
    )
    assert empty.join is not None and {
        item.status for item in empty.join.resolutions
    } == {"missing"}
    assert not any(issue.kind == "input_error" for issue in empty.report.issues)


def test_selected_vocabulary_lifecycle_and_legacy_evidence_do_not_change_identity() -> (
    None
):
    iri = URIRef("http://purl.obolibrary.org/obo/GO_0003674")
    graph = (
        Graph()
        .add((iri, RDF.type, OWL.Class))
        .add((iri, OWL.deprecated, Literal(False)))
    )
    parsed = read_go_annotations(_gaf())
    accepted = check_go_annotations(parsed, VocabularyView(graph), _registry())
    assert (
        accepted.report.verified
        and accepted.references[0].annotation is parsed.annotations[0]
    )
    assert accepted.declarations[0].iri == str(iri)
    retired = check_go_annotations(
        parsed,
        VocabularyView(graph.set((iri, OWL.deprecated, Literal(True)))),
        _registry(),
    )
    assert "go.term_retired" in {issue.code for issue in retired.report.issues}
    assert retired.references[0].deprecation is not None
    assert retired.references[0].deprecation.state == "deprecated"
    permitted = check_go_annotations(
        parsed,
        VocabularyView(graph),
        _registry(),
        policy=GoAnnotationPolicy(allow_deprecated=True),
    )
    assert permitted.report.verified and permitted.policy.allow_deprecated
    assert (
        permitted.references[0].deprecation is not None
        and permitted.references[0].deprecation.state == "deprecated"
    )
    missing = check_go_annotations(parsed, VocabularyView(Graph()), _registry())
    assert "go.reference_missing" in {issue.code for issue in missing.report.issues}
    evidence = check_go_annotations(
        parsed,
        VocabularyView(graph),
        _registry(),
        policy=GoAnnotationPolicy(roles=("term", "evidence"), allow_deprecated=True),
    )
    assert not evidence.report.verified
    assert any(
        reference.role == "evidence"
        and reference.iri is None
        and reference.original == "IPI"
        for reference in evidence.references
    )
    alternative = URIRef(
        "http://www.geneontology.org/formats/oboInOwl#hasAlternativeId"
    )
    graph = (
        Graph()
        .add((iri, RDF.type, OWL.Class))
        .add((iri, alternative, Literal("GO:0000000")))
    )
    aliased = read_go_annotations(_gaf(term="GO:0000000"))
    result = check_go_annotations(
        aliased,
        VocabularyView(graph),
        _registry(),
        policy=GoAnnotationPolicy(alternative_ids=True),
    )
    assert result.report.verified and result.references[0].original == "GO:0000000"
    graph.add((URIRef("urn:other:term"), RDF.type, RDFS.Class)).add(
        (URIRef("urn:other:term"), alternative, Literal("GO:0000000"))
    )
    assert not check_go_annotations(
        aliased,
        VocabularyView(graph),
        _registry(),
        policy=GoAnnotationPolicy(alternative_ids=True),
    ).report.verified


def test_cumulative_limits_cancel_and_fresh_core_recovery() -> None:
    document = _gaf()
    for limits in (
        Limits(max_work=32),
        Limits(max_states=8),
        Limits(max_total_bytes=128),
    ):
        budget = Budget(limits)
        result = read_go_annotations(document, budget=budget)
        assert not result.report.verified and any(
            issue.kind == "exhausted" for issue in result.report.issues
        )
    cancel = Event()
    cancel.set()
    assert not read_go_annotations(document, cancel=cancel).report.verified
    result = read_go_annotations(document)
    assert result.report.verified
    assert not write_go_annotations(
        result, budget=Budget(Limits(max_bytes=32))
    ).report.verified
    with pytest.raises(InputError, match="read_go_annotations"):
        GoAnnotations()


def test_actual_maintained_gaf_consumer_preserves_the_exported_record() -> None:
    native = pytest.importorskip("Bio.UniProt.GOA")
    document = _gaf()
    rows = list(native.gafiterator(io.StringIO(document.content)))
    assert len(rows) == 1 and rows[0]["Qualifier"] == ["NOT", "enables"]
    buffer = io.StringIO()
    native.writerec(rows[0], buffer)
    parsed = read_go_annotations(
        replace(document, content=_header("gaf") + buffer.getvalue())
    )
    assert parsed.report.verified
    exported = write_go_annotations(parsed)
    assert exported.content is not None
    assert list(native.gafiterator(io.StringIO(exported.content))) == rows
