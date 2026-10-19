"""Shared immutable native source capture and exact interpretation receipts."""

from collections.abc import Mapping

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text

from ..types import OwlDocument
from .native import prepare_owl_document


def document_receipt(source: OwlDocument) -> tuple[object, ...]:
    """Bind checked bytes, parser selection and both resolution identities."""
    return (
        source.content,
        source.format,
        source.document_iri,
        source.effective_url,
        source.sha256,
    )


def capture_document(source: OwlDocument, budget: Budget) -> OwlDocument:
    """Capture one ordinary descriptor without trusting subclass field callbacks."""
    if type(source) is not OwlDocument:
        raise InputError("Axiom transactions require an ordinary prepared OwlDocument")
    content, syntax, identity, effective, digest = (
        source.content,
        source.format,
        source.document_iri,
        source.effective_url,
        source.sha256,
    )
    if type(content) is not bytes or type(digest) is not str:
        raise InputError("Invalid native source record")
    result = prepare_owl_document(
        content,
        format=syntax,
        document_iri=identity,
        effective_url=effective,
        budget=budget,
    )
    bound_text(digest, budget)
    if result.sha256 != digest:
        raise InputError("Prepared source bytes differ from their captured hash")
    return result


def capture_imports(
    values: Mapping[str, OwlDocument] | None, budget: Budget
) -> Mapping[str, OwlDocument] | None:
    """Capture supplied import bytes before trusted application callbacks run."""
    if values is None:
        return None
    if not isinstance(values, Mapping):
        raise InputError("Native imports need an explicit finite mapping")
    size = len(values)
    budget.consume("resources", size)
    budget.consume("states", size)
    budget.consume("bytes", 64 + size * 128)
    result: dict[str, OwlDocument] = {}
    for alias, source in values.items():
        if len(result) >= size or type(alias) is not str:
            raise InputError("Import mapping changed or has a nontext alias")
        bound_text(alias, budget)
        result[alias] = capture_document(source, budget)
    if len(result) != size:
        raise InputError("Import mapping changed during capture")
    return result
