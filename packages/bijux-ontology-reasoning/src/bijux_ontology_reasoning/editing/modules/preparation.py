"""Capture exact module originals and finite owned structural edit instructions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import bound_text
from bijux_ontology.workflows.ontology.modules.types import ModuleBinding

from ...axioms.changes.preconditions import qualify_base, source_base
from ...axioms.changes.types import AxiomBase
from ...axioms.codec.encoding import ConstructionEncoder
from ...axioms.types import ProjectedAxiom
from ...documents.sources.capture import (
    capture_document,
    capture_imports,
    document_receipt,
)
from ...documents.sources.native import native_iri
from ...documents.types import OwlDocument
from ...inference.entailment.encoding import encode_axiom
from ...theories.sources import assemble_theory, reserve
from ...theories.types import TheoryComponent
from .types import (
    AxiomMove,
    ModuleConsumer,
    ModuleImportChange,
    ModuleInline,
    NamespaceTransfer,
)


@dataclass(frozen=True)
class CapturedModules:
    """One exact source-set base and original offline alias inventory."""

    originals: tuple[OwlDocument, ...]
    assembly: TheoryComponent
    owned: frozenset[str]
    bases_match: bool
    labels: tuple[tuple[str, str], ...]


def capture_modules(
    components: Sequence[TheoryComponent],
    expected: Mapping[str, AxiomBase],
    owned: Sequence[str],
    operation: Operation,
) -> CapturedModules:
    """Capture each supplied source once and assemble all selected originals."""
    originals: dict[str, OwlDocument] = {}
    aliases: dict[str, OwlDocument] = {}
    labels: set[str] = set()
    labeled: list[tuple[str, str]] = []
    roots: set[str] = set()

    def add(document: OwlDocument, alias: str | None = None) -> OwlDocument:
        """Preserve exact source context and reject first-winner alias conflicts."""
        source = capture_document(document, operation)
        reserve(operation, 3)
        prior = originals.get(source.effective_url)
        if prior is not None and document_receipt(prior) != document_receipt(source):
            raise ResourceError("Module originals conflict at one effective identity")
        originals[source.effective_url] = source
        for key in (source.document_iri, source.effective_url, alias):
            if key is None:
                continue
            native_iri(key, operation)
            prior = aliases.get(key)
            operation.consume("work", len(source.content) + 1)
            if prior is not None and document_receipt(prior) != document_receipt(
                source
            ):
                raise ResourceError("Module originals require conflicting aliases")
            reserve(operation)
            aliases[key] = source
        return source

    selected = capture_items(components, TheoryComponent, operation)
    if len(selected) < 2:
        raise InputError("A module transaction requires at least two original modules")
    for component in selected:
        if type(component) is not TheoryComponent:
            raise InputError("Choose ordinary labeled TheoryComponent records")
        label, document, imported = component.label, component.source, component.imports
        if type(label) is not str:
            raise InputError("Module component labels require plain text")
        bound_text(label, operation)
        if not label or label in labels:
            raise InputError("Module component labels must be distinct and nonempty")
        labels.add(label)
        source = add(document)
        identity = source.effective_url
        if identity in roots:
            raise InputError("Explicit module components must have distinct originals")
        reserve(operation)
        roots.add(identity)
        labeled.append((label, identity))
        for alias, child in (capture_imports(imported, operation) or {}).items():
            add(child, alias)
    chosen = capture_items(owned, str, operation)
    reserve(operation, len(chosen) + len(originals))
    owners = frozenset(native_iri(value, operation) for value in chosen)
    if not owners or len(owners) != len(chosen) or not owners <= originals.keys():
        raise InputError("Select distinct owned modules from the exact original set")
    if not isinstance(expected, Mapping):
        raise InputError("Exact source-set bases require an explicit mapping")
    reserve(operation, len(expected))
    bases: dict[str, AxiomBase] = {}
    count = len(expected)
    for key, value in expected.items():
        if len(bases) >= count:
            raise InputError("Module source-set bases changed during capture")
        if type(value) is not AxiomBase:
            raise InputError("Module bases require ordinary exact AxiomBase records")
        captured_base = AxiomBase(
            value.sha256, value.format, value.document_iri, value.effective_url
        )
        bases[native_iri(key, operation)] = qualify_base(captured_base, operation)
    if len(bases) != count:
        raise InputError("Module source-set bases changed during capture")
    matched = set(bases) == set(originals) and all(
        bases[identity] == source_base(document)
        for identity, document in originals.items()
    )
    # Every supplied original is selected, including explicitly read-only modules.
    components_owned = tuple(
        TheoryComponent(
            document.effective_url, document, aliases if index == 0 else None
        )
        for index, document in enumerate(originals.values())
    )
    assembled = assemble_theory("module-set", components_owned, operation)
    return CapturedModules(
        tuple(originals.values()), assembled, owners, matched, tuple(labeled)
    )


def _bindings(
    values: tuple[ModuleBinding, ...], operation: Operation
) -> tuple[ModuleBinding, ...]:
    """Copy only exact bounded provider names, with no implicit provider choice."""
    selected = capture_items(values, ModuleBinding, operation)
    reserve(operation, len(selected))
    result = []
    for value in selected:
        if type(value) is not ModuleBinding:
            raise InputError("Module consumers require ordinary provider bindings")
        requirement, provider = value.requirement, value.provider
        for text in (requirement, provider):
            if type(text) is not str:
                raise InputError("Provider binding names require plain text")
            bound_text(text, operation)
            if not text:
                raise InputError("Provider binding names must be nonempty")
        result.append(ModuleBinding(requirement, provider))
    return tuple(result)


def capture_consumers(
    values: Sequence[ModuleConsumer], identities: set[str], operation: Operation
) -> tuple[ModuleConsumer, ...]:
    """Require finite unchanged obligations in addition to explicit provider wiring."""
    selected = capture_items(values, ModuleConsumer, operation)
    if not selected:
        raise InputError("A checked module transaction requires selected consumers")
    reserve(operation, len(selected))
    result = []
    seen = set()
    for value in selected:
        if type(value) is not ModuleConsumer:
            raise InputError("Choose ordinary ModuleConsumer records")
        identity = native_iri(value.document, operation)
        if identity not in identities or identity in seen:
            raise InputError("Consumers must name distinct original modules")
        seen.add(identity)
        axioms, _ = ConstructionEncoder(operation).axioms(value.entailments)
        if not axioms:
            raise InputError("Every selected consumer requires native entailments")
        for axiom in axioms:
            encode_axiom(axiom, operation)
        result.append(
            ModuleConsumer(
                identity,
                _bindings(value.before_bindings, operation),
                _bindings(value.after_bindings, operation),
                axioms,
            )
        )
    return tuple(result)


def encode_changes(
    changes: Sequence[AxiomMove | ModuleInline],
    imports: Sequence[ModuleImportChange],
    protected: Mapping[str, Sequence[ProjectedAxiom]],
    indices: dict[str, int],
    owned: frozenset[str],
    operation: Operation,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    set[tuple[str, str]],
]:
    """Own annotated axioms and explicit import deltas before native entry."""
    selected = capture_items(changes, object, operation)
    if not selected:
        raise InputError("Choose explicit axiom moves or module inlining")
    reserve(operation, len(selected) * 4)
    wire: list[dict[str, object]] = []
    pairs: set[tuple[str, str]] = set()
    for value in selected:
        if not isinstance(value, AxiomMove | ModuleInline) or type(value) not in (
            AxiomMove,
            ModuleInline,
        ):
            raise InputError("Choose ordinary typed move or inline records")
        source, target = (
            native_iri(value.source, operation),
            native_iri(value.target, operation),
        )
        if source == target or source not in owned or target not in owned:
            raise InputError("Move endpoints must be distinct explicitly owned modules")
        pairs.add((source, target))
        axioms: list[dict[str, object]] = []
        inline = type(value) is ModuleInline
        if isinstance(value, AxiomMove):
            captured, axioms = ConstructionEncoder(operation).axioms(value.axioms)
            if not captured:
                raise InputError("An explicit axiom move must select actual structures")
        wire.append(
            {
                "source": indices[source],
                "target": indices[target],
                "inline": inline,
                "axioms": axioms,
            }
        )
    imported: list[dict[str, object]] = []
    for import_change in capture_items(imports, ModuleImportChange, operation):
        if type(import_change) is not ModuleImportChange:
            raise InputError("Import changes require ordinary typed records")
        identity = native_iri(import_change.document, operation)
        if identity not in owned:
            raise InputError("Import changes require an explicitly owned module")
        additions = [
            native_iri(text, operation)
            for text in capture_items(import_change.additions, str, operation)
        ]
        removals = [
            native_iri(text, operation)
            for text in capture_items(import_change.removals, str, operation)
        ]
        reserve(operation, len(additions) + len(removals) + 1)
        imported.append(
            {
                "document": indices[identity],
                "additions": additions,
                "removals": removals,
            }
        )
    if not isinstance(protected, Mapping):
        raise InputError("Protected axioms require an explicit source mapping")
    guarded: list[dict[str, object]] = []
    reserve(operation, len(protected))
    size = len(protected)
    for identity, values in protected.items():
        if len(guarded) >= size:
            raise InputError("Protected axiom inventory changed during capture")
        identity = native_iri(identity, operation)
        if identity not in indices:
            raise InputError("A protected axiom owner is outside the source set")
        captured, encoded = ConstructionEncoder(operation).axioms(values)
        if not captured:
            raise InputError("Protected axiom groups cannot be empty")
        guarded.append({"document": indices[identity], "axioms": encoded})
    if len(guarded) != size:
        raise InputError("Protected axiom inventory changed during capture")
    return wire, imported, guarded, pairs


def capture_namespaces(
    values: Sequence[NamespaceTransfer],
    pairs: set[tuple[str, str]],
    operation: Operation,
) -> tuple[NamespaceTransfer, ...]:
    """Keep explicit publication policy distinct from physical axiom occurrence."""
    result = []
    seen = set()
    for value in capture_items(values, NamespaceTransfer, operation):
        if type(value) is not NamespaceTransfer:
            raise InputError("Namespace transfers require ordinary explicit records")
        namespace, source, target = (
            native_iri(value.namespace, operation),
            native_iri(value.source, operation),
            native_iri(value.target, operation),
        )
        reserve(operation)
        if namespace in seen or (source, target) not in pairs:
            raise InputError(
                "Namespace policy must select distinct scopes and actual moves"
            )
        seen.add(namespace)
        result.append(NamespaceTransfer(namespace, source, target))
    return tuple(result)
