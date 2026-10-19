"""Repeated descriptions are application agreements, never guessed OWL conflicts."""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import overload

import pytest
from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.data.compatibility import check_data_compatibility
from bijux_ontology.data.consolidation import (
    ConsolidationCheck,
    CorpusDocument,
    PredicateAgreement,
    consolidate_entities,
    prepare_corpus,
)
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.types import PatchBlank
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

EX = Namespace("https://example.org/corpus/")


def _document(name: str, accession: Literal | None, label: str = "") -> CorpusDocument:
    """Supply known RDF with actual independent document origin metadata."""
    graph = Graph()
    if accession is not None:
        graph.add((EX.record, EX.accession, accession))
    if label:
        graph.add((EX.record, EX.label, Literal(label)))
    graph.add((EX.record, RDF.type, EX[name]))
    source = prepare_rdf(
        RdfSource(
            graph,
            document_iri=str(EX[name]),
            effective_url=str(EX["retrieved/" + name]),
        )
    )
    return CorpusDocument(name, source)


def _native(datatype: str = str(XSD.string), maximum: int = 1) -> ConsolidationCheck:
    """Run an ordinary public actual-output checker on the consolidated graph."""
    model = ModelDeclaration(
        "Record",
        (),
        fields=(
            FieldDeclaration(
                "accession",
                str(EX.accession),
                (ValueDeclaration("literal", datatype),),
                emission="guaranteed",
                output=OutputRequirement(
                    allow_null=True,
                    allow_empty=True,
                    minimum_values=1,
                    maximum_values=maximum,
                ),
            ),
        ),
    )
    return ConsolidationCheck(
        "native accession",
        lambda source: check_rdf_emission(
            source, (model,), (RdfModelUsage("Record", EX.record),)
        ),
    )


def test_agreeing_accession_additive_labels_and_conflicting_document_origins() -> None:
    """Complementary labels merge; immutable conflicts identify both documents."""
    first, second = (
        _document("First", Literal("stable", datatype=XSD.string), "First label"),
        _document("Second", Literal("stable", datatype=XSD.string), "Second label"),
    )
    index = prepare_corpus((first, second))
    assert index.report.verified and len(index.entities) == 1
    assert {item.origin.identifier for item in index.entities[0].contributions} == {
        "First",
        "Second",
    }
    policies = (PredicateAgreement(EX.accession),)
    result = consolidate_entities(
        index,
        agreements=policies,
        additive_predicates=(EX.label, RDF.type),
        checks=(_native(),),
    )
    assert result.report.verified and result.source is not None
    assert set(result.source.graph().objects(EX.record, EX.label)) == {
        Literal("First label", datatype=XSD.string),
        Literal("Second label", datatype=XSD.string),
    }
    assert set(result.source.graph().objects(EX.record, RDF.type)) == {
        EX.First,
        EX.Second,
    }
    assert result.merge_report is not None and result.merge_report.verified
    schema = Graph().add((EX.First, OWL.disjointWith, EX.Second))
    logical = consolidate_entities(
        index,
        agreements=policies,
        additive_predicates=(EX.label, RDF.type),
        checks=(
            ConsolidationCheck(
                "selected named logical obligations",
                lambda source: check_data_compatibility(source, schema),
            ),
        ),
    )
    assert logical.source is None and any(
        issue.code == "data.disjoint_conflict" for issue in logical.report.issues
    )
    changed = prepare_corpus(
        (first, _document("Second", Literal("changed"), "Second label"))
    )
    conflict = consolidate_entities(
        changed,
        agreements=policies,
        additive_predicates=(EX.label, RDF.type),
        checks=(_native(),),
    )
    assert conflict.source is None and conflict.report.outcome == "disagreement"
    issue = next(
        item
        for item in conflict.report.issues
        if item.code == "data.consolidation_conflict"
    )
    assert issue.evidence is not None
    contributions = issue.evidence["contributions"]
    assert isinstance(contributions, list)
    assert {item["document"] for item in contributions} == {"First", "Second"}
    assert {item["document_iri"] for item in contributions} == {
        str(EX.First),
        str(EX.Second),
    }


def test_exact_versus_explicit_literal_value_agreement_preserves_terms() -> None:
    """Value agreement never rewrites original lexical forms or RDF counts."""
    index = prepare_corpus(
        (
            _document("First", Literal("01", datatype=XSD.integer, normalize=False)),
            _document("Second", Literal("1", datatype=XSD.integer, normalize=False)),
        )
    )
    exact = consolidate_entities(
        index,
        agreements=(PredicateAgreement(EX.accession),),
        additive_predicates=(RDF.type,),
        checks=(_native(str(XSD.integer), 2),),
    )
    assert exact.source is None and exact.report.outcome == "disagreement"
    semantic = consolidate_entities(
        index,
        agreements=(PredicateAgreement(EX.accession, "owl2-datatype-map"),),
        additive_predicates=(RDF.type,),
        checks=(_native(str(XSD.integer), 2),),
    )
    assert semantic.source is not None and semantic.report.verified
    assert {
        str(value) for value in semantic.source.graph().objects(EX.record, EX.accession)
    } == {"01", "1"}
    unknown = prepare_corpus(
        (
            _document("First", Literal("NaN", datatype=XSD.double)),
            _document("Second", Literal("NaN", datatype=XSD.double)),
        )
    )
    result = consolidate_entities(
        unknown,
        agreements=(PredicateAgreement(EX.accession, "owl2-datatype-map"),),
        additive_predicates=(RDF.type,),
        checks=(_native(str(XSD.double)),),
    )
    assert result.source is None and not result.report.verified


def test_independent_blank_files_and_named_physical_participation() -> None:
    """Independent blank labels and distinct graph partitions never cross-join."""
    documents = tuple(
        CorpusDocument(
            name,
            prepare_rdf(
                RdfSource(
                    '_:same <https://example.org/corpus/accession> "' + name + '" .',
                    format="turtle",
                    document_iri=str(EX[name]),
                )
            ),
        )
        for name in ("First", "Second")
    )
    index = prepare_corpus(documents)
    assert len(index.entities) == 2 and all(
        isinstance(entity.subject, PatchBlank) for entity in index.entities
    )

    def blanks(source: PreparedSource) -> Report:
        subjects = set(source.graph().subjects())
        return Report(
            "independent blanks",
            1,
            ()
            if len(subjects) == 2
            else (Issue("test.blank_join", "Independent blank subjects joined"),),
        )

    candidate = consolidate_entities(
        index, checks=(ConsolidationCheck("two blanks", blanks),)
    )
    assert candidate.report.verified and candidate.source is not None
    left, right = Dataset(), Dataset()
    left.graph(EX.one).add(
        (EX.record, EX.accession, Literal("first", datatype=XSD.string))
    )
    left.graph(EX.empty)
    right.graph(EX.two).add(
        (EX.record, EX.accession, Literal("second", datatype=XSD.string))
    )
    pair = prepare_corpus(
        (
            CorpusDocument("one", prepare_rdf(left)),
            CorpusDocument("two", prepare_rdf(right)),
        )
    )

    def partitions(source: PreparedSource) -> Report:
        valid = set(source.graph(EX.one).objects(EX.record, EX.accession)) == {
            Literal("first", datatype=XSD.string)
        } and set(source.graph(EX.two).objects(EX.record, EX.accession)) == {
            Literal("second", datatype=XSD.string)
        }
        return Report(
            "physical partitions",
            1,
            ()
            if valid
            else (Issue("test.partitions", "Physical graph partitions changed"),),
        )

    retained = consolidate_entities(
        pair,
        agreements=(PredicateAgreement(EX.accession),),
        checks=(ConsolidationCheck("partitions", partitions),),
    )
    assert retained.source is not None and retained.report.verified
    assert EX.empty in {
        graph.identifier for graph in retained.source.dataset().graphs()
    }
    united = consolidate_entities(
        pair,
        agreements=(PredicateAgreement(EX.accession),),
        union=True,
        checks=(_native(),),
    )
    assert united.source is None and united.report.outcome == "disagreement"


def test_authorized_crop_bases_and_owned_index_snapshot() -> None:
    """Hidden graphs stay hidden and later caller literal/base edits cannot leak."""
    source = prepare_rdf(
        RdfSource(
            '<record> <accession> "kept" .',
            format="turtle",
            document_iri="https://logical.example/root",
            effective_url="https://retrieved.example/base/",
        )
    )
    index = prepare_corpus((CorpusDocument("relative", source),))
    assert str(index.entities[0].subject) == "https://retrieved.example/base/record"
    assert index.snapshots[0].origin.document_iri == "https://logical.example/root"
    assert index.snapshots[0].origin.effective_url == "https://retrieved.example/base/"
    literal = next(source.graph().objects())
    assert isinstance(literal, Literal)
    literal._datatype = XSD.integer
    source.document_iri = "urn:changed"
    assert index.snapshots[0].origin.document_iri == "https://logical.example/root"

    def original(source: PreparedSource) -> Report:
        value = next(source.graph().objects())
        return Report(
            "owned fields",
            1,
            ()
            if isinstance(value, Literal)
            and str(value) == "kept"
            and value.datatype in (None, XSD.string)
            else (Issue("test.changed", "Captured literal changed"),),
        )

    assert consolidate_entities(
        index, checks=(ConsolidationCheck("owned original", original),)
    ).report.verified
    dataset = Dataset()
    dataset.graph(EX.selected).add(
        (EX.record, EX.accession, Literal("stable", datatype=XSD.string))
    )
    dataset.graph(EX.hidden).add((EX.record, EX.accession, Literal("secret")))
    full = prepare_rdf(dataset)
    narrowed = prepare_corpus((CorpusDocument("selected", full, (EX.selected,)),))

    def crop(source: PreparedSource) -> Report:
        names = {graph.identifier for graph in source.dataset().graphs()}
        return Report(
            "crop",
            1,
            ()
            if EX.hidden not in names
            and set(source.graph(EX.selected).objects(EX.record, EX.accession))
            == {Literal("stable", datatype=XSD.string)}
            else (Issue("test.scope_leak", "Unselected graph leaked"),),
        )

    assert consolidate_entities(
        narrowed, checks=(ConsolidationCheck("authorized crop", crop),)
    ).report.verified
    pinned = prepare_rdf(
        RdfSource(
            '<https://example.org/corpus/selected> { <urn:s> <urn:p> "shown" } '
            '<https://example.org/corpus/hidden> { <urn:s> <urn:p> "secret" }',
            format="trig",
            graph_name=str(EX.selected),
        )
    )
    assert consolidate_entities(
        prepare_corpus((CorpusDocument("pinned", pinned),)),
        checks=(
            ConsolidationCheck(
                "pinned crop",
                lambda source: Report(
                    "pinned",
                    int(
                        EX.hidden
                        not in {graph.identifier for graph in source.dataset().graphs()}
                    ),
                ),
            ),
        ),
    ).report.verified


def test_unselected_additions_missing_fields_and_required_postconditions() -> None:
    """No implicit additive acceptance or successful publication with failed checks."""
    index = prepare_corpus(
        (
            _document("First", Literal("stable", datatype=XSD.string), "a"),
            _document("Second", Literal("stable", datatype=XSD.string), "b"),
        )
    )
    policies = (PredicateAgreement(EX.accession),)
    missing_policy = consolidate_entities(
        index, agreements=policies, checks=(_native(),)
    )
    assert (
        missing_policy.source is None and missing_policy.report.outcome == "unverified"
    )
    assert (
        consolidate_entities(
            index, agreements=policies, additive_predicates=(EX.label, RDF.type)
        ).source
        is None
    )
    failed = consolidate_entities(
        index,
        agreements=policies,
        additive_predicates=(EX.label, RDF.type),
        checks=(_native(str(XSD.integer)),),
    )
    assert failed.source is None and any(
        item.code == "emission.value_mismatch" for item in failed.report.issues
    )
    missing = prepare_corpus(
        (
            _document("First", Literal("stable", datatype=XSD.string)),
            _document("Second", None, "description"),
        )
    )
    result = consolidate_entities(
        missing,
        agreements=policies,
        additive_predicates=(EX.label, RDF.type),
        checks=(_native(),),
    )
    assert result.source is None and result.report.outcome == "disagreement"
    conflict = consolidate_entities(
        index,
        agreements=policies,
        additive_predicates=(EX.accession,),
        checks=(_native(),),
    )
    assert conflict.report.outcome == "input_error" and conflict.source is None


def test_finite_capture_callback_once_failure_and_candidate_limits() -> None:
    """Finite capture, raw reports and callback failures preserve operation truth."""
    first, second = (
        _document("First", Literal("stable", datatype=XSD.string)),
        _document("Second", Literal("stable", datatype=XSD.string)),
    )

    class Indexed(Sequence[CorpusDocument]):
        def __len__(self) -> int:
            return 2

        @overload
        def __getitem__(self, key: int) -> CorpusDocument: ...
        @overload
        def __getitem__(self, key: slice) -> Sequence[CorpusDocument]: ...
        def __getitem__(
            self, key: int | slice
        ) -> CorpusDocument | Sequence[CorpusDocument]:
            if key == 0:
                return first
            if key == 1:
                return second
            raise IndexError(key)

        def __iter__(self) -> Iterator[CorpusDocument]:
            raise AssertionError("Arbitrary iteration is outside finite capture")

    index = prepare_corpus(Indexed())
    assert index.report.verified
    calls = []

    def check(source: PreparedSource) -> Report:
        calls.append(source)
        return _native().check(source)

    result = consolidate_entities(
        index,
        agreements=(PredicateAgreement(EX.accession),),
        additive_predicates=(RDF.type,),
        checks=(ConsolidationCheck("once", check),),
    )
    assert result.report.verified and len(calls) == 1
    shortened = replace(index, snapshots=index.snapshots[:1])
    assert consolidate_entities(shortened, checks=(_native(),)).source is None

    def mutate(source: PreparedSource) -> Report:
        value = next(source.graph().objects(EX.record, EX.accession))
        assert isinstance(value, Literal)
        value._datatype = XSD.integer
        return Report("caller mutation", 1)

    isolated = consolidate_entities(
        index,
        agreements=(PredicateAgreement(EX.accession),),
        additive_predicates=(RDF.type,),
        checks=(ConsolidationCheck("mutate private input", mutate), _native()),
    )
    assert isolated.source is not None and isolated.report.verified
    assert {
        value.datatype
        for value in isolated.source.graph().objects(EX.record, EX.accession)
        if isinstance(value, Literal)
    } == {XSD.string}
    deliberate = InputError("caller failure")

    def fail(source: PreparedSource) -> Report:
        raise deliberate

    with pytest.raises(InputError) as caught:
        consolidate_entities(
            index,
            additive_predicates=(RDF.type,),
            checks=(ConsolidationCheck("caller", fail),),
        )
    assert caught.value is deliberate
    huge = CorpusDocument("x" * 100000, first.source)
    assert (
        prepare_corpus((huge,), limits=Limits(max_bytes=1000)).report.outcome
        == "exhausted"
    )
    bounded = consolidate_entities(
        index,
        agreements=(PredicateAgreement(EX.accession),),
        additive_predicates=(RDF.type,),
        checks=(_native(),),
        limits=Limits(max_work=10),
    )
    assert bounded.source is None and bounded.report.outcome == "exhausted"
    conflict_index = prepare_corpus((first, _document("Second", Literal("other"))))
    quota = consolidate_entities(
        conflict_index,
        agreements=(PredicateAgreement(EX.accession),),
        checks=(_native(),),
        limits=Limits(max_issues=1),
    )
    assert quota.source is None and any(
        item.kind == "finding" for item in quota.report.issues
    )
