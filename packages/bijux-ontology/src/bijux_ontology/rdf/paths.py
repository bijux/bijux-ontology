"""Owned finite RDF property paths captured from maintained path objects."""

from collections.abc import Sequence
from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.paths import AlternativePath, InvPath, MulPath, Path, SequencePath
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri


@dataclass(frozen=True)
class RdfPath:
    """A finite path expression; the original RDF node is always retained."""

    kind: str
    node: Node
    children: tuple["RdfPath", ...] = ()
    limitation: str | None = None

    @property
    def readable(self) -> str:
        """Render established path syntax without interpreting unsupported terms."""
        if self.kind == "predicate":
            return f"<{self.node}>"
        parts = tuple(child.readable for child in self.children)
        if self.kind in ("sequence", "alternative"):
            return "(" + (" / " if self.kind == "sequence" else " | ").join(parts) + ")"
        if parts:
            if self.kind == "inverse":
                return "^" + parts[0]
            suffix = {"zero_or_more": "*", "one_or_more": "+", "zero_or_one": "?"}
            return "(" + parts[0] + ")" + suffix[self.kind]
        return self.node.n3()

    def to_dict(self) -> dict[str, object]:
        """Export typed syntax and retained limitations beside a readable path."""
        return {
            "kind": self.kind,
            "node": self.node.n3(),
            "readable": self.readable,
            "children": [child.to_dict() for child in self.children],
            "limitation": self.limitation,
        }


def capture_path(
    path: URIRef | Path | RdfPath,
    *,
    budget: Budget | None = None,
    limits: Limits | None = None,
) -> RdfPath:
    """Snapshot a finite maintained path without parsing a string expression.

    Predicate spelling and existing record source nodes are retained. Maintained
    compound path objects receive fresh owned blank source tokens. Unsupported
    path objects, incomplete records and cycles are rejected before returning.
    """
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Path limits must match the supplied operation budget")
    operation = budget if budget is not None else Budget(limits)

    def text(value: str) -> str:
        """Bound retained metadata before validation or copying."""
        if not isinstance(value, str):
            raise InputError("Path metadata must be text")
        operation.consume("work", len(value) + 1)
        if len(value) > operation.limits.max_bytes:
            raise LimitExceeded("Path metadata exceeds max_bytes")
        try:
            encoded = value.encode("utf-8")
        except UnicodeError as error:
            raise InputError("Path metadata must be valid Unicode") from error
        if len(encoded) > operation.limits.max_bytes:
            raise LimitExceeded("Path metadata exceeds max_bytes")
        operation.consume("bytes", len(encoded))
        return value

    def walk(value: object, active: frozenset[int], depth: int) -> RdfPath:
        """Own one operator and its bounded arguments with cycle detection."""
        operation.consume("work")
        operation.consume("states")
        if depth > min(operation.limits.max_depth, 128):
            raise LimitExceeded("Path exceeds bounded capture depth")
        if isinstance(value, URIRef):
            iri = absolute_iri(text(value))
            return RdfPath("predicate", URIRef(iri))
        if id(value) in active:
            raise InputError("A property path must not contain a cycle")
        source: BNode
        arguments: Sequence[object]
        if isinstance(value, RdfPath):
            kind = text(value.kind)
            if value.limitation is not None:
                text(value.limitation)
                raise InputError("An incomplete property path cannot be captured")
            if type(value.children) is not tuple:
                raise InputError("Path children must be an immutable tuple")
            if kind == "predicate":
                if value.children or not isinstance(value.node, URIRef):
                    raise InputError("Predicate path requires one IRI and no children")
                return walk(value.node, active | {id(value)}, depth + 1)
            if not isinstance(value.node, BNode):
                raise InputError("Compound path source tokens must be blank nodes")
            source = BNode(text(value.node))
            arguments = value.children
        else:
            source = BNode()
            if type(value) is SequencePath:
                kind, arguments = "sequence", value.args
            elif type(value) is AlternativePath:
                kind, arguments = "alternative", value.args
            elif type(value) is InvPath:
                kind, arguments = "inverse", (value.arg,)
            elif type(value) is MulPath:
                modifier = text(value.mod)
                modifiers = {
                    "*": "zero_or_more",
                    "+": "one_or_more",
                    "?": "zero_or_one",
                }
                if modifier not in modifiers:
                    raise InputError("Unsupported maintained path modifier")
                kind, arguments = modifiers[modifier], (value.path,)
            else:
                raise InputError("Unsupported property path object")
        if kind not in {
            "sequence",
            "alternative",
            "inverse",
            "zero_or_more",
            "one_or_more",
            "zero_or_one",
        }:
            raise InputError("Unsupported property path operator")
        if not isinstance(arguments, (tuple, list)):
            raise InputError("Maintained path arguments must be a finite sequence")
        count = len(arguments)
        if count > operation.limits.max_states:
            raise LimitExceeded("Path argument count exceeds max_states")
        if count < 2 if kind in ("sequence", "alternative") else count != 1:
            raise InputError("Invalid property path operator arity")
        captured: list[object] = []
        for index in range(count):
            operation.consume("work")
            operation.consume("states")
            captured.append(arguments[index])
        children = tuple(
            walk(item, active | {id(value)}, depth + 1) for item in captured
        )
        return RdfPath(kind, source, children)

    return walk(path, frozenset(), 0)
