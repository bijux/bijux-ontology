"""Explicit RDF meaning for named JSON Schema object contracts."""

from dataclasses import dataclass

from bijux_ontology.declarations.types import ModelDeclaration, OutputRequirement
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri


@dataclass(frozen=True)
class SchemaModel:
    """Map one schema object to explicit RDF classes and serialized properties.

    Schema URIs identify supplied schema resources or their JSON Pointer/anchor
    fragments. They are not RDF class guesses. Output requirements are authored
    separately: JSON Schema ``required`` never guarantees an RDF occurrence.
    """

    schema_uri: str
    identifier: str
    types: tuple[str, ...]
    fields: tuple[tuple[str, RdfField], ...] = ()
    requirements: tuple[tuple[str, OutputRequirement], ...] = ()

    def __post_init__(self) -> None:
        """Own finite typed mappings and reject ambiguous property definitions."""
        absolute_iri(self.schema_uri)
        if (
            not isinstance(self.types, (tuple, list))
            or len(self.types) > Limits().max_states
        ):
            raise InputError("Schema RDF classes require a finite sequence")
        model = ModelDeclaration(self.identifier, self.types)
        object.__setattr__(self, "types", model.types)
        for name, expected in (
            ("fields", RdfField),
            ("requirements", OutputRequirement),
        ):
            supplied = getattr(self, name)
            if not isinstance(supplied, (tuple, list)):
                raise InputError("Schema mappings require finite property sequences")
            count = len(supplied)
            if count > Limits().max_states:
                raise InputError("Schema mapping exceeds the finite property bound")
            captured = []
            seen = set()
            for index in range(count):
                pair = supplied[index]
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise InputError("Expected a property and its explicit contract")
                key, contract = pair
                if (
                    not isinstance(key, str)
                    or not key
                    or not isinstance(contract, expected)
                ):
                    raise InputError("Expected a named typed property contract")
                if key in seen:
                    raise InputError("Duplicate schema property mapping")
                seen.add(key)
                captured.append((key, contract))
            object.__setattr__(self, name, tuple(captured))
        if set(dict(self.requirements)) - set(dict(self.fields)):
            raise InputError("Output requirements need an explicitly mapped property")
