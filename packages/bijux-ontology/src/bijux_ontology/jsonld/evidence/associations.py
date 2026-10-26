"""Incremental standards-observed source objects and association limitations."""

from collections.abc import Callable, Iterator
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.json.paths import SourcePath
from bijux_ontology.jsonld.evidence.entities import SourceEntity, source_entity
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage


def source_associations(
    document: object,
    observe: Callable[[SourcePath], ObservedUsage],
    budget: Budget,
) -> Iterator[SourceEntity | ObservedUsage]:
    """Yield completed associations or original unresolved usage evidence.

    Descriptions, references, graph scope and opaque content retain their actual
    observed identities. Selection/count/absence policies belong to callers.
    """

    def walk(value: object, path: SourcePath) -> Iterator[SourceEntity | ObservedUsage]:
        """Skip contexts and opaque payloads while checking node identity."""
        budget.consume("work")
        if not isinstance(value, (list, dict)):
            return
        try:
            usage = observe(path) if path else None
        except LimitExceeded:
            raise
        except InputError:
            return
        if usage is not None and any(
            item.traits.kind == "literal"
            and isinstance(item.value, dict)
            and "@value" in item.value
            and item.value["@value"] == value
            for item in usage.occurrences
        ):
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                budget.consume("states")
                budget.reserve_storage(64 + 8 * (len(path) + 1))
                yield from walk(item, (*path, index))
            return
        node = cast(dict[str, object], value)
        if "@value" in node:
            return
        budget.consume("work", len(node))
        wrapper = bool(node) and all(
            key
            in {
                "@context",
                "@graph",
                "@included",
                "@list",
                "@set",
                "@index",
            }
            for key in node
        )
        if not wrapper:
            try:
                binding = source_entity(node, path, observe, budget)
            except LimitExceeded:
                raise
            except InputError:
                binding = None
            if binding is not None:
                yield binding
            elif usage is not None and usage.limitation:
                yield usage
        for key, item in node.items():
            if key != "@context":
                budget.consume("states")
                budget.reserve_storage(64 + 8 * (len(path) + 1))
                yield from walk(item, (*path, key))

    yield from walk(document, ())
