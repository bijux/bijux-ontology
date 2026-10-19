"""Supplied Core shapes qualify actual explicitly mapped serializer output."""

from collections.abc import Sequence
from dataclasses import replace
from typing import cast, overload

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.shacl import (
    ShapeContractImport,
    ShapeFieldBinding,
    check_imported_output,
    check_imported_rdf,
    import_shape_contracts,
)
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_term, term_key
from bijux_ontology.shacl.checking import validate_shapes

EX = Namespace("https://example.invalid/import/")


def shapes() -> Graph:
    """Declare the exact selected ordinary integer property constraint."""
    graph = Graph()
    for triple in (
        (EX.Shape, RDF.type, SH.NodeShape),
        (EX.Shape, SH.targetNode, EX.record),
        (EX.Shape, SH.property, EX.Field),
        (EX.Field, SH.path, EX.value),
        (EX.Field, SH.datatype, XSD.integer),
        (EX.Field, SH.minCount, Literal(1)),
        (EX.Field, SH.maxCount, Literal(1)),
    ):
        graph.add(triple)
    return graph


def vocabulary() -> Graph:
    """Keep application property selection independent of shape targets."""
    graph = Graph()
    for predicate in (EX.value, EX.owner):
        graph.add((predicate, RDF.type, RDF.Property))
    graph.add((EX.Record, RDF.type, OWL.Class))
    return graph


def test_real_serializer_counts_datatype_alias_and_exact_returned_output() -> None:
    """A positive and mutations use the actual callback, native checks and backend."""
    pytest.importorskip("pyld")
    pytest.importorskip("pyshacl")
    source = prepare_rdf(shapes())
    imported = import_shape_contracts(
        source,
        [EX.Shape],
        [ShapeFieldBinding(EX.Field, "quantity", key="n")],
        model="Record",
    )
    assert not imported.rdf_contract_report.verified
    assert not imported.untranslated
    assert not imported.extraction.report.verified
    assert imported.inspection is not None and imported.inspection.source is source
    assert not imported.untranslated
    model = imported.extraction.declarations[0]
    assert model.types == ()
    assert model.fields[0].name == "quantity" and model.fields[0].key == "n"
    assert model.fields[0].output is not None
    assert not model.fields[0].output.require_property
    # Exact root type is independent application policy, never a shape target.
    calls = []

    def serializer(value: object, *, selected: bool) -> object:
        """Return precisely the supplied instance without preprocessing."""
        calls.append(selected)
        return value

    context = {"n": str(EX.value)}
    for value, expected in ((1, True), ("1", False), (None, False)):
        output: dict[str, object] = {"@id": str(EX.record), "@type": str(EX.Record)}
        if value is not None:
            output["n"] = value
        checked = check_imported_output(
            serializer(output, selected=True),
            imported,
            [ModelUsage("Record")],
            vocabulary=vocabulary(),
            types=(str(EX.Record),),
            context=context,
        )
        assert calls[-1] is True and len(calls) <= 3
        assert checked.output == output
        assert checked.report.verified is expected, checked.report.to_dict()
        projected = prepare_jsonld({"@context": context, **output})
        assert projected.source is not None
        backend = validate_shapes(projected.source, source, preflight=True)
        assert backend.conforms is expected, backend.report.to_dict()
    assert calls == [True, True, True]
    context["alias"] = str(EX.value)
    base = {"@id": str(EX.record), "@type": str(EX.Record), "n": 1}
    aggregate_cases: tuple[
        tuple[dict[str, object], tuple[str | int, ...], bool], ...
    ] = (
        ({**base, "alias": 2}, (), False),
        ({"@graph": [base, {"@id": str(EX.record), "alias": 2}]}, ("@graph", 0), False),
        ({"@id": str(EX.record), "@type": str(EX.Record), "alias": 1}, (), True),
    )
    for output, path, expected in aggregate_cases:
        checked = check_imported_output(
            output,
            imported,
            [ModelUsage("Record", path)],
            vocabulary=vocabulary(),
            types=(str(EX.Record),),
            context=context,
        )
        assert checked.report.verified is expected, checked.report.to_dict()
        projected = prepare_jsonld({"@context": context, **output})
        assert projected.source is not None
        assert (
            validate_shapes(projected.source, source, preflight=True).conforms
            is expected
        )


def test_inverse_value_alternatives_optional_null_and_exact_enum_identity() -> None:
    """Finite logical alternatives and term membership retain RDF distinctions."""
    source = prepare_rdf(
        """@prefix ex:<https://example.invalid/import/>.
        @prefix sh:<http://www.w3.org/ns/shacl#>.
        @prefix xsd:<http://www.w3.org/2001/XMLSchema#>.
        ex:P sh:path [sh:inversePath ex:owner]; sh:nodeKind sh:IRI;
             sh:minCount 1; sh:maxCount 1.
        ex:V sh:path ex:value; sh:or ([sh:datatype xsd:integer] [sh:nodeKind sh:IRI]).
    """,
        format="turtle",
    )
    imported = import_shape_contracts(
        source,
        [EX.P, EX.V],
        [ShapeFieldBinding(EX.P, "owner"), ShapeFieldBinding(EX.V, "value")],
        model="R",
    )
    assert not imported.rdf_contract_report.verified
    assert not imported.untranslated
    assert imported.extraction.declarations[0].fields[0].reverse
    for actual, expected in (
        (literal_term("01", datatype=XSD.integer), True),
        (EX.named, True),
        (Literal("bad"), False),
    ):
        data = Graph()
        data.add((EX.person, EX.owner, EX.record))
        data.add((EX.record, EX.value, actual))
        report = check_imported_rdf(
            prepare_rdf(data),
            imported,
            [RdfModelUsage("R", EX.record)],
        )
        assert report.verified is expected, report.to_dict()
    graph = Graph()
    graph.add((EX.E, SH.path, EX.value))
    graph.add((EX.E, SH["in"], EX.list))
    graph.add((EX.list, RDF.first, literal_term("01", datatype=XSD.integer)))
    graph.add((EX.list, RDF.rest, RDF.nil))
    enumeration = import_shape_contracts(
        prepare_rdf(graph), [EX.E], [ShapeFieldBinding(EX.E, "value")], model="R"
    )
    assert not enumeration.rdf_contract_report.verified
    assert not enumeration.untranslated
    for lexical in ("01", "1"):
        data = Graph()
        data.add((EX.record, EX.value, literal_term(lexical, datatype=XSD.integer)))
        assert check_imported_rdf(
            prepare_rdf(data),
            enumeration,
            [RdfModelUsage("R", EX.record)],
        ).verified is (lexical == "01")
    pytest.importorskip("pyld")
    for output in (
        {"@id": str(EX.record), "@type": str(EX.Record)},
        {"@id": str(EX.record), "@type": str(EX.Record), "value": None},
    ):
        result = check_imported_output(
            output,
            enumeration,
            [ModelUsage("R")],
            vocabulary=vocabulary(),
            types=(str(EX.Record),),
            context={"value": str(EX.value)},
        )
        assert result.report.verified, result.report.to_dict()


def test_class_conformance_never_invents_an_exact_asserted_type_promise() -> None:
    """The original subclass-aware obligation remains required and executable."""
    pytest.importorskip("pyshacl")
    graph = Graph()
    graph.add((EX.P, SH.path, EX.value))
    graph.add((EX.P, SH["class"], EX.Parent))
    graph.add((EX.P, SH.targetNode, EX.record))
    source = prepare_rdf(graph)
    imported = import_shape_contracts(
        source, [EX.P], [ShapeFieldBinding(EX.P, "value")], model="R"
    )
    assert not imported.extraction.report.verified
    assert imported.extraction.report.outcome == "unverified"
    assert any(
        item.predicate == SH["class"] and item.value == EX.Parent
        for item in imported.untranslated
    )
    assert all(
        not value.types and value.role is None
        for value in imported.extraction.declarations[0].fields[0].values
    )
    data = Graph()
    data.add((EX.record, EX.value, EX.child))
    data.add((EX.child, RDF.type, EX.Sub))
    data.add((EX.Sub, RDFS.subClassOf, EX.Parent))
    backend = validate_shapes(data, source, preflight=True)
    assert backend.conforms is True
    native = check_rdf_emission(
        prepare_rdf(data),
        imported.extraction.declarations,
        [RdfModelUsage("R", EX.record)],
    )
    assert native.verified, native.to_dict()
    document = {
        "@id": str(EX.record),
        "@type": str(EX.Record),
        "v": {"@id": str(EX.child), "@type": str(EX.Sub)},
    }
    result = check_imported_output(
        document,
        imported,
        [ModelUsage("R")],
        vocabulary=vocabulary(),
        context={"v": str(EX.value)},
        types=(str(EX.Record),),
    )
    assert result.report.outcome == "unverified", result.report.to_dict()
    assert not any(
        issue.code == "emission.types_mismatch" for issue in result.report.issues
    )
    assert not any(
        issue.code == "emission.types_mismatch"
        for issue in imported.extraction.report.issues
    )


def test_unbound_recursive_sparql_and_unsupported_alternatives_stay_required() -> None:
    """A partial contract retains original assertions rather than pruning siblings."""
    graph = shapes()
    graph.add((EX.Field, SH.node, EX.Child))
    graph.add((EX.Child, SH.node, EX.Child))
    graph.add((EX.Child, SH.sparql, EX.Query))
    graph.add((EX.Query, SH.select, Literal("SELECT $this WHERE {}")))
    source = prepare_rdf(graph)
    imported = import_shape_contracts(
        source, [EX.Shape], [ShapeFieldBinding(EX.Field, "n")], model="R"
    )
    assert imported.inspection is not None
    assert not imported.extraction.report.verified
    exact = {(item.shape, item.predicate, item.value) for item in imported.untranslated}
    assert (EX.Field, SH.node, EX.Child) in exact
    assert (EX.Child, SH.node, EX.Child) in exact
    assert (EX.Child, SH.sparql, EX.Query) in exact
    missing = import_shape_contracts(source, [EX.Shape], [], model="R")
    assert not missing.extraction.report.verified
    assert any(
        issue.code == "adapter.shape_binding_unverified"
        for issue in missing.extraction.report.issues
    )
    alternatives = prepare_rdf(
        """@prefix ex:<https://example.invalid/import/>.
      @prefix sh:<http://www.w3.org/ns/shacl#>.
      ex:P sh:path ex:value; sh:or ([sh:nodeKind sh:IRI] [sh:pattern "a"]).""",
        format="turtle",
    )
    partial = import_shape_contracts(
        alternatives, [EX.P], [ShapeFieldBinding(EX.P, "value")], model="R"
    )
    assert not partial.extraction.report.verified
    assert any(item.predicate == SH["or"] for item in partial.untranslated)
    assert any(item.predicate == SH.pattern for item in partial.untranslated)
    for kind in (SH.BlankNode, SH.BlankNodeOrLiteral):
        g = Graph()
        g.add((EX.P, SH.path, EX.value))
        g.add((EX.P, SH.nodeKind, kind))
        result = import_shape_contracts(
            prepare_rdf(g), [EX.P], [ShapeFieldBinding(EX.P, "value")], model="R"
        )
        assert any(item.predicate == SH.nodeKind for item in result.untranslated)
    nested = prepare_rdf(
        """@prefix ex:<https://example.invalid/import/>.
      @prefix sh:<http://www.w3.org/ns/shacl#>.
      ex:P sh:path ex:value; sh:node ex:N.
      ex:N sh:property ex:Q. ex:Q sh:path ex:owner; sh:minCount 1.""",
        format="turtle",
    )
    result = import_shape_contracts(
        nested,
        [EX.P],
        [ShapeFieldBinding(EX.P, "value"), ShapeFieldBinding(EX.Q, "owner")],
        model="R",
    )
    assert [field.name for field in result.extraction.declarations[0].fields] == [
        "value"
    ]
    assert not result.extraction.report.verified


def test_scope_ownership_metadata_and_late_cutoffs_never_certify_empty_imports() -> (
    None
):
    """Pins, exact blank ownership and retained obligations use the actual Budget."""
    dataset = Dataset()
    for triple in shapes():
        dataset.graph(EX.G).add(triple)
    dataset.graph(EX.H).add((EX.Field, SH.datatype, XSD.string))
    source = PreparedSource(
        dataset,
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
        graph_name=str(EX.G),
    )
    before = {
        tuple(term_key(term) for term in quad) for quad in source.dataset().quads()
    }
    binding = ShapeFieldBinding(EX.Field, "value")
    imported = import_shape_contracts(source, [EX.Shape], [binding], model="R")
    assert not imported.rdf_contract_report.verified
    assert not imported.untranslated
    with pytest.raises(InputError):
        ShapeContractImport()
    with pytest.raises(TypeError):
        replace(imported, untranslated=())
    assert before == {
        tuple(term_key(term) for term in quad) for quad in source.dataset().quads()
    }
    graph = Graph()
    blank = BNode()
    graph.add((blank, SH.path, EX.value))
    owned = prepare_rdf(graph)
    actual = next(owned.graph().subjects(SH.path))
    assert isinstance(actual, BNode)
    blank_import = import_shape_contracts(
        owned, [actual], [ShapeFieldBinding(actual, "value")], model="R"
    )
    assert blank_import.inspection is not None
    assert blank_import.inspection.report.verified
    foreign = BNode(str(actual))
    assert not import_shape_contracts(
        owned, [actual], [ShapeFieldBinding(foreign, "value")], model="R"
    ).extraction.report.verified
    huge = import_shape_contracts(
        source, [EX.Shape], [binding], model="😀" * 300, limits=Limits(max_bytes=1000)
    )
    assert (
        huge.extraction.report.outcome == "exhausted"
        and not huge.extraction.declarations
    )
    with pytest.raises(InputError):
        import_shape_contracts(
            source, [EX.Shape], [binding, replace(binding, name="other")], model="R"
        )
    with pytest.raises(InputError):
        import_shape_contracts(
            source, [EX.Shape], [binding], model="R", budget=cast(Budget, object())
        )

    class Changed(Sequence[ShapeFieldBinding]):
        """An unstable finite inventory cannot silently remove selected fields."""

        def __len__(self) -> int:
            return 1

        @overload
        def __getitem__(self, index: int) -> ShapeFieldBinding: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[ShapeFieldBinding]: ...

        def __getitem__(
            self, index: int | slice
        ) -> ShapeFieldBinding | Sequence[ShapeFieldBinding]:
            raise IndexError

    with pytest.raises(InputError):
        import_shape_contracts(source, [EX.Shape], Changed(), model="R")
    g = shapes()
    g.add((EX.Field, SH["class"], EX.Parent))
    partial = prepare_rdf(g)
    paid_cases = 0
    for cap in range(500, 18_000, 503):
        budget = Budget(Limits(max_work=cap))
        result = import_shape_contracts(
            partial, [EX.Shape], [binding], model="R", budget=budget
        )
        if any(
            issue.code == "adapter.shape_constraint_untranslated"
            for issue in budget._retained_issues.values()
        ):
            paid_cases += 1
            assert any(
                issue.code == "adapter.shape_constraint_untranslated"
                for issue in result.extraction.report.issues
            )
        if result.extraction.report.outcome == "exhausted":
            assert not result.extraction.report.verified
    assert paid_cases


def test_actual_list_heads_remain_unknown_without_poisoning_other_entities() -> None:
    """List representation is qualified on joined facts before any value matcher."""
    pytest.importorskip("pyld")
    pytest.importorskip("pyshacl")
    graph = shapes()
    graph.remove((EX.Field, SH.datatype, None))
    graph.remove((EX.Field, SH.maxCount, None))
    source = prepare_rdf(graph)
    imported = import_shape_contracts(
        source, [EX.Shape], [ShapeFieldBinding(EX.Field, "n")], model="R"
    )
    context = {"n": str(EX.value), "alias": str(EX.value)}
    base: dict[str, object] = {"@id": str(EX.record), "@type": str(EX.Record)}
    cases: tuple[tuple[dict[str, object], tuple[str | int, ...]], ...] = (
        ({**base, "n": {"@list": [{"@id": str(EX.child)}]}}, ()),
        ({**base, "alias": {"@list": []}}, ()),
        (
            {
                "@graph": [
                    base,
                    {
                        "@id": str(EX.record),
                        "alias": {"@list": [{"@id": str(EX.child)}]},
                    },
                ]
            },
            ("@graph", 0),
        ),
        ({**base, "n": {"@id": str(RDF.nil)}}, ()),
    )
    for output, path in cases:
        checked = check_imported_output(
            output,
            imported,
            [ModelUsage("R", path)],
            vocabulary=vocabulary(),
            context=context,
            types=(str(EX.Record),),
        )
        assert checked.report.outcome == "unverified", checked.report.to_dict()
        assert not any(issue.kind == "finding" for issue in checked.report.issues)
        converted = prepare_jsonld({**output, "@context": context})
        assert converted.source is not None
        assert (
            validate_shapes(converted.source, source, preflight=True).conforms is True
        )
        native = check_imported_rdf(
            converted.source,
            imported,
            [RdfModelUsage("R", EX.record)],
            graph_name="@default",
        )
        assert native.outcome == "unverified", native.to_dict()
        assert any(
            issue.code == "adapter.shape_list_unverified" for issue in native.issues
        )
    output = {
        "@graph": [{**base, "n": 1}, {"@id": str(EX.unrelated), "alias": {"@list": []}}]
    }
    assert check_imported_output(
        output,
        imported,
        [ModelUsage("R", ("@graph", 0))],
        vocabulary=vocabulary(),
        context=context,
        types=(str(EX.Record),),
    ).report.verified
    dataset = Dataset()
    dataset.graph(EX.G).add((EX.record, EX.value, EX.child))
    dataset.graph(EX.H).add((EX.record, EX.value, RDF.nil))
    pinned = PreparedSource(
        dataset,
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
        graph_name=str(EX.G),
    )
    assert check_imported_rdf(
        pinned, imported, [RdfModelUsage("R", EX.record)]
    ).verified
