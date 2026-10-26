"""Exact consumer upgrade evidence, original findings and selected-use witnesses."""

from dataclasses import dataclass, field
from typing import Literal

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.results import Deprecation
from bijux_ontology.vocabulary.comparison import (
    Change,
    VocabularyChanges,
)
from bijux_ontology.workflows.upgrades.anonymous import (
    AnonymousAffectedUse,
)
from bijux_ontology.workflows.upgrades.facts import (
    FactAffectedUse,
)
from bijux_ontology.workflows.upgrades.uses import Use, UseIndex


@dataclass(frozen=True)
class ContractChange:
    """Stable check/location identity with original evidence from both versions."""

    status: Literal["introduced", "resolved", "persistent", "uncertain"]
    contract: str
    before: Issue | None
    after: Issue | None


@dataclass(frozen=True)
class AffectedUse:
    """An actual direct reference or a possible asserted-hierarchy dependency."""

    change: Change
    use: Use
    relation: Literal["direct", "hierarchy"]
    via: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeprecatedUse:
    """Newly deprecated occurrence with untouched replacement hints."""

    use: Use
    before: Deprecation
    after: Deprecation


@dataclass(frozen=True)
class UpgradeAssessment:
    """Finite contract verdict, raw differences and retained per-version checks."""

    verdict: Literal["demonstrated-break", "unverified", "informational", "agreement"]
    changes: VocabularyChanges
    uses: UseIndex
    affected: tuple[AffectedUse, ...]
    contracts: tuple[ContractChange, ...]
    deprecated: tuple[DeprecatedUse, ...]
    before: tuple[Report, ...]
    after: tuple[Report, ...]
    report: Report
    fact_uses: tuple[FactAffectedUse, ...] = field(default=(), kw_only=True)
    anonymous_uses: tuple[AnonymousAffectedUse, ...] = field(default=(), kw_only=True)
    change_qualification: Report | None = field(default=None, kw_only=True)
