"""Original selected SHACL reference occurrences and exact source witnesses."""

from collections.abc import Iterator

from rdflib import BNode, URIRef
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.parameters.reading import ParameterReader

from .types import ShapeDependency, ShapeTerm, Triple

DIRECT_REFERENCES = frozenset((SH.node, SH.property, SH["not"], SH.qualifiedValueShape))
LOGICAL_REFERENCES = frozenset((SH["and"], SH["or"], SH.xone))
REFERENCE_PARAMETERS = DIRECT_REFERENCES | LOGICAL_REFERENCES


class ReferenceReader:
    """Share reference facts without assigning execution or recursion policy."""

    def __init__(self, parameters: ParameterReader, owned_blanks: set[int]) -> None:
        self.parameters = parameters
        self.owned_blanks = owned_blanks

    def _members(self, parameter: URIRef, anchor: Node) -> Iterator[Node]:
        """Delegate logical membership to the existing bounded RDF list reader."""
        return (
            iter_rdf_list(
                self.parameters.graph,
                anchor,
                max_items=self.parameters.budget.limits.max_states,
                budget=self.parameters.budget,
            )
            if parameter in LOGICAL_REFERENCES
            else iter((anchor,))
        )

    def from_assertion(
        self, shape: ShapeTerm, parameter: URIRef, anchor: Node
    ) -> Iterator[ShapeDependency]:
        """Retain every occurrence with its connected, original list-position prefix.

        Fully validate a logical list before emitting its first occurrence. This
        extracts asserted facts, independently of a consumer's singleton-parameter
        or native execution policy.
        """
        budget = self.parameters.budget
        triple = shape, parameter, anchor
        if parameter in DIRECT_REFERENCES:
            if not isinstance(anchor, URIRef | BNode):
                raise InputError("Shape dependency requires an RDF resource")
            budget.consume("states")
            budget.consume("bytes", 128)
            yield ShapeDependency(shape, parameter, anchor, (triple,))
        elif parameter in LOGICAL_REFERENCES:
            members = tuple(self._members(parameter, anchor))
            witness: list[Triple] = [triple]
            cell = anchor
            for member in members:
                first = cell, RDF.first, member
                rest = next(self.parameters.graph.objects(cell, RDF.rest))
                witness.extend((first, (cell, RDF.rest, rest)))
                budget.consume("states", len(witness))
                budget.consume("bytes", 64 + 24 * len(witness))
                if not isinstance(member, URIRef | BNode):
                    raise InputError("Logical shape lists need resource members")
                yield ShapeDependency(shape, parameter, member, tuple(witness))
                cell = rest
        else:
            raise InputError("Unsupported shape-reference parameter")

    def _bound_metadata(self, node: Node) -> None:
        """Require actual snapshot ownership before anonymous equality lookups."""
        bound_node(node, self.parameters.budget)
        if isinstance(node, BNode) and id(node) not in self.owned_blanks:
            raise InputError(
                "Shape metadata blank terms require exact snapshot ownership"
            )

    def qualify(
        self, roots: tuple[ShapeTerm, ...], dependencies: tuple[ShapeDependency, ...]
    ) -> dict[Node, None]:
        """Qualify supplied endpoints and complete witnesses against the owned source.

        Caller-created profile records are not proof of completeness. Every term,
        source assertion, connected list prefix and required occurrence is checked
        before any downstream static or execution policy consumes these facts.
        """
        nodes: dict[Node, None] = {}
        for node in roots:
            if not isinstance(node, URIRef | BNode):
                raise InputError("Selected shape roots require resource terms")
            self._bound_metadata(node)
            self.parameters.budget.consume("states")
            nodes[node] = None
        for dependency in dependencies:
            self.parameters.budget.consume("work")
            if not isinstance(dependency, ShapeDependency):
                raise InputError(
                    "Analysis requires exact supported shape-reference metadata"
                )
            if (
                not isinstance(dependency.shape, URIRef | BNode)
                or not isinstance(dependency.dependency, URIRef | BNode)
                or not isinstance(dependency.parameter, URIRef)
                or type(dependency.via) is not tuple
                or not dependency.via
            ):
                raise InputError("Shape references require exact resource witnesses")
            for node in (
                dependency.shape,
                dependency.parameter,
                dependency.dependency,
            ):
                self._bound_metadata(node)
            if dependency.parameter not in REFERENCE_PARAMETERS:
                raise InputError(
                    "Analysis requires exact supported shape-reference metadata"
                )
            self.parameters.budget.consume("states", 2 + len(dependency.via))
            for triple in dependency.via:
                self.parameters.budget.consume("work")
                if type(triple) is not tuple or len(triple) != 3:
                    raise InputError("Shape witnesses require original RDF triples")
                for term in triple:
                    self._bound_metadata(term)
                if triple not in self.parameters.graph:
                    raise InputError("Shape reference witness is outside the profile")
            nodes[dependency.shape] = None
            nodes[dependency.dependency] = None
        self._inventory(nodes, dependencies)
        return nodes

    def _inventory(
        self, nodes: dict[Node, None], dependencies: tuple[ShapeDependency, ...]
    ) -> None:
        """Verify the supplied complete dependency plan against actual owned assertions.

        The maintained list iterator establishes logical membership. Supplied witness
        prefixes only describe positions in that already qualified list; they never
        substitute a caller-authored parameter or endpoint for an RDF assertion.
        """
        budget = self.parameters.budget
        logical = LOGICAL_REFERENCES
        expected: dict[tuple[Node, URIRef, Node, int | None], Node] = {}
        for shape in nodes:
            budget.consume("work")
            for parameter in REFERENCE_PARAMETERS:
                budget.consume("work")
                for anchor in self.parameters.values(
                    shape, parameter, single=parameter in logical
                ):
                    members = self._members(parameter, anchor)
                    for position, target in enumerate(members):
                        budget.consume("work")
                        bound_node(target, budget)
                        if (
                            not isinstance(target, URIRef | BNode)
                            or target not in nodes
                        ):
                            raise InputError(
                                "Profile reference endpoint is absent "
                                "from its dependency inventory"
                            )
                        budget.consume("states")
                        expected[
                            (
                                shape,
                                parameter,
                                target,
                                position if parameter in logical else None,
                            )
                        ] = anchor
        seen = set()
        for dependency in dependencies:
            budget.consume("work")
            shape, parameter, target = (
                dependency.shape,
                dependency.parameter,
                dependency.dependency,
            )
            via = dependency.via
            witness_position = (len(via) - 3) // 2 if parameter in logical else None
            key = shape, parameter, target, witness_position
            if key not in expected or key in seen:
                raise InputError(
                    "Profile dependency metadata differs "
                    "from the actual reference inventory"
                )
            anchor = expected[key]
            if via[0] != (shape, parameter, anchor):
                raise InputError(
                    "Profile dependency anchor does not match its source and parameter"
                )
            if parameter in logical:
                if len(via) < 3 or len(via) % 2 != 1:
                    raise InputError(
                        "Logical dependencies require their exact list-position witness"
                    )
                cell = anchor
                for index in range(1, len(via), 2):
                    budget.consume("work")
                    first, rest = via[index], via[index + 1]
                    if first[:2] != (cell, RDF.first) or rest[:2] != (cell, RDF.rest):
                        raise InputError(
                            "Logical dependency witness is not a connected list prefix"
                        )
                    cell = rest[2]
                if via[-2][2] != target:
                    raise InputError(
                        "Logical dependency witness names a different endpoint"
                    )
            elif len(via) != 1:
                raise InputError(
                    "Direct dependencies require their exact source assertion"
                )
            budget.consume("states")
            seen.add(key)
        if len(seen) != len(expected):
            raise InputError(
                "Profile dependency inventory omits actual outgoing references"
            )
