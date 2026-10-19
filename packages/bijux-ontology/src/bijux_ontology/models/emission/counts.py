"""Count actual RDF objects and list positions without importing OWL semantics."""

from collections.abc import Sequence

from rdflib import RDF, Literal

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import OutputRequirement
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.observations import ObservedOccurrence
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.reports import Issue


def check_occurrences(
    requirement: OutputRequirement | None,
    occurrences: Sequence[ObservedOccurrence],
    *,
    where: str,
    budget: Budget,
    native_literals: dict[str, Literal],
    present: bool | None = None,
    raw: object = ABSENT,
) -> tuple[Issue, ...]:
    """Keep source presence/null, distinct RDF values and per-list lengths separate."""
    if requirement is None:
        return ()
    issues = []
    budget.consume("work")
    if requirement.require_property and present is not True:
        issues.append(
            Issue(
                "emission.source_presence_unverified"
                if present is None
                else "emission.required_occurrence_missing",
                "Source property presence is unavailable"
                if present is None
                else "Required source property was omitted",
                where,
                kind="unverified" if present is None else "finding",
            )
        )
    if present is True and raw is None and not requirement.allow_null:
        issues.append(
            Issue("emission.null_forbidden", "Selected source property is null", where)
        )
    selected = tuple(occurrences)
    if present is True and not selected and not requirement.allow_empty:
        issues.append(
            Issue(
                "emission.empty_forbidden",
                "Selected property emits no RDF value",
                where,
            )
        )
    values: set[object] = set()
    lists: dict[tuple[object, ...], int] = {}
    for item in selected:
        budget.consume("work")
        scope = (
            item.subject,
            item.subject_identity,
            item.graph,
            item.graph_identity,
            item.anonymous_graph,
        )
        if item.container == "list":
            if item.list_id is None or item.list_length is None:
                issues.append(
                    Issue(
                        "emission.cardinality_unverified",
                        "List identity or length was not established",
                        where,
                        kind="unverified",
                    )
                )
                continue
            lists[(*scope, item.list_id)] = item.list_length
            values.add(
                (*scope, "resource", str(RDF.nil))
                if item.empty
                else (*scope, "list", item.list_id)
            )
            continue
        traits = item.traits
        if traits.kind == "literal":
            if traits.direction is not None:
                if (
                    requirement.minimum_values is not None
                    or requirement.maximum_values is not None
                ):
                    issues.append(
                        Issue(
                            "emission.cardinality_unverified",
                            "Directional RDF term correspondence is not established",
                            where,
                            kind="unverified",
                        )
                    )
                continue
            value = item.value
            if not isinstance(value, dict) or "@value" not in value:
                issues.append(
                    Issue(
                        "emission.cardinality_unverified",
                        "Literal identity was not established",
                        where,
                        kind="unverified",
                    )
                )
                continue
            lexical = value["@value"]
            term: Literal | None
            if isinstance(lexical, str):
                try:
                    term = literal_term(
                        lexical, datatype=traits.datatype, language=traits.language
                    )
                except InputError as error:
                    issues.append(
                        Issue(
                            "emission.cardinality_unverified",
                            str(error),
                            where,
                            kind="unverified",
                        )
                    )
                    continue
            else:
                term = native_literals.get(item.value_json)
                if term is None:
                    issues.append(
                        Issue(
                            "emission.cardinality_unverified",
                            "Exact native literal identity is unavailable",
                            where,
                            kind="unverified",
                        )
                    )
                    continue
            values.add((*scope, "literal", term))
        elif traits.identifier is not None:
            values.add((*scope, "resource", traits.identifier))
        elif item.identifier_identity is not None:
            values.add((*scope, "blank", item.identifier_identity))
        else:
            issues.append(
                Issue(
                    "emission.cardinality_unverified",
                    "Resource identity is unavailable",
                    where,
                    kind="unverified",
                )
            )
    issues.extend(
        check_output_counts(
            requirement,
            distinct_values=None
            if any(issue.code == "emission.cardinality_unverified" for issue in issues)
            else len(values),
            list_lengths=tuple(lists.values()),
            non_list=any(item.container != "list" for item in selected),
            where=where,
            budget=budget,
        )
    )
    return tuple(issues)


def check_output_counts(
    requirement: OutputRequirement,
    *,
    distinct_values: int | None,
    list_lengths: Sequence[int | None],
    non_list: bool,
    where: str,
    budget: Budget,
) -> tuple[Issue, ...]:
    """Apply output count bounds to established RDF identities and list lengths."""
    issues = []
    if distinct_values is not None and (
        requirement.minimum_values is not None or requirement.maximum_values is not None
    ):
        count = distinct_values
        if (
            count < (requirement.minimum_values or 0)
            or requirement.maximum_values is not None
            and count > requirement.maximum_values
        ):
            issues.append(
                Issue(
                    "emission.value_count_mismatch",
                    "Distinct RDF value count violates the output requirement",
                    where,
                    expected=(
                        f"{requirement.minimum_values or 0}.."
                        f"{requirement.maximum_values}"
                    ),
                    actual=str(count),
                )
            )
    if (
        requirement.minimum_list_members is not None
        or requirement.maximum_list_members is not None
    ):
        if non_list or not list_lengths and (requirement.minimum_values or 0) > 0:
            issues.append(
                Issue(
                    "emission.list_required",
                    "List-member bounds require actual RDF lists",
                    where,
                )
            )
        for index, length in enumerate(list_lengths):
            budget.consume("work")
            if length is None:
                issues.append(
                    Issue(
                        "emission.cardinality_unverified",
                        "Actual RDF list length is unavailable",
                        f"{where}/lists/{index}",
                        kind="unverified",
                    )
                )
                continue
            if (
                length < (requirement.minimum_list_members or 0)
                or requirement.maximum_list_members is not None
                and length > requirement.maximum_list_members
            ):
                issues.append(
                    Issue(
                        "emission.list_length_mismatch",
                        "RDF list positions violate the output requirement",
                        f"{where}/lists/{index}",
                        expected=(
                            f"{requirement.minimum_list_members or 0}.."
                            f"{requirement.maximum_list_members}"
                        ),
                        actual=str(length),
                    )
                )
    return tuple(issues)
