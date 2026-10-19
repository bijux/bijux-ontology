"""Prepare actual usages and compare every supported selected occurrence."""

from collections.abc import Sequence
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.paths import SourcePath, bound_path, bounded_location, location
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.evidence.entities import EntityInventory
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence, ObservedUsage
from bijux_ontology.jsonld.evidence.values import ValueTraits, value_traits
from bijux_ontology.jsonld.selectors.checking import resolve_selection
from bijux_ontology.jsonld.selectors.types import (
    EntitySelector,
    Selection,
    SourceSelector,
)

from .expansion import _drop_issues, _validate_allowed_drops, expand_document
from .expectations import ContextExpectation, compare_usage
from .omissions import project_omissions
from .resources import (
    ContextInputError,
    DocumentLoader,
    context_resources,
)
from .selection import UsageIndex


class PreparedUsages:
    """Own one JSON-LD operation and reuse sound occurrence associations.

    Original expansion and effective resources are prepared once. Selections use
    semantic multiset differences, never positional marker correspondence. A
    valid but ambiguous derivative remains unverified, not malformed input.
    """

    def __init__(
        self,
        document: object,
        *,
        base: str | None = None,
        document_loader: DocumentLoader | None = None,
        allowed_drops: frozenset[str] = frozenset(),
        allowed_omissions: tuple[SourcePath, ...] = (),
        limits: Limits | None = None,
        budget: Budget | None = None,
    ) -> None:
        _validate_allowed_drops(allowed_drops)
        if not isinstance(allowed_omissions, tuple):
            raise InputError("allowed_omissions requires a tuple of concrete paths")
        self._allowed_drops = allowed_drops
        self._loader = context_resources(document_loader, limits=limits, budget=budget)
        self._budget = self._loader.budget
        self._document = own_json(
            document,
            max_bytes=self._budget.limits.max_bytes,
            max_depth=self._budget.limits.max_depth,
            budget=self._budget,
        )
        self._base = base
        dropped: list[str | None] = []
        expanded = expand_document(
            self._document,
            base=base,
            document_loader=self._loader,
            on_property_dropped=dropped.append,
        )
        self._omissions: tuple[SourcePath, ...] = ()
        self._omission_issues: tuple[Issue, ...] = ()
        if allowed_omissions:
            original = UsageIndex(
                self._document,
                self._expand,
                expanded,
                unscoped_resources=self._loader.unscoped,
                budget=self._budget,
            )
            projection = project_omissions(
                self._document,
                allowed_omissions,
                self._expand,
                original.select,
                self._budget,
            )
            self._omission_issues = projection.issues
            self._omissions = projection.paths
            if self._omissions:
                self._document = projection.document
                dropped.clear()
                expanded = expand_document(
                    self._document,
                    base=base,
                    document_loader=self._loader,
                    on_property_dropped=dropped.append,
                )
        self._dropped = tuple(dropped)
        self._root_nodes = tuple(value_traits(node, "") for node in expanded)
        self._entities = EntityInventory(expanded, self._budget)
        self._index = UsageIndex(
            self._document,
            self._expand,
            expanded,
            unscoped_resources=self._loader.unscoped,
            budget=self._budget,
        )

    @property
    def entities(self) -> EntityInventory:
        """Read graph-qualified asserted facts from this same owned expansion."""
        return self._entities

    @property
    def root_nodes(self) -> tuple[ValueTraits, ...]:
        """Inspect actual expanded roots without re-expansion or mutable output."""
        return self._root_nodes

    @property
    def allowed_drops(self) -> frozenset[str]:
        """Broad spelling exclusions covering every matching observed term."""
        return self._allowed_drops

    @property
    def allowed_omissions(self) -> tuple[SourcePath, ...]:
        """Applied whole-property exclusions, located in the actual original source."""
        return self._omissions

    def _expand(self, document: object) -> list[dict[str, object]]:
        return expand_document(document, base=self._base, document_loader=self._loader)

    def observe(
        self, path: SourcePath, *, budget: Budget | None = None
    ) -> ObservedUsage:
        """Return complete selected evidence within its preparation operation.

        A different budget may reuse an already cached selection. Uncached
        expansion stays with the originating budget and resource owner.
        """
        if (
            not isinstance(path, tuple)
            or not path
            or any(
                not isinstance(key, (str, int))
                or isinstance(key, bool)
                or isinstance(key, int)
                and key < 0
                for key in path
            )
        ):
            raise ContextInputError("Expected a nonempty key/index usage selector")
        operation = budget if budget is not None else self._budget
        bound_path(path, operation)
        if operation is not self._budget:
            cached = self._index._cache.get(path)
            if cached is None:
                raise ContextInputError(
                    "Uncached prepared selection requires "
                    "its originating operation budget"
                )
            return cached
        try:
            return self._index.select(path)
        except (KeyError, IndexError) as error:
            raise ContextInputError(
                "No supplied usage", location=bounded_location(path, self._budget)
            ) from error

    def observe_many(self, paths: Sequence[SourcePath]) -> tuple[ObservedUsage, ...]:
        """Share proven nested preparation while preserving each concrete usage."""
        if not isinstance(paths, Sequence) or isinstance(
            cast(object, paths), (str, bytes)
        ):
            raise ContextInputError("Expected an explicit sequence of usage selectors")
        if len(paths) > self._budget.limits.max_states:
            raise LimitExceeded("Observation batch exceeds max_states")
        selected = tuple(paths)
        for path in selected:
            if (
                not isinstance(path, tuple)
                or not path
                or any(
                    type(key) not in (str, int) or isinstance(key, int) and key < 0
                    for key in path
                )
            ):
                raise ContextInputError("Expected nonempty key/index usage selectors")
            bound_path(path, self._budget)
        self._index.prepare_many(selected)
        return tuple(self.observe(path) for path in selected)

    def select(self, selector: SourceSelector | EntitySelector) -> Selection:
        """Resolve typed finite matches with explicit source/entity coverage."""
        return resolve_selection(
            selector, self._document, self._entities, self.observe, self._budget
        )

    def _index_derivative(
        self, path: SourcePath
    ) -> tuple[
        ObservedUsage,
        tuple[ObservedOccurrence, ...],
        tuple[ObservedOccurrence, ...],
        str | None,
        str | None,
    ]:
        """Reuse owned identities and bounded derivative processing."""
        return self._index.index_derivative(path)

    def check(
        self,
        expectations: Sequence[ContextExpectation],
        *,
        allow_empty: bool = False,
    ) -> Report:
        """Check retained occurrences; exclusions never prove retained coverage.

        Each applied path excludes only its whole source property. Excluded
        expectations do not satisfy the required minimum retained scope.
        """
        issues = IssueCollector(self._budget)
        checked = 0
        requested = len(expectations)
        excluded = 0
        try:
            issues.extend(self._omission_issues)
            issues.extend(_drop_issues(self._dropped, self._allowed_drops))
            for expectation in expectations:
                bound_path(expectation.path, self._budget)
            self._index.prepare_many(
                tuple(
                    expectation.path
                    for expectation in expectations
                    if not any(
                        expectation.path[: len(path)] == path
                        for path in self._omissions
                    )
                )
            )
            for expectation in expectations:
                self._budget.consume("work")
                if any(
                    expectation.path[: len(path)] == path for path in self._omissions
                ):
                    excluded += 1
                    requested -= 1
                    continue
                where = bounded_location(expectation.path, self._budget)
                if expectation.predicate.startswith(
                    "@"
                ) and expectation.predicate not in {
                    "@id",
                    "@type",
                }:
                    checked += 1
                    issues.append(
                        Issue(
                            "context.unsupported_check",
                            "Structural keyword comparisons need a contained usage",
                            where,
                            kind="unverified",
                        )
                    )
                    continue
                try:
                    usage = self.observe(expectation.path)
                except LimitExceeded:
                    raise
                except InputError as error:
                    issues.append(
                        Issue(
                            "context.invalid_path",
                            str(error),
                            where,
                            kind="input_error",
                        )
                    )
                    continue
                checked += 1
                if usage.limitation or usage.empty:
                    issues.append(
                        Issue(
                            "context.value_unverified",
                            usage.limitation or "No representative expanded value",
                            where,
                            kind="unverified",
                        )
                    )
                    continue
                if usage.dropped:
                    issues.append(
                        Issue(
                            "context.term_dropped",
                            "The supplied field did not survive expansion",
                            where,
                        )
                    )
                    continue
                issues.extend(compare_usage(expectation, usage, budget=self._budget))
            if excluded and not requested and not allow_empty:
                checked += 1
                requested += 1
                issues.append(
                    Issue(
                        "context.required_scope_excluded",
                        "Required checking coverage contains no retained usage",
                        expected="at least one retained usage",
                        actual="0",
                    )
                )
        except LimitExceeded as error:
            issues.exhausted(str(error))
        return Report(
            "contexts",
            checked,
            tuple(dict.fromkeys(issues)),
            requested=requested,
            allow_empty=allow_empty,
            complete=not any(issue.kind == "exhausted" for issue in issues),
            regime="jsonld",
            families=("usage_mapping", "value_traits"),
            exclusions=tuple(
                f"omitted observed term: {term}" for term in sorted(self._allowed_drops)
            )
            + tuple(
                f"excluded source property: {location(path)}"
                for path in self._omissions
            ),
        )


def prepare_usages(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    allowed_omissions: tuple[SourcePath, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PreparedUsages:
    """Prepare owned resources and actual occurrences for repeated checks."""
    return PreparedUsages(
        document,
        base=base,
        document_loader=document_loader,
        allowed_drops=allowed_drops,
        allowed_omissions=allowed_omissions,
        limits=limits,
        budget=budget,
    )


def check_context(
    document: object,
    expectations: Sequence[ContextExpectation],
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    allowed_omissions: tuple[SourcePath, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check complete selected usages through public standards processing."""
    try:
        prepared = prepare_usages(
            document,
            base=base,
            document_loader=document_loader,
            allowed_drops=allowed_drops,
            allowed_omissions=allowed_omissions,
            limits=limits,
            budget=budget,
        )
    except LimitExceeded as error:
        return Report(
            "contexts",
            issues=(Issue("execution.limit_exceeded", str(error), kind="exhausted"),),
            requested=len(expectations),
            complete=False,
            regime="jsonld",
        )
    except InputError as error:
        return Report(
            "contexts",
            issues=(
                Issue(
                    "context.processor_unverified"
                    if isinstance(error, ResourceError)
                    else "context.input_error",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                ),
            ),
            requested=len(expectations),
            complete=False,
            regime="jsonld",
        )
    return prepared.check(expectations)
