"""Consumer use joins for established anonymous RDF attachments."""

from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import chain
from typing import Literal

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.vocabulary.anonymous.types import (
    AnonymousChange,
    AnonymousDifferences,
)

from .facts import role_matches
from .uses import Use, UseIndex


@dataclass(frozen=True)
class AnonymousAffectedUse:
    """A supplied use reached by an attachment or an original named reference."""

    change: AnonymousChange
    use: Use
    relation: Literal["attachment", "reference", "hierarchy", "uncertain"]
    via: tuple[str, ...] = ()


def anonymous_uses(
    differences: AnonymousDifferences,
    uses: UseIndex,
    budget: Budget,
    issues: IssueCollector,
    result: list[AnonymousAffectedUse],
    *,
    dependency_children: dict[tuple[str, str], set[str]],
) -> None:
    """Scope possible impact without equating attribution with contract failure."""
    by_iri: dict[str, list[Use]] = defaultdict(list)
    for use in uses.entries:
        budget.consume("work")
        budget.consume("states")
        by_iri[use.iri].append(use)
    for change in differences.changes:
        seen: set[Use] = set()
        if not change.qualified or not uses.report.complete:
            issues.append(
                Issue(
                    "upgrade.anonymous_impact_unverified",
                    "Changed anonymous component lacks complete "
                    "named attachment or use coverage",
                    kind="unverified",
                )
            )
        for fact in chain(change.before, change.after):
            budget.consume("work")
            for occurrence in classify_statement(
                fact.statement,
                source_scope=fact.source_scope,
                graph=fact.graph.name,
                budget=budget,
            ):
                if not isinstance(occurrence.term, URIRef):
                    continue
                for use in by_iri.get(str(occurrence.term), ()):
                    budget.consume("work")
                    matches = role_matches(use, occurrence.role)
                    if matches is False or use in seen:
                        continue
                    budget.consume("states")
                    seen.add(use)
                    attached = occurrence.position == "subject" and any(
                        attachment.owner == use.iri
                        and attachment.predicate == str(fact.statement[1])
                        for attachment in change.attachments
                    )
                    result.append(
                        AnonymousAffectedUse(
                            change,
                            use,
                            "uncertain"
                            if matches is None
                            else "attachment"
                            if attached
                            else "reference",
                        )
                    )
                    issues.append(
                        Issue(
                            "upgrade.anonymous_impact_unverified",
                            "Changed anonymous attachment or reference "
                            "may affect this supplied use; "
                            "attribution does not establish OWL consequences",
                            use.location,
                            kind="unverified",
                        )
                    )
                domain = (
                    "class"
                    if occurrence.role == "class"
                    else "property"
                    if occurrence.role
                    in (
                        "property",
                        "object_property",
                        "datatype_property",
                        "annotation_property",
                    )
                    else None
                )
                if domain is None:
                    continue
                queue: deque[tuple[str, tuple[str, ...]]] = deque(
                    ((str(occurrence.term), ()),)
                )
                visited = {str(occurrence.term)}
                while queue:
                    iri, path = queue.popleft()
                    budget.consume("work")
                    if path:
                        for use in by_iri.get(iri, ()):
                            budget.consume("work")
                            if use in seen or not role_matches(use, occurrence.role):
                                continue
                            budget.consume("states")
                            seen.add(use)
                            result.append(
                                AnonymousAffectedUse(change, use, "hierarchy", path)
                            )
                            issues.append(
                                Issue(
                                    "upgrade.anonymous_impact_unverified",
                                    "Changed anonymous neighborhood may affect "
                                    "an asserted hierarchy dependency",
                                    use.location,
                                    kind="unverified",
                                )
                            )
                    for child in dependency_children.get((iri, domain), ()):
                        budget.consume("work")
                        if child in visited:
                            continue
                        if len(path) >= budget.limits.max_depth:
                            raise LimitExceeded(
                                "Anonymous use dependency exceeds max_depth"
                            )
                        budget.consume("states")
                        visited.add(child)
                        queue.append((child, (*path, iri)))
