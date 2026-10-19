"""Bounded role-aware relationship query and native node wire representation."""

from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

from ...axioms.types import DataProperty, NamedEntity, ObjectProperty
from ...backend.owlapi.protocol import (
    result_boolean,
    result_counter,
    result_records,
    result_text,
)
from ...backend.types import ProfileDocument
from ...documents.types import OwlDocument
from .types import (
    InferredQuery,
    RelationshipGroup,
    RelationshipMember,
    RelationshipValue,
)


def _text(value: object, budget: Budget) -> str:
    """Bound ordinary input IRIs before validation, comparison or hashing."""
    if type(value) is not str:
        raise InputError("Relationship query IRIs require plain text")
    bound_text(value, budget)
    return str(absolute_iri(value))


def _query(query: InferredQuery, budget: Budget) -> dict[str, object] | None:
    """Encode the maintained finite profile and preserve unsupported work."""
    if type(query) is not InferredQuery:
        raise InputError("Relationship requests require ordinary InferredQuery records")
    budget.consume("states", 3)
    budget.consume("bytes", 384)
    relation = cast(object, query.relation)
    if type(relation) is not str:
        raise InputError("Relationship query names require plain text")
    bound_text(relation, budget)
    if relation not in (
        "super",
        "sub",
        "equivalent",
        "inverse",
        "domain",
        "range",
    ):
        raise InputError("Choose a supported relationship query name")
    hierarchical = query.relation in ("super", "sub", "domain", "range")
    if (hierarchical and type(query.direct) is not bool) or (
        not hierarchical and query.direct is not None
    ):
        raise InputError(
            "Select explicit direct/indirect depth only for hierarchy queries"
        )
    value: dict[str, object]
    focus = query.focus
    if type(focus) is NamedEntity:
        role = cast(object, focus.kind)
        if type(role) is not str:
            raise InputError("Named query roles require plain text")
        bound_text(role, budget)
        iri = _text(focus.iri, budget)
        if focus.kind != "class":
            return None
        value = {"kind": "class", "iri": iri}
        if query.relation not in ("super", "sub", "equivalent"):
            return None
    elif type(focus) is ObjectProperty:
        if type(focus.inverse) is not bool:
            raise InputError("Object property inverse selection requires a boolean")
        value = {
            "kind": "object_property",
            "iri": _text(focus.iri, budget),
            "inverse": focus.inverse,
        }
    elif type(focus) is DataProperty:
        value = {"kind": "data_property", "iri": _text(focus.iri, budget)}
        if query.relation in ("range", "inverse"):
            return None
    else:
        raise InputError("Relationship focus requires an existing typed class/property")
    return {"focus": value, "relation": query.relation, "direct": query.direct}


def _value(raw: object, budget: Budget) -> RelationshipValue:
    """Retain exact named roles and inverse expressions rather than rendered strings."""
    item = object_value(raw, location="native relationship value")
    kind = result_text(item.get("kind"), budget)
    iri = result_text(item.get("iri"), budget)
    assert iri is not None
    absolute_iri(iri)
    budget.consume("bytes", 128)
    budget.consume("states")
    if kind == "class":
        return NamedEntity("class", iri)
    if kind == "object_property":
        return ObjectProperty(iri, result_boolean(item.get("inverse")))
    if kind == "data_property":
        return DataProperty(iri)
    raise ResourceError("Native relationship member has an unknown role")


def _groups(
    raw: object,
    query: InferredQuery,
    documents: tuple[OwlDocument | ProfileDocument, ...],
    budget: Budget,
    destination: list[RelationshipGroup],
) -> None:
    """Publish paid native nodes before later decoding can exhaust its allowance."""
    seen = set()
    class_results = type(query.focus) is NamedEntity or query.relation in (
        "domain",
        "range",
    )
    for raw_group in result_records(raw, budget):
        item = object_value(raw_group, location="native relationship equivalence node")
        budget.consume("states", 2)
        budget.consume("bytes", 192)
        top, bottom, complete = (
            result_boolean(item.get(name)) for name in ("top", "bottom", "complete")
        )
        members = []
        try:
            for raw_member in result_records(item.get("members"), budget):
                member = object_value(raw_member, location="native relationship member")
                value = _value(member.get("value"), budget)
                if (
                    (class_results and type(value) is not NamedEntity)
                    or (not class_results and type(value) is not type(query.focus))
                    or value in seen
                ):
                    raise ResourceError(
                        "Native relationship node role or uniqueness is invalid"
                    )
                budget.consume("states", 3)
                budget.consume("bytes", 192)
                seen.add(value)
                owners = []
                owned = set()
                for raw_owner in result_records(member.get("asserted_in"), budget):
                    index = result_counter(raw_owner, budget, len(documents) - 1)
                    if index in owned:
                        raise ResourceError("Duplicate asserted relationship owner")
                    budget.consume("bytes", 32)
                    budget.consume("states")
                    owned.add(index)
                    owners.append(documents[index])
                members.append(RelationshipMember(value, tuple(owners)))
        except LimitExceeded:
            if members:
                destination.append(
                    RelationshipGroup(tuple(members), top, bottom, False)
                )
            raise
        if complete and not members:
            raise ResourceError("Native complete equivalence node cannot be empty")
        destination.append(RelationshipGroup(tuple(members), top, bottom, complete))
