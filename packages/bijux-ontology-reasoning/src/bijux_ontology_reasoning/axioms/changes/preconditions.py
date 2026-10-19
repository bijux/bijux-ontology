"""Exact source identity preconditions for checked native axiom changes."""

from bijux_ontology.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, Limits, bound_text

from ...documents.sources.capture import capture_document
from ...documents.types import OwlDocument
from .types import AxiomBase


def source_base(source: OwlDocument) -> AxiomBase:
    """Retain already-qualified immutable scalar preconditions."""
    return AxiomBase(
        source.sha256, source.format, source.document_iri, source.effective_url
    )


def axiom_base(
    source: OwlDocument, *, limits: Limits | None = None, budget: Budget | None = None
) -> AxiomBase:
    """Capture raw integrity together with syntax and exact resolution identities."""
    operation = Operation(limits, budget, 30, None)
    return source_base(capture_document(source, operation))


def qualify_base(expected: AxiomBase, budget: Budget) -> AxiomBase:
    """Prepay required scalars before comparing interpretation context."""
    if type(expected) is not AxiomBase:
        raise InputError("Expected a complete typed native axiom base")
    for value in (
        expected.sha256,
        expected.format,
        expected.document_iri,
        expected.effective_url,
    ):
        if type(value) is not str:
            raise InputError("Native axiom base fields require plain text")
        bound_text(value, budget)
    return expected
