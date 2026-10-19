"""Owned finite module interfaces shared by checked ontology transactions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.owl.obligations import OntologyObligation
from bijux_ontology.workflows.ontology.modules.checking import _exports, _requirement
from bijux_ontology.workflows.ontology.modules.types import (
    ModuleContract,
    ModulePropertyRequirement,
    ModuleRequirement,
)

from ..documents.sources.native import native_iri
from ..documents.types import OwlDocument


@dataclass(frozen=True)
class ModuleInterfaceChange:
    """Caller-authored interface replacement for one selected effective document."""

    document: str
    before: ModuleContract
    after: ModuleContract


@dataclass(frozen=True)
class ModuleSet:
    """Finite document and interface inventory supplied to trusted after checks."""

    documents: tuple[OwlDocument, ...]
    interfaces: tuple[tuple[str, ModuleContract], ...]


def capture_contract(value: ModuleContract, budget: Budget) -> ModuleContract:
    """Reuse finite module contract qualification and own terminal records."""
    if type(value) is not ModuleContract or type(value.name) is not str:
        raise InputError("Interfaces need ordinary explicit module contracts")
    bound_text(value.name, budget)
    if not value.name.strip():
        raise InputError("Module interface names must be nonempty")
    exports = _exports(value.exports, budget)
    requirements = capture_items(value.requires, ModuleRequirement, budget)
    budget.consume("bytes", 128 + 128 * (len(exports) + len(requirements)))
    owned_exports = []
    for term in exports:
        if type(term) is not TermDeclaration:
            raise InputError("Public exports require ordinary term declarations")
        owned_exports.append(
            TermDeclaration(term.iri, tuple(term.kinds), term.location)
        )
    owned_requirements = []
    for requirement in requirements:
        if type(requirement) is not ModuleRequirement:
            raise InputError("Module requirements need ordinary records")
        qualified = _requirement(requirement, budget)
        budget.consume(
            "states",
            len(qualified.exports)
            + len(qualified.obligations)
            + len(qualified.properties),
        )
        budget.consume(
            "bytes",
            128
            + 256
            * (
                len(qualified.exports)
                + len(qualified.obligations)
                + len(qualified.properties)
            ),
        )
        if (
            any(type(term) is not TermDeclaration for term in qualified.exports)
            or any(
                type(question) is not OntologyObligation
                for question in qualified.obligations
            )
            or any(
                type(question) is not ModulePropertyRequirement
                for question in qualified.properties
            )
        ):
            raise InputError("Module interfaces need ordinary nested contract records")
        for question in qualified.obligations:
            bound_text(question.kind, budget)
            bound_text(question.regime, budget)
        # Existing qualification pays every actual operand and selected relation.
        # Reconstructed frozen records exclude references to caller-owned arrays.
        owned_requirements.append(
            replace(
                qualified,
                exports=tuple(
                    TermDeclaration(t.iri, tuple(t.kinds), t.location)
                    for t in qualified.exports
                ),
                obligations=tuple(
                    replace(q, actual=tuple(q.actual)) for q in qualified.obligations
                ),
                properties=tuple(replace(q) for q in qualified.properties),
            )
        )
    return ModuleContract(value.name, tuple(owned_exports), tuple(owned_requirements))


def capture_interfaces(
    originals: Mapping[str, ModuleContract] | None,
    changes: Sequence[ModuleInterfaceChange],
    documents: set[str],
    owned: set[str],
    budget: Budget,
) -> tuple[
    tuple[tuple[str, ModuleContract], ...], tuple[tuple[str, ModuleContract], ...]
]:
    """Replace only explicitly selected interfaces, without rewriting symbols."""
    original: dict[str, ModuleContract] = {}
    if originals is not None:
        if not isinstance(originals, Mapping):
            raise InputError("Interfaces need an explicit document mapping")
        size = len(originals)
        budget.consume("bytes", 128 + size * 192)
        budget.consume("states", size)
        for key, value in originals.items():
            if len(original) >= size:
                raise InputError("Interface mapping changed during capture")
            identity = native_iri(key, budget)
            if identity not in documents:
                raise InputError("An interface names an unsupplied document")
            original[identity] = capture_contract(value, budget)
        if len(original) != size:
            raise InputError("Interface mapping changed during capture")
    budget.consume("bytes", 128 + len(original) * 128)
    after = dict(original)
    selected = capture_items(changes, ModuleInterfaceChange, budget)
    budget.consume("states", len(selected))
    seen: set[str] = set()
    for change in selected:
        if type(change) is not ModuleInterfaceChange:
            raise InputError("Use ordinary explicit interface replacements")
        identity = native_iri(change.document, budget)
        before, updated = (
            capture_contract(change.before, budget),
            capture_contract(change.after, budget),
        )
        if identity in seen or identity not in owned or identity not in original:
            raise InputError(
                "Interface replacement needs one selected owned existing interface"
            )
        if before != original[identity]:
            raise InputError(
                "Interface replacement differs from its required before contract"
            )
        seen.add(identity)
        after[identity] = updated
    budget.consume("bytes", 128 + len(original) * 64)
    return tuple(original.items()), tuple(after.items())
