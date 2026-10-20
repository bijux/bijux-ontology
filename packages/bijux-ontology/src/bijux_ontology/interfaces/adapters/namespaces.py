"""Finite namespace declarations with caller-selected vocabulary ownership."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from inspect import get_annotations

from rdflib import Graph
from rdflib.namespace import DefinedNamespace

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary


@dataclass(frozen=True)
class NamespaceInventory:
    """Named declaration snapshots; exclusions are exact IRIs, never prefixes."""

    declarations: tuple[TermDeclaration, ...]
    exclusions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        declarations = tuple(self.declarations)
        by_name: dict[str, TermDeclaration] = {}
        for term in declarations:
            if not isinstance(term, TermDeclaration) or not term.location:
                raise InputError("Namespace declarations require qualified names")
            if term.location in by_name and by_name[term.location] != term:
                raise InputError(
                    "Conflicting namespace declaration", location=term.location
                )
            by_name[term.location] = term
        exclusions = tuple(absolute_iri(iri) for iri in self.exclusions)
        object.__setattr__(self, "declarations", tuple(by_name.values()))
        object.__setattr__(self, "exclusions", exclusions)

    def check(
        self,
        vocabulary: Vocabulary | Graph,
        *,
        owned_namespaces: Sequence[str] = (),
        require_coverage: bool = False,
        coverage_terms: Sequence[str] = (),
        coverage_kinds: Sequence[str] = (),
        limits: Limits | None = None,
    ) -> Report:
        """Check forward membership and optionally selected local reverse coverage.

        Ownership is always supplied by the caller. Owned namespace membership
        must be local, so descriptive lookup cannot conceal a local omission.
        Reverse coverage requires an explicit selected scope and never assumes
        every ontology declaration belongs in the Python inventory.
        """
        return check_vocabulary(
            vocabulary,
            self.declarations,
            local_namespaces=owned_namespaces,
            exclude=self.exclusions,
            require_coverage=require_coverage,
            coverage_terms=coverage_terms,
            coverage_kinds=coverage_kinds,
            limits=limits,
        )


def namespace_inventory(
    namespaces: Mapping[str, type[DefinedNamespace]],
    *,
    registries: Mapping[str, Mapping[str, str | TermDeclaration]] | None = None,
    kinds: Mapping[str, tuple[str, ...]] | None = None,
    exclude: Sequence[str] = (),
) -> NamespaceInventory:
    """Extract explicitly supplied namespace classes and named term registries.

    Keys qualify declaration names, for example ``{"app.BIA": BIA}`` yields
    ``app.BIA.Dataset``. Inherited annotations and finite RDFLib ``_extras`` are
    included. Dynamic names and arbitrary application modules are not inspected.
    Registry values retain their exact IRIs and accepted kinds; ``kinds`` adds
    explicit kind contracts by qualified name. No kinds are inferred from names.
    """
    if not isinstance(namespaces, Mapping) or (
        registries is not None and not isinstance(registries, Mapping)
    ):
        raise InputError("Namespace and term registries must be named mappings")
    if kinds is not None and not isinstance(kinds, Mapping):
        raise InputError("Accepted kinds must be a named mapping")
    declarations: dict[str, TermDeclaration] = {}

    def _add(label: str, name: str, value: str | TermDeclaration) -> None:
        if not isinstance(label, str) or not label.strip():
            raise InputError("Namespace registries require nonempty names")
        if not isinstance(name, str) or not name:
            raise InputError("Namespace terms require nonempty names", location=label)
        location = f"{label}.{name}"
        if isinstance(value, TermDeclaration):
            iri, accepted = value.iri, value.kinds
        else:
            iri, accepted = value, ()
        selected = (kinds or {}).get(location, accepted)
        if accepted and selected != accepted:
            raise InputError("Conflicting accepted kinds", location=location)
        declaration = TermDeclaration(iri, selected, location)
        if location in declarations and declarations[location] != declaration:
            raise InputError("Conflicting namespace declaration", location=location)
        declarations[location] = declaration

    for label, cls in namespaces.items():
        if (
            not isinstance(cls, type)
            or not issubclass(cls, DefinedNamespace)
            or cls is DefinedNamespace
        ):
            raise InputError("Expected an RDFLib DefinedNamespace class")
        names: dict[str, None] = {}
        for base in reversed(cls.__mro__):
            if base is DefinedNamespace or not issubclass(base, DefinedNamespace):
                continue
            for name in get_annotations(base, eval_str=False):
                if name not in {
                    "_NS",
                    "_warn",
                    "_fail",
                    "_extras",
                    "_underscore_num",
                } and not name.startswith("__"):
                    names[name] = None
            for name in vars(base).get("_extras", ()):
                names[name] = None
        for name in names:
            try:
                value = str(cls[name])
            except (AttributeError, KeyError, TypeError) as error:
                raise InputError(
                    "Invalid namespace declaration", location=label
                ) from error
            _add(label, name, value)
    for label, registry in (registries or {}).items():
        if not isinstance(registry, Mapping):
            raise InputError("Term registries require named mappings")
        for name, term in registry.items():
            _add(label, name, term)
    unknown = set(kinds or ()) - declarations.keys()
    if unknown:
        raise InputError(
            "Accepted kinds name unknown declarations", location=sorted(unknown)[0]
        )
    return NamespaceInventory(tuple(declarations.values()), tuple(exclude))
