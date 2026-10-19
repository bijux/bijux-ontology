"""Bounded original-module capture and explicit theory assembly."""

from collections.abc import Sequence
from xml.etree.ElementTree import Element, SubElement, tostring

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import bound_text

from ..documents.sources.capture import (
    capture_document,
    capture_imports,
    document_receipt,
)
from ..documents.sources.native import prepare_owl_document
from ..documents.types import OwlDocument
from .types import TheoryCombination, TheoryComponent


def reserve(operation: Operation, count: int = 1) -> None:
    """Pay owned references and terminal handoff storage before a native stage."""
    operation.consume("work", count)
    operation.consume("states", 8 * count)
    operation.consume("bytes", 512 * count)


def _label(value: str, operation: Operation) -> str:
    """Validate bounded immutable labels without allocating stripped copies."""
    if type(value) is not str:
        raise InputError("Theory labels require ordinary strings")
    bound_text(value, operation)
    if not value or value.isspace():
        raise InputError("Theory labels must be nonempty")
    return value


def derived_label(value: str, suffix: str, operation: Operation) -> str:
    """Reserve derived deployment metadata before copying a qualified label."""
    size = len(value) + len(suffix)
    operation.consume("work", size + 1)
    operation.consume("bytes", 4 * size)
    return value + suffix


def capture_theories(
    components: Sequence[TheoryComponent],
    combinations: Sequence[TheoryCombination],
    operation: Operation,
) -> tuple[dict[str, TheoryComponent], tuple[TheoryCombination, ...]]:
    """Capture referenced sources before calls; leave unselected data unparsed."""
    selected = []
    required: set[str] = set()
    labels: set[str] = set()
    for value in capture_items(combinations, TheoryCombination, operation):
        if type(value) is not TheoryCombination:
            raise InputError("Choose ordinary TheoryCombination records")
        label = _label(value.label, operation)
        if label in labels:
            raise InputError("Combination labels must be distinct")
        labels.add(label)
        ordinary = capture_items(value.components, str, operation)
        bridges = capture_items(value.bridges, str, operation)
        reserve(operation, len(ordinary) + len(bridges) + 1)
        seen: set[str] = set()
        for name in (*ordinary, *bridges):
            _label(name, operation)
            if name in seen:
                raise InputError("A combination cannot repeat a component or bridge")
            seen.add(name)
            required.add(name)
        if not ordinary or len(seen) < 2:
            raise InputError("A joint theory requires at least two selected sources")
        selected.append(TheoryCombination(label, ordinary, bridges))
    if not selected:
        raise InputError("Choose a nonempty finite combination inventory")
    captured: dict[str, TheoryComponent] = {}
    available: set[str] = set()
    for component in capture_items(components, TheoryComponent, operation):
        if type(component) is not TheoryComponent:
            raise InputError("Choose ordinary TheoryComponent records")
        label = _label(component.label, operation)
        if label in available:
            raise InputError("Component labels must be distinct")
        available.add(label)
        if label in required:
            reserve(operation)
            captured[label] = TheoryComponent(
                label,
                capture_document(component.source, operation),
                capture_imports(component.imports, operation),
            )
    if required - captured.keys():
        raise InputError("A selected component or bridge is not supplied")
    return captured, tuple(selected)


def assemble_theory(
    label: str,
    components: Sequence[TheoryComponent],
    operation: Operation,
) -> TheoryComponent:
    """Create an imports-only native root with no source flattening or axiom guesses.

    Native OWLAPI supplies the actual imports-closure semantics. Alias/base
    conflicts are unsupported joint interpretations, never last-writer choices.
    The generated ontology is anonymous, so its ontology identity cannot collide
    with an original named ontology. Its resolution identity avoids all supplied
    aliases and bases. ElementTree only serializes escaped import identifiers;
    no new parser or axiom representation is introduced.
    """
    label = _label(label, operation)
    chosen = capture_items(components, TheoryComponent, operation)
    if not chosen:
        raise InputError("An assembled theory requires selected components")
    reserve(operation, len(chosen) + 2)
    operation.consume("work", len(chosen))
    maximum = 256 + sum(96 + 6 * len(item.source.effective_url) for item in chosen)
    if maximum > operation.limits.max_bytes:
        raise LimitExceeded(
            "Joint XML serialization exceeds its conservative byte bound"
        )
    aliases: dict[str, OwlDocument] = {}
    bases: dict[str, OwlDocument] = {}
    identities: set[str] = set()

    def add(alias: str, document: OwlDocument) -> None:
        """Retain qualified aliases without changing any module's interpretation."""
        bound_text(alias, operation)
        reserve(operation)
        prior = aliases.get(alias)
        operation.consume("work", len(document.content) + 1)
        if prior is not None and document_receipt(prior) != document_receipt(document):
            raise ResourceError("Selected theories require conflicting import aliases")
        prior = bases.get(document.effective_url)
        if prior is not None and document_receipt(prior) != document_receipt(document):
            raise ResourceError(
                "Selected theories conflict at one effective source URL"
            )
        aliases[alias] = document
        bases[document.effective_url] = document
        if document.document_iri not in identities:
            operation.consume("states")
            operation.consume("bytes", 128)
            identities.add(document.document_iri)

    root = Element("{http://www.w3.org/2002/07/owl#}Ontology")
    for component in chosen:
        document = component.source
        add(document.effective_url, document)
        add(document.document_iri, document)
        for alias, imported in (component.imports or {}).items():
            add(alias, imported)
        # Reserve escaped serializer storage before retaining XML text/children.
        operation.consume("work", len(document.effective_url) + 1)
        operation.consume("bytes", 1024 + 24 * len(document.effective_url))
        SubElement(
            root, "{http://www.w3.org/2002/07/owl#}Import"
        ).text = document.effective_url
    identity = "urn:bijux:ontology:joint"
    for index in range(len(aliases) + len(bases) + len(identities) + 1):
        bound_text(identity, operation)
        if (
            identity not in aliases
            and identity not in bases
            and identity not in identities
        ):
            break
        reserve(operation)
        identity = f"urn:bijux:ontology:joint:{index + 1}"
    content = tostring(root, encoding="utf-8", xml_declaration=True)
    prepared = prepare_owl_document(
        content,
        format="owlxml",
        document_iri=identity,
        budget=operation,
    )
    return TheoryComponent(label, prepared, aliases)
