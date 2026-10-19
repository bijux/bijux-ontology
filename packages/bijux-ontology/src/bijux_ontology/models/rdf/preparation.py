"""Bounded subject admission and live declared-child RDF obligation discovery."""

from collections.abc import Sequence

from rdflib import BNode, Graph, URIRef
from rdflib.term import Node

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.models.contracts.registry import index_models
from bijux_ontology.models.emission.matching import (
    ValueObservation,
    identity_matches,
)
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.value_constraints.checking import (
    retain_enumeration_report,
)


def prepare_model_obligations(
    models: Sequence[ModelDeclaration],
    budget: Budget,
    issues: IssueCollector,
    enumeration_reports: dict[tuple[int, bool], Report],
) -> tuple[dict[str, ModelDeclaration], bool]:
    """Admit actual model references and retain each paid enumeration report."""
    registry = index_models(models, budget, issues)
    needs_roles = False
    for declaration in registry.values():
        for field in declaration.fields:
            for branch in field.values:
                budget.consume("work")
                needs_roles |= branch.role is not None
                retain_enumeration_report(
                    branch.constraints,
                    enumeration_reports,
                    budget=budget,
                    issues=issues,
                )
                if branch.model is not None and branch.model not in registry:
                    issues.append(
                        Issue(
                            "input.model_reference_missing",
                            "Value branch references an unknown model",
                            f"{declaration.identifier}.{field.name}",
                            kind="input_error",
                            actual=branch.model,
                        )
                    )
    return registry, needs_roles


class RdfBindingDiscovery:
    """Own exact selected subjects and a live queue of recursive obligations."""

    def __init__(
        self,
        graph: Graph,
        registry: dict[str, ModelDeclaration],
        budget: Budget,
        issues: IssueCollector,
    ) -> None:
        self.registry = registry
        self.budget = budget
        self.issues = issues
        budget.consume("resources")
        budget.consume("triples", len(graph))
        self.nodes: set[Node] = set()
        self.blank_objects: set[int] = set()
        for subject, _, value in graph:
            budget.consume("work")
            self.nodes.update((subject, value))
            self.blank_objects.update(
                id(node) for node in (subject, value) if isinstance(node, BNode)
            )
        self.pending: list[RdfModelUsage] = []
        self.selected: set[tuple[str, Node]] = set()

    def bind(self, binding: RdfModelUsage) -> None:
        """Retain each exact model/subject obligation once, including recursion."""
        identity = binding.model, binding.subject
        if identity not in self.selected:
            self.budget.consume("states")
            self.selected.add(identity)
            self.pending.append(binding)

    def admit(self, usages: Sequence[RdfModelUsage]) -> None:
        """Validate original requests in order before recursive discovery starts."""
        for binding in usages:
            self.budget.consume("work")
            if not isinstance(binding, RdfModelUsage):
                raise InputError("Expected typed RDF model usage bindings")
            if binding.model not in self.registry:
                self.issues.append(
                    Issue(
                        "input.model_reference_missing",
                        "RDF usage references an unknown model",
                        binding.location or f"entity[{binding.subject}]",
                        kind="input_error",
                        actual=binding.model,
                    )
                )
                continue
            if (
                isinstance(binding.subject, BNode)
                and id(binding.subject) not in self.blank_objects
            ):
                self.issues.append(
                    Issue(
                        "emission.node_unverified",
                        "Blank subject did not come from this owned snapshot",
                        binding.location or f"entity[{binding.subject}]",
                        kind="unverified",
                    )
                )
                continue
            self.bind(binding)
        if not usages:
            self.issues.append(
                Issue(
                    "emission.node_unverified",
                    "No RDF subject binding was supplied",
                    kind="unverified",
                )
            )

    def discover(
        self,
        field: FieldDeclaration,
        node: Node,
        actual: ValueObservation,
        matches: tuple[bool | None, ...],
        *,
        empty: bool,
        field_where: str,
    ) -> None:
        """Append established children once; iteration must keep this queue live."""
        targets = {
            branch.model
            for branch, match in zip(field.values, matches, strict=True)
            if branch.model is not None and match is not False
        }
        if targets and not empty and isinstance(node, URIRef | BNode):
            established = {
                target
                for target in targets
                if target in self.registry
                and set(self.registry[target].types) <= set(actual.types)
                and identity_matches(self.registry[target].subject, actual)
            }
            if len(established) == 1:
                self.bind(RdfModelUsage(next(iter(established)), node))
            elif not any((target, node) in self.selected for target in targets):
                self.issues.append(
                    Issue(
                        "emission.nested_scope_unverified",
                        "Linked model requires an unambiguous subject binding",
                        field_where,
                        kind="unverified",
                    )
                )
