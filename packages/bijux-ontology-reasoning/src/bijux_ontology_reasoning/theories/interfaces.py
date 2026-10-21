"""Owned finite module interfaces shared by checked ontology transactions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget
from bijux_ontology.owl.modules import qualification
from bijux_ontology.owl.modules.contracts import (
    ModuleContract,
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
            original[identity] = qualification.capture_contract(value, budget)
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
            qualification.capture_contract(change.before, budget),
            qualification.capture_contract(change.after, budget),
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
