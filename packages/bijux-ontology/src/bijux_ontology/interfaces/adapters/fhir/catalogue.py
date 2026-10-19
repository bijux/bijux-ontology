"""One operation's exact supplied terminology resources and version index."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits, bound_text

from .code_system.reading import read_code_system
from .code_system.types import FhirCodeSystem


@dataclass(frozen=True, slots=True)
class CodeSystemResolution:
    """Resource selection only; code membership and consumer policy stay separate."""

    status: Literal["missing", "ambiguous", "unsupported", "resolved"]
    code_system: FhirCodeSystem | None = None


@dataclass(frozen=True, init=False, slots=True)
class CodeSystemCatalogue:
    """Fresh native resources and their exact source keys, scoped to one budget."""

    systems: tuple[FhirCodeSystem, ...]
    _owner: Budget
    _limits: Limits
    _index: Mapping[tuple[str, str | None], tuple[FhirCodeSystem, ...]]

    def __init__(self) -> None:
        """Require actual bounded resource preparation rather than asserted entries."""
        raise InputError("Use prepare_code_systems for an owned terminology catalogue")

    def require_owner(
        self, budget: Budget, systems: tuple[FhirCodeSystem, ...]
    ) -> None:
        """Reject another operation or inventory before any prepared reuse."""
        if (
            type(self) is not CodeSystemCatalogue
            or budget is not self._owner
            or systems is not self.systems
            or budget.limits != self._limits
        ):
            raise InputError("Terminology catalogue belongs to another operation")
        budget.consume("work", 0)

    def resolve(
        self, system: str, version: str | None, *, budget: Budget
    ) -> CodeSystemResolution:
        """Select exact source metadata without inferring a missing business version."""
        self.require_owner(budget, self.systems)
        if type(system) is not str or type(version) not in (str, type(None)):
            raise InputError("Terminology identities require original text")
        bound_text(system, budget)
        if version is not None:
            bound_text(version, budget)
        budget.consume("work", len(system) + len(version or "") + 1)
        budget.consume("states")
        budget.reserve_storage(160)
        candidates = self._index.get((system, version), ())
        if not candidates:
            return CodeSystemResolution("missing")
        if len(candidates) != 1:
            return CodeSystemResolution("ambiguous")
        selected = candidates[0]
        return CodeSystemResolution(
            "resolved" if selected.report.verified else "unsupported", selected
        )


def prepare_code_systems(
    systems: tuple[FhirCodeSystem, ...],
    budget: Budget,
    *,
    on_original: Callable[[FhirCodeSystem], None],
    on_captured: Callable[[FhirCodeSystem], None],
) -> CodeSystemCatalogue:
    """Prepare actual sources once, retaining domain reports before later work.

    Callbacks preserve each consumer's original admission and report-retention
    points. Index keys come only from the freshly read payload; duplicate source
    keys retain every occurrence and therefore cannot select a first version.
    """
    if type(systems) is not tuple or not isinstance(budget, Budget):
        raise InputError("Terminology preparation requires a finite tuple and budget")
    limits = budget.limits
    count = len(systems)
    # Fund both index tables, key/bucket tuples and captured-resource references.
    budget.consume("work", 2 * count + 1)
    budget.consume("states", 3 * count + 1)
    budget.reserve_storage(512 + 512 * count)
    captured: list[FhirCodeSystem] = []
    index: dict[tuple[str, str | None], list[FhirCodeSystem]] = {}
    for original in systems:
        if not isinstance(original, FhirCodeSystem):
            raise InputError("Local terminology requires owned CodeSystem records")
        on_original(original)
        if budget.limits != limits:
            raise InputError("Terminology policy changed during source preparation")
        resource = read_code_system(
            original.payload,
            bindings=original.bindings,
            case_comparison=original.case_comparison,
            budget=budget,
        )
        on_captured(resource)
        if budget.limits != limits:
            raise InputError("Terminology policy changed during source preparation")
        bound_text(resource.system, budget)
        if resource.version is not None:
            bound_text(resource.version, budget)
        budget.consume("work", len(resource.system) + len(resource.version or "") + 1)
        key = resource.system, resource.version
        index.setdefault(key, []).append(resource)
        captured.append(resource)
    owned = object.__new__(CodeSystemCatalogue)
    object.__setattr__(owned, "systems", tuple(captured))
    object.__setattr__(owned, "_owner", budget)
    object.__setattr__(owned, "_limits", limits)
    object.__setattr__(
        owned,
        "_index",
        MappingProxyType({key: tuple(values) for key, values in index.items()}),
    )
    return owned
