"""Stable record manifests and distinct semantic/checkpoint completion."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.persistence.types import PreparationIdentity
from bijux_ontology.rdf.sources.types import RdfSource


@dataclass(frozen=True)
class CorpusRecord:
    """One immutable RDF resource with explicit named model and key bindings.

    Identifiers are stable application identities, never array positions. Blank
    subjects and blank graph selectors require a separate portable identity
    contract and are outside this restart profile. Source graph pins remain
    authoritative; graph_name/union follow ordinary RDF model selection rules.
    """

    identifier: str
    source: RdfSource
    usages: tuple[RdfModelUsage, ...]
    key_subject: str | None = None
    graph_name: str | None = None
    union: bool = False


@dataclass(frozen=True)
class CorpusRecordResult:
    """An actual completed or pending outcome beside its durability receipt."""

    identifier: str
    identity: str
    report: Report
    reused: bool
    durable: bool


@dataclass(frozen=True)
class CorpusResult:
    """Required aggregate, semantic outcomes and checkpoint integrity separately.

    A fresh recheck can establish semantic truth despite a failed checkpoint.
    The aggregate retains required checkpoint uncertainty; cached negatives
    remain complete negative outcomes. Global obligations are always fresh.
    """

    report: Report
    semantic_report: Report
    checkpoint_report: Report
    records: tuple[CorpusRecordResult, ...]
    pending: tuple[str, ...]
    manifest_complete: bool
    global_report: Report | None
    revision: str
    identity: PreparationIdentity
