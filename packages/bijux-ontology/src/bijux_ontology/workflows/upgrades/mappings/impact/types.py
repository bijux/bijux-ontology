"""Explicit directional consumer obligations over original mapping occurrences."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.types import MappingSet


@dataclass(frozen=True)
class MappingUse:
    """One actual record/location consuming an original directional mapping row.

    Direction belongs to the consumer. MappingPolicy cardinalities retain the
    authored table's subject/object orientation in either consumer direction.
    """

    record: str
    location: str
    occurrence: MappingOccurrence
    direction: Literal["forward", "reverse"]


@dataclass(frozen=True)
class MappingCoverage:
    """Caller-declared exact record inventory; completeness never means all users."""

    records: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class MappingQualifications:
    """Exact lexical row cells and set metadata selected as consumer obligations.

    Endpoint identifiers, predicate and declared endpoint/predicate roles always
    belong to the contract. Additional fields are explicit, including confidence
    or justification when wanted; neither is automatically semantic truth.
    Selected justification uses its already expanded native identity. Other
    selected identifier slots require qualified prefix interpretation; a changed
    prefix context stays unknown while ordinary literal cells remain lexical.
    Metadata selection of curie_map binds the actual retained prefix map, even
    when the maintained producer omits it from the other metadata JSON.
    """

    cells: tuple[str, ...]
    metadata: tuple[str, ...]


@dataclass(frozen=True)
class MappingChange:
    """One unmatched original occurrence under the selected qualification contract."""

    status: Literal["added", "removed"]
    occurrence: MappingOccurrence


@dataclass(frozen=True)
class MappingObligation:
    """An actual supplied use, exact surviving rows and introduced requirement loss."""

    use: MappingUse
    status: Literal["preserved", "broken", "unverified"]
    surviving: tuple[MappingOccurrence, ...]
    failures: tuple[Issue, ...]


@dataclass(frozen=True)
class MappingImpact:
    """Scoped release impact, original tables and independently retained checks.

    Raw application-policy findings are retained even for unused rows. The
    verdict demonstrates a consumer break only through a supplied obligation;
    agreement qualifies only the declared finite record inventory.
    """

    verdict: Literal["demonstrated-break", "unverified", "informational", "agreement"]
    before: MappingSet | None
    after: MappingSet | None
    coverage: MappingCoverage | None
    qualifications: MappingQualifications | None
    changes: tuple[MappingChange, ...]
    obligations: tuple[MappingObligation, ...]
    before_check: Report | None
    after_check: Report | None
    report: Report
