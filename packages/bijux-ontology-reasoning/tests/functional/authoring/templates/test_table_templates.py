"""Real ROBOT table rows, native typed output and essential partial-output guards."""

import csv
import io
import os
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.authoring.templates import (
    TableCompilation,
    TemplateBackend,
    TemplateColumn,
    TemplateIdentifier,
    TemplateTerm,
    compile_table_template,
    prepare_template_backend,
)
from bijux_ontology_reasoning.axioms.types import (
    AnnotationAssertion,
    IriValue,
    NamedEntity,
    NativeLiteral,
    ObjectProperty,
    ObjectQuantifier,
    SubClassOf,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document

from bijux_ontology.limits import Budget, Limits

PARENT = NamedEntity("class", "https://example.org/Parent")
FILLER = NamedEntity("class", "https://example.org/Filler")
CHILD = NamedEntity("class", "https://example.org/Child")
OTHER = NamedEntity("class", "https://example.org/Other")
PART = NamedEntity("object_property", "https://example.org/part_of")
DEFINITION = NamedEntity("annotation_property", "https://example.org/definition")
COLUMNS = (
    TemplateColumn("Identifier", "ID"),
    TemplateColumn("Label", "LABEL"),
    TemplateColumn("Parent", "SC %"),
    TemplateColumn("Part", "SC part_of some %"),
    TemplateColumn("Definition", "A definition"),
)
IDENTIFIERS = (
    TemplateIdentifier("ex:Child", CHILD.iri),
    TemplateIdentifier("ex:Other", OTHER.iri),
)
TERMS = (
    TemplateTerm("ex:Parent", PARENT),
    TemplateTerm("ex:Filler", FILLER),
    TemplateTerm("part_of", PART),
    TemplateTerm("definition", DEFINITION),
)
POSTCONDITIONS = (
    SubClassOf(CHILD, PARENT, ()),
    SubClassOf(OTHER, ObjectQuantifier("some", ObjectProperty(PART.iri), FILLER), ()),
)
LITERAL = 'literal " ) SubClassOf(<urn:evil> <urn:Any>)'


def bindings() -> OwlDocument:
    """Supply only explicit local native declarations and unambiguous labels."""
    return prepare_owl_document(
        """Ontology(<urn:bindings>
Declaration(Class(<https://example.org/Parent>))
Declaration(Class(<https://example.org/Filler>))
Declaration(ObjectProperty(<https://example.org/part_of>))
Declaration(AnnotationProperty(<https://example.org/definition>))
AnnotationAssertion(<http://www.w3.org/2000/01/rdf-schema#label>
 <https://example.org/part_of> "part_of")
AnnotationAssertion(<http://www.w3.org/2000/01/rdf-schema#label>
 <https://example.org/definition> "definition")
)""",
        format="functional",
        document_iri="urn:bindings",
    )


def table(delimiter: str = ",", rows: tuple[tuple[str, ...], ...] | None = None) -> str:
    """Use the ordinary maintained CSV serializer, including literal quotes/newlines."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow([column.header for column in COLUMNS])
    writer.writerow([column.template for column in COLUMNS])
    writer.writerows(
        rows
        if rows is not None
        else (
            (
                "ex:Child",
                LITERAL,
                "ex:Parent",
                "ex:Filler",
                "a definition\nwith detail",
            ),
            ("ex:Other", "Other", "ex:Parent", "ex:Filler", "another definition"),
        )
    )
    return stream.getvalue()


@pytest.fixture
def engines(tmp_path: Path) -> tuple[TemplateBackend, OwlProfileBackend]:
    """Use actual explicit installed native releases; no ambient Java discovery."""
    java, bundle, pin, robot_java, robot_jar = (
        os.environ.get(name)
        for name in (
            "BIJUX_OWL_JAVA",
            "BIJUX_OWL_BUNDLE",
            "BIJUX_OWL_MANIFEST_SHA256",
            "BIJUX_ROBOT_JAVA",
            "BIJUX_ROBOT_JAR",
        )
    )
    if not (java and bundle and pin and robot_java and robot_jar):
        pytest.skip("Explicit pinned ROBOT and OWL native distributions are required")
    runtime = prepare_template_backend(
        Path(robot_java),
        Path(robot_jar),
        sha256="53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26",
        artifacts=tmp_path / "templates",
        limits=Limits(max_total_bytes=128_000_000),
    )
    backend = prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "native"
    )
    return runtime, backend


def run(
    engines: tuple[TemplateBackend, OwlProfileBackend],
    content: str,
    *,
    identifiers: tuple[TemplateIdentifier, ...] = IDENTIFIERS,
    terms: tuple[TemplateTerm, ...] = TERMS,
    columns: tuple[TemplateColumn, ...] = COLUMNS,
    delimiter: str = "csv",
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> TableCompilation:
    """Call the real public compiler with explicit row/pattern/consumer contracts."""
    return compile_table_template(
        *engines,
        content,
        columns=columns,
        bindings=bindings(),
        identifiers=identifiers,
        terms=terms,
        prefixes=(("ex", "https://example.org/"),),
        ontology_iri="urn:generated",
        postconditions=POSTCONDITIONS,
        delimiter=delimiter,
        budget=budget,
        cancel=cancel,
        timeout=60,
    )


@pytest.mark.parametrize("separator,syntax", [(",", "csv"), ("\t", "tsv")])
def test_real_table_creates_complete_labels_parents_restrictions_and_definitions(
    engines: tuple[TemplateBackend, OwlProfileBackend],
    separator: str,
    syntax: str,
) -> None:
    original = table(separator)
    result = run(engines, original, delimiter=syntax)
    assert result.report.verified, result.report.to_dict()
    assert result.output is not None and result.inspection is not None
    assert result.table == original.encode() and result.bindings is not None
    assert result.native_version == "ROBOT version 1.9.11"
    assert all(
        receipt.row in (3, 4)
        and receipt.output_report is not None
        and receipt.output_report.verified
        for receipt in result.rows
    )
    assert result.conversion is not None and result.conversion.lossless is True
    assert result.obligations is not None and result.obligations.consistent is True
    axioms = {record.axiom for record in result.inspection.axioms}
    assert (
        AnnotationAssertion(
            "http://www.w3.org/2000/01/rdf-schema#label",
            IriValue(CHILD.iri),
            NativeLiteral(LITERAL, "http://www.w3.org/2001/XMLSchema#string", None),
            (),
        )
        in axioms
    )
    assert not any(
        "urn:evil" in repr(axiom)
        for axiom in axioms
        if not isinstance(axiom, AnnotationAssertion)
    )


def test_predispatch_rows_duplicates_scope_and_pattern_guards_are_located(
    engines: tuple[TemplateBackend, OwlProfileBackend],
) -> None:
    cases = (
        (
            table(
                rows=(
                    ("ex:Child", "A", "not (", "ex:Filler", "def"),
                    ("ex:Other", "B", "ex:Parent", "ex:Filler", "def"),
                )
            ),
            3,
        ),
        (
            table(
                rows=(
                    ("ex:Child", "A", "ex:Parent", "ex:Filler", "def"),
                    ("ex:Child", "different", "ex:Parent", "ex:Filler", "def"),
                )
            ),
            4,
        ),
        (table(rows=((),)), 3),
        (table(rows=(("ex:Child", "wrong width"),)), 3),
    )
    for content, number in cases:
        result = run(engines, content)
        assert result.output is None and result.native_version is None
        assert any(f"table[{number}]" in issue.location for issue in result.report)
    bad_pattern = run(
        engines,
        table(),
        columns=(TemplateColumn("Identifier", "ID"), TemplateColumn("Label", "AP")),
    )
    assert bad_pattern.output is None and any(
        "table[2]" in issue.location for issue in bad_pattern.report
    )
    bad_header = run(engines, table().replace("Identifier,Label", "Wrong,Label", 1))
    assert bad_header.output is None and any(
        "table[1]" in issue.location for issue in bad_header.report
    )


def test_native_unresolved_term_has_actual_row_diagnostic_and_no_partial_output(
    engines: tuple[TemplateBackend, OwlProfileBackend],
) -> None:
    content = table(
        rows=(
            ("ex:Child", "good", "ex:Parent", "ex:Filler", "def"),
            ("ex:Other", "bad", "unresolved arbitrary label", "ex:Filler", "def"),
        )
    )
    result = run(
        engines,
        content,
        terms=TERMS + (TemplateTerm("unresolved arbitrary label", PARENT),),
    )
    assert result.output is None and result.native_version == "ROBOT version 1.9.11"
    assert any(
        issue.code == "templates.native_row_error" and "table[4]" in issue.location
        for issue in result.report
    ), result.report.to_dict()
    assert result.rows[1].output_report is not None
    assert not result.rows[1].output_report.complete
    assert result.rows[0].output_report is None


def test_wrong_expansion_is_rejected_by_exact_typed_row_coverage_and_limits(
    engines: tuple[TemplateBackend, OwlProfileBackend],
) -> None:
    identifiers = (
        TemplateIdentifier("ex:Child", "https://different.example/Child"),
        IDENTIFIERS[1],
    )
    result = run(engines, table(), identifiers=identifiers)
    assert result.output is None and result.inspection is not None
    assert any(issue.code == "templates.row_output_missing" for issue in result.report)
    budget = Budget(Limits(max_total_bytes=256))
    stopped = run(engines, table(), budget=budget)
    assert stopped.report.outcome == "exhausted" and stopped.output is None
    parser_limit = csv.field_size_limit(128)
    try:
        long_cell = run(
            engines,
            table(rows=(("ex:Child", "x" * 256, "ex:Parent", "ex:Filler", "def"),)),
        )
        assert long_cell.output is None and long_cell.report.outcome == "exhausted"
        assert any(
            "CSV parser field allowance" in issue.message for issue in long_cell.report
        )
    finally:
        csv.field_size_limit(parser_limit)
    event = Event()
    event.set()
    assert run(engines, table(), cancel=event).output is None
