"""Role-qualified approval and namespace authority on original occurrences."""

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import IssueCollector, bound_text
from bijux_ontology.rdf.patches.types import PatchQuad, SnapshotWitness
from bijux_ontology.rdf.roles.classification import declaration_role
from bijux_ontology.rdf.roles.types import TermPosition, TermRole
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.registry.types import (
    IdentifierRegistry,
    NamespaceContribution,
)
from bijux_ontology.workflows.upgrades.uses import Use

from .types import ApprovedReplacement, ReplacementDecision, ReplacementSite

_PROPERTY = ("property", "object_property", "datatype_property", "annotation_property")


class Selection:
    """An operation-local decision log; callbacks see the unchanged witness only."""

    def __init__(
        self,
        approvals: tuple[ApprovedReplacement, ...],
        ownership: IdentifierRegistry,
        before: Graph,
        after: Graph,
        witness: SnapshotWitness,
        budget: Operation,
        issues: IssueCollector,
    ) -> None:
        """Qualify every approved endpoint before any consumer discriminator runs."""
        self.budget, self.issues, self.witness = budget, issues, witness
        self.decisions: list[ReplacementDecision] = []
        self.rules: dict[URIRef, list[ApprovedReplacement]] = {}
        self.kinds: dict[URIRef, tuple[TermRole, ...]] = {}
        self.present: set[tuple[URIRef, TermRole]] = set()
        self.joins: dict[tuple[URIRef, TermRole], dict[URIRef, bool]] = {}
        contributions = capture_items(
            ownership.contributions, NamespaceContribution, budget
        )
        for item in contributions:
            for text in (
                item.namespace,
                item.owner,
                item.canonical_prefix,
                *item.aliases,
            ):
                bound_text(text, budget)
        for rule in approvals:
            bound_text(rule.kind, budget)
            bound_text(rule.owner, budget)
            budget.consume("states", len(rule.sources) + len(rule.targets))
            budget.consume("bytes", 16 * (len(rule.sources) + len(rule.targets)))
            for iri in (*rule.sources, *rule.targets):
                bound_node(iri, budget)
                matches = []
                for item in contributions:
                    budget.consume("work", len(item.namespace) + 1)
                    if str(iri).startswith(item.namespace):
                        budget.consume("states")
                        matches.append(item)
                longest = max((len(item.namespace) for item in matches), default=-1)
                owners = {
                    item.owner for item in matches if len(item.namespace) == longest
                }
                if owners != {rule.owner}:
                    self.unknown(
                        "Replacement endpoint lacks the explicitly approved namespace "
                        "owner"
                    )
            for source in rule.sources:
                if any(old.kind == rule.kind for old in self.rules.get(source, ())):
                    raise InputError(
                        "Conflicting replacement approvals for the same entity facet"
                    )
                budget.consume("states")
                self.rules.setdefault(source, []).append(rule)
                self.kinds[source] = self.qualify(before, source, rule.kind)
            target_roles = [
                self.qualify(after, target, rule.kind) for target in rule.targets
            ]
            if rule.kind == "property":
                budget.consume("work", len(rule.sources) + len(rule.targets))
                original_facets = {
                    frozenset(
                        role for role in self.kinds[source] if role in _PROPERTY[1:]
                    )
                    for source in rule.sources
                }
                target_facets = {
                    frozenset(role for role in roles if role in _PROPERTY[1:])
                    for roles in target_roles
                }
                for original in original_facets:
                    for replacement in target_facets:
                        budget.consume("work")
                        if original == replacement:
                            continue
                        if original and replacement and original - replacement:
                            issues.append(
                                Issue(
                                    "upgrade.replacement_kind_mismatch",
                                    "Replacement loses an established property facet",
                                )
                            )
                        else:
                            self.unknown("Property facets are not fully qualified")
            if len(rule.sources) > 1 and not rule.merge:
                self.unknown("Many-to-one replacements require explicit merge intent")

    def unknown(self, message: str) -> None:
        """Retain a fixed diagnostic without copying unqualified caller metadata."""
        self.issues.append(
            Issue("upgrade.replacement_unverified", message, kind="unverified")
        )

    def roles(self, graph: Graph, iri: URIRef) -> tuple[TermRole, ...]:
        """Read exact named declaration/individual witnesses under the shared budget."""
        roles: set[TermRole] = set()
        for kind in graph.objects(iri, RDF.type):
            self.budget.consume("work")
            bound_node(kind, self.budget)
            role = declaration_role(kind)
            if kind == SKOS.Concept:
                roles.add("concept")
            elif role is not None:
                roles.add(role)
            elif isinstance(kind, URIRef):
                roles.add("individual")
        return tuple(sorted(roles))

    def qualify(
        self, graph: Graph, iri: URIRef, role: TermRole
    ) -> tuple[TermRole, ...]:
        """
        Wrong established facets are findings; missing role evidence stays unknown.
        """
        actual = self.roles(graph, iri)
        matched = (
            role in actual or role == "property" and bool(set(actual) & set(_PROPERTY))
        )
        if not matched:
            self.issues.append(
                Issue(
                    "upgrade.replacement_kind_mismatch"
                    if actual
                    else "upgrade.replacement_unverified",
                    "Replacement endpoint lacks its approved entity kind",
                    kind="finding" if actual else "unverified",
                )
            )
        return actual

    def matching(
        self, iri: URIRef, roles: tuple[TermRole, ...]
    ) -> tuple[ApprovedReplacement, ...]:
        """
        Preserve known unselected roles while retaining unknown affected positions.
        """
        rules = self.rules.get(iri, ())
        self.budget.consume("work", len(rules) * (len(roles) + 1))
        self.budget.consume("states", len(rules))
        return tuple(
            rule
            for rule in rules
            if rule.kind in roles
            or "property" in roles
            and rule.kind in _PROPERTY
            or rule.kind == "property"
            and any(role in _PROPERTY for role in roles)
        )

    def rewrite(
        self,
        iri: URIRef,
        roles: tuple[TermRole, ...],
        *,
        use: Use | None = None,
        quad: PatchQuad | None = None,
        position: TermPosition | None = None,
    ) -> URIRef:
        """
        Select exactly one approved target without hint or callback-result guessing.
        """
        bound_node(iri, self.budget)
        rules = self.rules.get(iri)
        if rules is None:
            return iri
        if not roles or any(
            role in ("unknown", "structural", "declaration") for role in roles
        ):
            self.unknown("Affected occurrence has no unambiguous replaceable role")
            return iri
        # SKOS Concept positions describe one individual with an intrinsic facet.
        # Separate rdf:type C individual/class positions remain independent.
        if (
            set(roles) == {"concept", "individual"}
            and any(rule.kind == "concept" for rule in rules)
            and not any(rule.kind == "individual" for rule in rules)
        ):
            roles = ("concept",)
        selected = self.matching(iri, roles)
        if not selected:
            return iri
        targets: set[URIRef] = set()
        for rule in selected:
            self.budget.consume("work")
            self.budget.consume("states")
            site = ReplacementSite(iri, rule.kind, self.witness, use, quad, position)
            chosen: URIRef | tuple[URIRef, ...] | None = (
                rule.targets[0] if len(rule.targets) == 1 else None
            )
            if rule.choose is not None:
                chosen = rule.choose(site)
                self.budget.consume("work")
            if isinstance(chosen, tuple):
                options = capture_items(chosen, URIRef, self.budget)
                chosen = options[0] if len(options) == 1 else None
            if chosen is not None and not isinstance(chosen, URIRef):
                raise InputError(
                    "Replacement discriminators return URIRef, candidate tuple or None"
                )
            if chosen is not None:
                bound_node(chosen, self.budget)
            self.budget.consume(
                "work",
                len(rule.targets) * (len(chosen) + 1 if chosen is not None else 1),
            )
            if chosen not in rule.targets:
                self.unknown(
                    "Split discriminator did not select exactly one approved target"
                )
                continue
            assert chosen is not None
            if (
                chosen != iri
                and any((chosen, role) in self.present for role in roles)
                and not rule.merge
            ):
                self.unknown(
                    "Joining an existing target occurrence requires explicit merge "
                    "intent"
                )
            key = (chosen, "property" if rule.kind in _PROPERTY else rule.kind)
            origins = self.joins.get(key, {})
            self.budget.consume("work", len(origins) + 1)
            if any(
                old != iri and (not allowed or not rule.merge)
                for old, allowed in origins.items()
            ):
                self.unknown("Selected many-to-one joins require explicit merge intent")
            self.budget.consume("states")
            origins[iri] = rule.merge
            self.joins[key] = origins
            self.budget.consume("states")
            self.budget.consume("bytes", 192)
            self.decisions.append(ReplacementDecision(site, chosen))
            targets.add(chosen)
        # One physical occurrence cannot be rewritten separately for two facets.
        if len(targets) != 1 or (len(selected) != len(roles) and len(roles) != 1):
            self.unknown(
                "One occurrence requires conflicting or inseparable facet decisions"
            )
            return iri
        return next(iter(targets))
