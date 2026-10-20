"""Selected existential relation paths with original projection witnesses."""

from collections import deque
from collections.abc import Sequence

from rdflib import URIRef
from rdflib.namespace import OWL

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView

from .projection import _named, _operation, _Projection, _report
from .types import RestrictionPath, RestrictionProjection


def existential_path(
    view: VocabularyView,
    source: str,
    target: str,
    *,
    properties: Sequence[str],
    inherited: bool = True,
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> RestrictionPath:
    """Find a nonempty path of explicitly selected ``someValuesFrom`` projections.

    Path composition describes syntactic relation navigation, not an OWL
    transitive-property entailment. A found witness survives other unsupported
    alternatives; its report retains those projection coverage limitations.
    Universal/value restrictions never serve as existential steps.
    """
    operation = _operation(view, budget)
    start = _named(source, operation)
    goal = _named(target, operation)
    if type(inherited) is not bool:
        raise InputError("inherited must be a boolean")
    maximum = operation.limits.max_depth if max_depth is None else max_depth
    if type(maximum) is not int or maximum < 0:
        raise InputError("Restriction path depth requires a nonnegative integer")
    maximum = min(maximum, operation.limits.max_depth)
    if not isinstance(properties, Sequence) or isinstance(properties, str | bytes):
        raise InputError("Restriction properties require a finite sequence")
    count = len(properties)
    if not count:
        raise InputError("Restriction navigation requires at least one property")
    operation.consume("states", count)
    operation.consume("bytes", 128 + 64 * count)
    selected = frozenset(_named(properties[index], operation) for index in range(count))
    state = _Projection(view, operation)
    queue: deque[tuple[URIRef, tuple[RestrictionProjection, ...]]] = deque(
        [(start, ())]
    )
    seen = {start}
    found: tuple[RestrictionProjection, ...] = ()
    try:
        while queue and not found:
            owner, path = queue.popleft()
            for projection in state.collect(owner, inherited):
                operation.consume("work")
                if (
                    projection.status != "supported"
                    or projection.quantifier != OWL.someValuesFrom
                    or projection.property not in selected
                    or not isinstance(projection.filler, URIRef)
                ):
                    continue
                if len(path) >= maximum:
                    state.issues.exhausted(
                        "Restriction path exceeds max_depth", location=str(owner)
                    )
                    state.complete = False
                    continue
                operation.consume("work", len(path) + 1)
                operation.consume("states", len(path) + 2)
                operation.consume("bytes", 128 + 64 * (len(path) + 1))
                following = (*path, projection)
                if projection.filler == goal:
                    found = following
                elif projection.filler not in seen:
                    seen.add(projection.filler)
                    queue.append((projection.filler, following))
            # Finish the selected owner's projection inventory even after finding
            # a witness, retaining malformed/unsupported sibling alternatives.
    except LimitExceeded as error:
        state.issues.exhausted(str(error), location=str(start))
        state.complete = False
    return RestrictionPath(
        "found" if found else "unreachable" if state.complete else "incomplete",
        found,
        _report("terms.existential_path", len(found), state.issues, state.complete),
    )
