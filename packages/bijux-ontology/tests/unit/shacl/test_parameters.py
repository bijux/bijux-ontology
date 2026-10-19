"""Exact RDF scalar admission cannot collapse ambiguity or exhaustion into truth."""

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SH, XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.shacl.parameters.reading import (
    ParameterReader,
    boolean_value,
    count_value,
)


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("+01", XSD.integer, 1),
        ("0", XSD.integer, 0),
        ("-1", XSD.integer, None),
        ("1.0", XSD.integer, None),
        ("1", XSD.boolean, None),
    ],
)
def test_count_admission_preserves_exact_rdf_type(
    text: str, kind: URIRef, expected: int | None
) -> None:
    value = Literal(text, datatype=kind, normalize=False)
    if expected is None:
        with pytest.raises(InputError):
            count_value(value, Budget())
    else:
        assert count_value(value, Budget()) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("true", True), ("1", True), ("false", False), ("0", False), ("TRUE", None)],
)
def test_boolean_admission_never_uses_python_truthiness(
    text: str, expected: bool | None
) -> None:
    value = Literal(text, datatype=XSD.boolean, normalize=False)
    if expected is None:
        with pytest.raises(InputError):
            boolean_value(value)
    else:
        assert boolean_value(value) is expected


def test_parameter_ownership_ambiguity_and_integer_exhaustion_are_distinct() -> None:
    graph, shape = Graph(), URIRef("urn:shape")
    value = Literal("01", datatype=XSD.integer, normalize=False)
    graph.add((shape, SH.minCount, value))
    reader = ParameterReader(graph, Budget())
    assert reader.one(shape, SH.minCount) is value
    graph.add((shape, SH.minCount, Literal("1", datatype=XSD.integer, normalize=False)))
    with pytest.raises(InputError, match="multiple values"):
        reader.count(shape, SH.minCount)
    with pytest.raises(LimitExceeded, match="representation"):
        count_value(
            Literal("1" * 4097, datatype=XSD.integer, normalize=False), Budget()
        )
    with pytest.raises(LimitExceeded):
        count_value(value, Budget(Limits(max_work=1)))
