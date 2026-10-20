"""Finite existing declaration cases translated into explicit logical questions."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import combinations

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.json.values import capture_json
from bijux_ontology.models.contracts.registry import index_models
from bijux_ontology.models.emission.groups import declaration_states, group_limitation
from bijux_ontology.models.persistence.codecs import declaration_data

from ..datatypes.types import DatatypeFieldBranch
from .types import ModelQuestion


@dataclass(frozen=True)
class LogicalPlan:
    """Owned inert declarations and exact question/branch inventories."""

    models: tuple[ModelDeclaration, ...]
    questions: tuple[ModelQuestion, ...]
    wire: tuple[dict[str, object], ...]
    datatypes: tuple[DatatypeFieldBranch, ...]


def _state_storage(model: ModelDeclaration, budget: Budget) -> None:
    """Reserve an upper bound before the existing emission enumerator allocates.

    This counts capacity only; declaration_states remains the semantic owner of
    scenario/condition intersection, exclusions and actual finite state evidence.
    """
    cap = budget.remaining("states")
    cross = total = 1
    for group in model.emission_groups:
        options = len(group.scenarios)
        if not group.scenarios or not group.closed:
            size = len(group.fields)
            upper = group.maximum if group.maximum is not None else size
            for chosen in range(group.minimum, upper + 1):
                # Stop before a combinatorial integer can grow beyond the cap.
                choose = min(chosen, size - chosen)
                count = 1
                for index in range(1, choose + 1):
                    count = count * (size - choose + index) // index
                    if count > cap:
                        raise LimitExceeded(
                            "Finite emission options exceed remaining states"
                        )
                options += count
                if options > cap:
                    raise LimitExceeded(
                        "Finite emission options exceed remaining states"
                    )
        cross *= max(1, options)
        if cross > cap:
            raise LimitExceeded("Finite emission product exceeds remaining states")
        total += options + cross
        if total > 3 * cap:
            raise LimitExceeded("Finite emission workspace exceeds remaining states")
    budget.consume("bytes", total * (1024 + len(model.fields) * 256))


def plan_models(
    values: Sequence[ModelDeclaration], budget: Budget, issues: IssueCollector
) -> LogicalPlan:
    """Reuse declaration codecs and emission states, never Python control flow."""
    supplied = capture_items(values, ModelDeclaration, budget)
    captured: list[ModelDeclaration] = []
    for model in supplied:
        if type(model) is not ModelDeclaration:
            raise InputError("Logical model checks require ordinary declarations")
        bound_text(model.identifier, budget)
        try:
            inert = declaration_data((model,), budget)
            copied = models_from_data(
                capture_json(
                    inert,
                    max_bytes=budget.limits.max_bytes,
                    max_depth=budget.limits.max_depth,
                    budget=budget,
                ).value
            )
            budget.consume("bytes", 128 + 64 * sum(len(item.fields) for item in copied))
            for item in copied:
                copied_fields = []
                for field in item.fields:
                    if not field.location:
                        length = len(item.identifier) + len(field.name) + 1
                        budget.consume("bytes", 128 + length)
                        budget.consume("work", length)
                        field = replace(
                            field, location=f"{item.identifier}.{field.name}"
                        )
                    copied_fields.append(field)
                captured.append(replace(item, fields=tuple(copied_fields)))
        except LimitExceeded:
            raise
        except InputError as error:
            issues.append(
                Issue(
                    "models.logical_declaration_unverified",
                    str(error),
                    model.identifier,
                    kind="unverified",
                )
            )
    registry = index_models(captured, budget, issues)
    questions: list[ModelQuestion] = []
    wire: list[dict[str, object]] = []
    datatypes: list[DatatypeFieldBranch] = []

    def append(
        question: ModelQuestion,
        *,
        types: tuple[str, ...],
        endpoint: str = "model",
        predicate: str | None = None,
        reverse: bool = False,
        assumptions: tuple[tuple[str, bool], ...] = (),
        unsupported: str | None = None,
    ) -> None:
        """Prepay both native request and terminal unknown answer references."""
        budget.consume("states", 4 + len(types) + len(assumptions))
        budget.consume(
            "bytes", 768 + 64 * (len(types) + len(assumptions) + len(question.present))
        )
        for text in (
            *types,
            question.model,
            question.location,
            question.field,
            predicate,
            unsupported,
        ):
            if text is not None:
                bound_text(text, budget)
        questions.append(question)
        wire.append(
            {
                "types": types,
                "kind": question.kind,
                "endpoint": endpoint,
                "predicate": predicate,
                "reverse": reverse,
                "assumptions": assumptions,
                "unsupported": unsupported,
            }
        )

    for model in registry.values():
        where = model.location or model.identifier
        append(
            ModelQuestion(model.identifier, "possible_satisfiability", where),
            types=model.types,
            unsupported=None if model.types else "Model has no declared class types",
        )
        fields = {field.name: field for field in model.fields}
        governed = {name for group in model.emission_groups for name in group.fields}
        limitations = tuple(
            reason
            for group in model.emission_groups
            if (reason := group_limitation(model, group))
        )
        _state_storage(model, budget)
        states = declaration_states(model, budget)
        if not states:
            issues.append(
                Issue(
                    "models.logical_emission_conflict",
                    "No emission state satisfies declared groups and required fields",
                    where,
                )
            )
        for field in model.fields:
            location = field.location or f"{model.identifier}.{field.name}"
            parent_unknown = (
                "Structural keywords have no global class endpoint"
                if field.predicate.startswith("@")
                else None
            )
            parent_cases = states if model.emission_groups else (None,)
            for state in parent_cases:
                if (
                    state is not None
                    and field.name in governed
                    and field.name not in state.present
                ):
                    continue
                assumptions = (
                    ()
                    if state is None
                    else tuple(
                        (fields[name].predicate, fields[name].reverse)
                        for name in sorted(
                            state.promised
                            | ({field.name} if field.name in governed else set())
                        )
                        if not fields[name].predicate.startswith("@")
                    )
                )
                present = () if state is None else tuple(sorted(state.present))
                covered = state is None or (state.covered and not state.conditions)
                append(
                    ModelQuestion(
                        model.identifier,
                        "required_subsumption",
                        location,
                        field.name,
                        present=present,
                        covered=covered,
                    ),
                    types=model.types,
                    endpoint="parent",
                    predicate=field.predicate,
                    reverse=field.reverse,
                    assumptions=assumptions,
                    unsupported=parent_unknown
                    or (limitations[0] if limitations else None)
                    or (None if model.types else "Model has no declared class types"),
                )
            if not field.values:
                issues.append(
                    Issue(
                        "models.logical_value_unverified",
                        "Field has no declared value branches",
                        location,
                        kind="unverified",
                    )
                )
            for index, value in enumerate(field.values):
                branch_location = f"{location}[{index}]"
                if value.kind == "literal":
                    budget.consume("bytes", 64)
                    budget.consume("states")
                    datatypes.append(DatatypeFieldBranch(field, index))
                    continue
                child = registry.get(value.model) if value.model else None
                types = tuple(
                    dict.fromkeys(
                        (
                            *value.types,
                            *(child.types if child is not None else ()),
                            *(
                                value.role.types
                                if value.role is not None
                                and value.role.kind == "instance"
                                and value.role.bridge is None
                                else ()
                            ),
                        )
                    )
                )
                unsupported = parent_unknown
                if (
                    value.kind == "unknown"
                    or value.container not in (None, "@set")
                    or value.direction not in (None, ABSENT)
                ):
                    unsupported = (
                        "Value context is outside the finite class endpoint profile"
                    )
                if value.model is not None and child is None:
                    unsupported = "Value references an unresolved child model"
                if value.role is not None and (
                    value.role.kind != "instance" or value.role.bridge is not None
                ):
                    unsupported = (
                        "Resource role or bridge needs a separate application proof"
                    )
                append(
                    ModelQuestion(
                        model.identifier,
                        "required_subsumption",
                        branch_location,
                        field.name,
                        index,
                    ),
                    types=types,
                    endpoint="value",
                    predicate=field.predicate,
                    reverse=field.reverse,
                    unsupported=unsupported,
                )
                append(
                    ModelQuestion(
                        model.identifier,
                        "possible_satisfiability",
                        branch_location,
                        field.name,
                        index,
                    ),
                    types=types,
                    unsupported=unsupported,
                )
        optional = tuple(
            field.name
            for field in model.fields
            if field.name not in governed
            and field.emission != "guaranteed"
            and not (
                field.output is not None and (field.output.minimum_values or 0) > 0
            )
        )
        # Ungoverned possibilities are not asserted co-emission promises. Enumerate
        # all bounded combinations instead of reducing three-field cases to pairs.
        if len(optional) >= budget.remaining("states").bit_length():
            raise LimitExceeded(
                "Optional emission combinations exceed remaining states"
            )
        for state in states:
            for size in range(len(optional) + 1):
                for additional in combinations(optional, size):
                    budget.consume("states")
                    budget.consume(
                        "bytes", 128 + 64 * (len(state.present) + len(additional))
                    )
                    present = tuple(sorted(state.present | set(additional)))
                    assumptions = tuple(
                        (fields[name].predicate, fields[name].reverse)
                        for name in present
                        if not fields[name].predicate.startswith("@")
                    )
                    append(
                        ModelQuestion(
                            model.identifier,
                            "coemission_contradiction",
                            where,
                            present=present,
                            covered=state.covered and not state.conditions,
                            guaranteed=not additional
                            and state.present <= state.promised,
                        ),
                        types=model.types,
                        assumptions=assumptions,
                        unsupported=(limitations[0] if limitations else None)
                        or (
                            None if model.types else "Model has no declared class types"
                        ),
                    )
    budget.consume(
        "bytes", 128 + 64 * (len(registry) + len(questions) + len(datatypes))
    )
    return LogicalPlan(
        tuple(registry.values()), tuple(questions), tuple(wire), tuple(datatypes)
    )
