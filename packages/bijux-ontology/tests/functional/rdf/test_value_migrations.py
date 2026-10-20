"""Selected exact value migrations remain atomic and require explicit acceptance."""

import json
import threading

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.declarations.types import OutputRequirement, ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.path_fields import PathFieldUsage, check_path_fields
from bijux_ontology.rdf.migrations import MigrationCheck
from bijux_ontology.rdf.migrations.values import (
    LiteralConversion,
    ValueMap,
    ValueMigrationCandidate,
    ValueSelection,
    migrate_values,
)
from bijux_ontology.rdf.records.types import PatchLiteral
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.terms import literal_term, term_key

EX = Namespace("urn:value-migration:")
SELECTION = ValueSelection((str(EX.amount),))
CONVERSION = LiteralConversion(str(XSD.string), str(XSD.integer))


def _check(datatype: str, *, graph: str | None = None) -> MigrationCheck:
    """Check an actual logical value field, without deriving data from its contract."""
    field = PathFieldDeclaration(
        "amount",
        EX.amount,
        (ValueDeclaration("literal", datatype=datatype),),
        output=OutputRequirement(allow_null=True, allow_empty=True, minimum_values=1),
    )

    def check(source: PreparedSource) -> Report:
        return check_path_fields(
            source, (PathFieldUsage(field, EX.record),), graph_name=graph
        )

    return MigrationCheck("amount", check)


def _migrate(
    source: PreparedSource,
    action: LiteralConversion | ValueMap = CONVERSION,
    *,
    allow_merge: bool = False,
    limits: Limits | None = None,
    cancel: threading.Event | None = None,
) -> ValueMigrationCandidate:
    """Select actual after-datatype contracts for the essential conversion profile."""
    return migrate_values(
        source,
        SELECTION,
        action,
        before_checks=(_check(str(XSD.integer)),),
        after_checks=(_check(str(XSD.integer)),),
        allow_merge=allow_merge,
        limits=limits,
        cancel=cancel,
    )


def test_string_integer_repair_exact_lexical_noop_and_accepted_export() -> None:
    """A real failed baseline can be repaired without rewriting lexical bytes."""
    source = prepare_rdf(Graph().add((EX.record, EX.amount, Literal("+001"))))
    result = _migrate(source)
    assert (
        result.source is not None
        and result.patch is not None
        and result.report.verified
    )
    assert not result.before[0].verified and result.after[0].verified
    assert str(result.source.graph().value(EX.record, EX.amount)) == "+001"
    original = source.graph().value(EX.record, EX.amount)
    assert isinstance(original, Literal) and original.datatype is None
    added = result.patch.additions[0].value
    assert isinstance(added, PatchLiteral) and added.lexical == "+001"
    assert export_migration(result, format="turtle").content is None
    exported = export_migration(result, accept=True, format="turtle")
    assert exported.content is not None and exported.report.verified
    noop = _migrate(
        result.source, LiteralConversion(str(XSD.integer), str(XSD.integer))
    )
    assert noop.report.verified and noop.patch is not None
    assert not noop.patch.additions and not noop.patch.removals
    assert noop.selected == 1 and noop.unchanged == 1 and noop.changed == 0


def test_invalid_samples_and_unsupported_conversions_are_atomic() -> None:
    """Invalid or unsupported selected values never expose a partial source."""
    for text in ("1.5", "1e3", " 1 ", "١"):
        graph = Graph().add((EX.record, EX.amount, Literal("01")))
        graph.add((EX.other, EX.amount, Literal(text)))
        source = prepare_rdf(graph)
        result = _migrate(source)
        assert result.source is None and not result.report.verified
        assert {str(v) for v in source.graph().objects(None, EX.amount)} == {"01", text}
        assert any(i.code == "literal.lexical_invalid" for i in result.report.issues)
        witnesses = [
            json.loads(i.evidence_json)
            for i in result.report.issues
            if i.code == "rdf.value_migration_rejected" and i.evidence_json
        ]
        assert any(w["quad"][2][1] == text for w in witnesses)
    for value, action in (
        (
            literal_term("128", datatype=str(XSD.integer)),
            LiteralConversion(str(XSD.integer), str(XSD.byte)),
        ),
        (
            literal_term("1.0", datatype=str(XSD.decimal)),
            LiteralConversion(str(XSD.decimal), str(XSD.integer)),
        ),
        (
            literal_term("9007199254740993", datatype=str(XSD.integer)),
            LiteralConversion(str(XSD.integer), str(XSD.double)),
        ),
        (Literal("1", lang="en"), CONVERSION),
    ):
        result = _migrate(
            prepare_rdf(Graph().add((EX.record, EX.amount, value))), action
        )
        assert result.source is None and not result.report.verified
    safe = migrate_values(
        prepare_rdf(
            Graph().add(
                (EX.record, EX.amount, literal_term("127", datatype=str(XSD.integer)))
            )
        ),
        SELECTION,
        LiteralConversion(str(XSD.integer), str(XSD.byte)),
        before_checks=(_check(str(XSD.integer)),),
        after_checks=(_check(str(XSD.byte)),),
    )
    assert safe.source is not None and safe.report.verified


def test_exact_maps_language_permission_and_intentional_collapse() -> None:
    """Map identities retain numeric lexicals and require explicit lossy intent."""
    first, second = Literal("01"), Literal("1")
    source = prepare_rdf(
        Graph().add((EX.record, EX.amount, first)).add((EX.record, EX.amount, second))
    )
    target = literal_term("1", datatype=str(XSD.integer))
    mapping = ValueMap(((first, target), (second, target)))
    rejected = _migrate(source, mapping)
    assert rejected.source is None and any(
        i.code == "rdf.value_migration_collision" for i in rejected.report.issues
    )
    allowed = _migrate(source, mapping, allow_merge=True)
    assert allowed.source is not None and allowed.report.verified
    assert len(allowed.source.graph()) == 1
    original = Literal("hello", lang="en")
    translated = Literal("bonjour", lang="fr")
    source = prepare_rdf(Graph().add((EX.record, EX.amount, original)))
    rule = ValueSelection((str(EX.amount),))
    checks = (_check(str(XSD.string)),)
    denied = migrate_values(
        source, rule, ValueMap(((original, translated),)), after_checks=checks
    )
    assert denied.source is None and not denied.report.verified

    def check_french(candidate: PreparedSource) -> Report:
        """Require the actual rewritten literal's declared French language."""
        actual = candidate.graph().value(EX.record, EX.amount)
        return Report(
            "language",
            int(isinstance(actual, Literal) and actual.language == "fr"),
            requested=1,
        )

    accepted = migrate_values(
        source,
        rule,
        ValueMap(((original, translated),), allow_language_change=True),
        before_checks=(MigrationCheck("before", lambda _: Report("selected", 1)),),
        after_checks=(MigrationCheck("after", check_french),),
    )
    assert accepted.source is not None and accepted.report.verified

    iri_source = prepare_rdf(
        Graph().add((EX.record, EX.amount, EX.old)).add((EX.other, EX.amount, EX.old))
    )
    reference = PathFieldDeclaration(
        "reference",
        EX.amount,
        (ValueDeclaration("iri"),),
        output=OutputRequirement(allow_null=True, allow_empty=True, minimum_values=1),
    )
    check_reference = MigrationCheck(
        "reference",
        lambda s: check_path_fields(s, (PathFieldUsage(reference, EX.record),)),
    )
    rewritten = migrate_values(
        iri_source,
        ValueSelection((str(EX.amount),), subjects=(str(EX.record),)),
        ValueMap(((EX.old, EX.new),)),
        before_checks=(check_reference,),
        after_checks=(check_reference,),
    )
    assert rewritten.report.verified and rewritten.source is not None
    assert rewritten.source.graph().value(EX.record, EX.amount) == EX.new
    assert rewritten.source.graph().value(EX.other, EX.amount) == EX.old


def test_selected_contexts_and_owned_blank_subjects_preserve_unselected_terms() -> None:
    """The patch retains exact joint blanks, empty graphs and selected scope."""
    data = Dataset()
    data.graph(EX.G).add((EX.record, EX.amount, Literal("01")))
    data.graph(EX.G).add((EX.record, EX.other, Literal("unchanged")))
    data.graph(EX.hidden).add((EX.record, EX.amount, Literal("bad")))
    data.graph(EX.empty)
    source = prepare_rdf(RdfSource(data, graph_name=str(EX.G)))
    checks = (_check(str(XSD.integer)),)
    result = migrate_values(
        source, SELECTION, CONVERSION, before_checks=checks, after_checks=checks
    )
    assert result.source is not None and result.report.verified
    assert term_key(result.source.graph().value(EX.record, EX.other)) == term_key(
        Literal("unchanged")
    )
    assert EX.hidden not in {g.identifier for g in result.source.dataset().graphs()}
    whole = prepare_rdf(data)
    result = migrate_values(
        whole,
        ValueSelection((str(EX.amount),), graphs=(EX.G,)),
        CONVERSION,
        before_checks=(_check(str(XSD.integer), graph=str(EX.G)),),
        after_checks=(_check(str(XSD.integer), graph=str(EX.G)),),
    )
    assert result.source is not None and result.report.verified
    assert term_key(
        result.source.graph(str(EX.hidden)).value(EX.record, EX.amount)
    ) == term_key(Literal("bad"))
    assert EX.empty in {g.identifier for g in result.source.dataset().graphs()}
    blank = BNode()
    source = prepare_rdf(
        Graph().add((EX.record, EX.child, blank)).add((blank, EX.amount, Literal("01")))
    )
    result = migrate_values(
        source,
        SELECTION,
        CONVERSION,
        before_checks=(MigrationCheck("sample", lambda _: Report("sample", 1)),),
        after_checks=(MigrationCheck("sample", lambda _: Report("sample", 1)),),
    )
    assert result.source is not None and result.report.verified
    actual = result.source.graph().value(EX.record, EX.child)
    assert str(result.source.graph().value(actual, EX.amount)) == "01"


def test_required_after_contracts_unexercised_and_bounds_never_green() -> None:
    """Absent qualification and stopped work cannot manufacture acceptance."""
    source = prepare_rdf(Graph().add((EX.record, EX.amount, Literal("01"))))
    result = migrate_values(source, SELECTION, CONVERSION)
    assert (
        not result.report.verified
        and export_migration(result, accept=True, format="turtle").content is None
    )
    unknown = MigrationCheck("required", lambda _: Report("required", 0, requested=1))
    result = migrate_values(source, SELECTION, CONVERSION, after_checks=(unknown,))
    assert not result.report.verified
    result = migrate_values(source, ValueSelection((str(EX.absent),)), CONVERSION)
    assert result.source is None and not result.report.verified
    for limits in (Limits(max_work=1), Limits(max_bytes=100)):
        result = _migrate(source, limits=limits)
        assert not result.report.verified and result.report.outcome == "exhausted"
    invalid = prepare_rdf(
        Graph()
        .add((EX.record, EX.amount, Literal("1.5")))
        .add((EX.other, EX.amount, Literal("bad")))
    )
    exhausted = _migrate(invalid, limits=Limits(max_issues=1))
    assert exhausted.report.outcome == "exhausted"
    assert any(i.code == "literal.lexical_invalid" for i in exhausted.report.issues)
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    unsupported = migrate_values(
        prepare_rdf(Graph().add((EX.record, EX.amount, Literal(1)))),
        SELECTION,
        LiteralConversion(str(XSD.integer), str(XSD.double)),
        budget=spent,
    )
    assert unsupported.source is None and unsupported.report.outcome == "exhausted"
    stop = threading.Event()
    stop.set()
    assert not _migrate(source, cancel=stop).report.verified
    assert _migrate(source).report.verified
    with pytest.raises(InputError):
        ValueMap(
            (
                (Literal("same"), URIRef("urn:a")),
                (Literal("same", datatype=XSD.string), URIRef("urn:b")),
            )
        )
