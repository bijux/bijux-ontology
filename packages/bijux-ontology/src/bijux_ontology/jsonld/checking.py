"""Prepare actual usages and compare every supported selected occurrence."""

from collections.abc import Sequence

from bijux_ontology._json import own_json
from bijux_ontology.limits import Budget, IssueCollector, Limits

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report
from .entities import EntityInventory
from .expansion import _drop_issues, _validate_allowed_drops, expand_document
from .expectations import ContextExpectation, compare_usage
from .observations import ObservedUsage, SourcePath, location
from .resources import (
    ContextInputError,
    DocumentLoader,
    context_resources,
)
from .selection import UsageIndex
from .values import ValueTraits, value_traits


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
        limits: Limits | None = None,
        budget: Budget | None = None,
    ) -> None:
        _validate_allowed_drops(allowed_drops)
        self._allowed_drops = allowed_drops
        self._loader = context_resources(document_loader, limits=limits, budget=budget)
        self._budget = self._loader.budget
        self._document = own_json(
            document,
            max_bytes=self._budget.limits.max_bytes,
            max_depth=self._budget.limits.max_depth,
        )
        self._base = base
        dropped: list[str | None] = []
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
        """Exact observed terms outside preservation scope; omissions remain visible."""
        return self._allowed_drops

    def _expand(self, document: object) -> list[dict[str, object]]:
        return expand_document(document, base=self._base, document_loader=self._loader)

    def observe(self, path: SourcePath) -> ObservedUsage:
        """Return the complete supported selected usage, or its limitation."""
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
        try:
            return self._index.select(path)
        except (KeyError, IndexError) as error:
            raise ContextInputError(
                "No supplied usage", location=location(path)
            ) from error

    def check(
        self,
        expectations: Sequence[ContextExpectation],
        *,
        allow_empty: bool = False,
    ) -> Report:
        """Compare every selected occurrence without dropping unknown members."""
        issues = IssueCollector(self._budget)
        checked = 0
        try:
            issues.extend(_drop_issues(self._dropped, self._allowed_drops))
            for expectation in expectations:
                self._budget.consume("work")
                where = location(expectation.path)
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
        except LimitExceeded as error:
            issues.exhausted(str(error))
        return Report(
            "contexts",
            checked,
            tuple(dict.fromkeys(issues)),
            requested=len(expectations),
            allow_empty=allow_empty,
            complete=not any(issue.kind == "exhausted" for issue in issues),
            regime="jsonld",
            families=("usage_mapping", "value_traits"),
            exclusions=tuple(
                f"omitted observed term: {term}" for term in sorted(self._allowed_drops)
            ),
        )


def prepare_usages(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PreparedUsages:
    """Prepare owned resources and actual occurrences for repeated checks."""
    return PreparedUsages(
        document,
        base=base,
        document_loader=document_loader,
        allowed_drops=allowed_drops,
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
            issues=(Issue("context.input_error", str(error), kind="input_error"),),
            requested=len(expectations),
            complete=False,
            regime="jsonld",
        )
    return prepared.check(expectations)
