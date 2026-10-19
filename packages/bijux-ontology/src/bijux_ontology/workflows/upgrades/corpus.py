"""Representative context-release evidence with explicit exercised-branch coverage."""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.paths import SourcePath
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.resources import DocumentLoader, context_resources
from bijux_ontology.jsonld.usages.checking import PreparedUsages, prepare_usages
from bijux_ontology.jsonld.usages.comparison import compare_context_usages
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.snapshots import PreparedSource

from .assessment.contracts import _contract_changes
from .assessment.types import ContractChange


@dataclass(frozen=True)
class ContextCorpusDocument:
    """One named representative output and the same required contract on both sides.

    ``before=ABSENT`` means no baseline was supplied. ``after=ABSENT`` reuses the
    supplied baseline output with the candidate context resources. JSON null is
    a real supplied value, independent of those absence markers.
    """

    name: str
    before: object
    expectations: tuple[ContextExpectation, ...]
    after: object = ABSENT
    base: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("Corpus documents require nonempty names")
        expectations = tuple(self.expectations)
        if any(not isinstance(item, ContextExpectation) for item in expectations):
            raise InputError("Expected typed context expectations")
        if self.base is not None:
            absolute_iri(self.base)
        object.__setattr__(self, "expectations", expectations)


@dataclass(frozen=True)
class CorpusWitness:
    """A concrete selected usage proposed to exercise one consumer branch."""

    document: str
    path: SourcePath

    def __post_init__(self) -> None:
        if not isinstance(self.document, str) or not self.document.strip():
            raise InputError("A coverage witness requires a document name")
        ContextExpectation(self.path, "@id")


@dataclass(frozen=True)
class CorpusBranch:
    """Caller-selected branch coverage measured by distinct actual usage pairs."""

    name: str
    witnesses: tuple[CorpusWitness, ...]
    minimum: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("Corpus branches require nonempty names")
        if type(self.minimum) is not int or self.minimum < 1:
            raise InputError("Coverage minimum must be a positive integer")
        witnesses = tuple(self.witnesses)
        if any(not isinstance(item, CorpusWitness) for item in witnesses):
            raise InputError("Expected typed corpus witnesses")
        object.__setattr__(self, "witnesses", tuple(dict.fromkeys(witnesses)))


@dataclass(frozen=True)
class CorpusUsageEvidence:
    """Retain the actual old/new usages, including absence or association limits."""

    witness: CorpusWitness
    before: ObservedUsage | None
    after: ObservedUsage | None

    @property
    def exercised(self) -> bool:
        """Require retained nonempty, undropped, unambiguous usages on both sides."""
        return all(
            usage is not None
            and bool(usage.occurrences)
            and not (usage.empty or usage.dropped or usage.limitation)
            for usage in (self.before, self.after)
        )


@dataclass(frozen=True)
class CorpusCoverage:
    """Raw coverage of one required branch, independent of contract agreement."""

    branch: CorpusBranch
    evidence: tuple[CorpusUsageEvidence, ...]

    @property
    def report(self) -> Report:
        """Never certify an unexercised branch from caller labels alone."""
        count = sum(item.exercised for item in self.evidence)
        return Report(
            self.branch.name,
            checked=int(count >= self.branch.minimum),
            requested=1,
            issues=()
            if count >= self.branch.minimum
            else (
                Issue(
                    "upgrade.corpus_coverage_missing",
                    "Required corpus branch was not exercised on both versions",
                    self.branch.name,
                    kind="unverified",
                    expected=str(self.branch.minimum),
                    actual=str(count),
                ),
            ),
            families=("corpus-coverage",),
        )


def _named(report: Report, name: str, *, scope: str | None = None) -> Report:
    version = report.scope if scope is None else scope
    prefix = "/documents/" + name.replace("~", "~0").replace("/", "~1") + "/" + version
    return replace(
        report,
        scope=f"{name}/{report.scope}",
        issues=tuple(
            replace(
                issue,
                location=prefix
                + (
                    issue.location
                    if issue.location.startswith("/")
                    else "/" + issue.location
                    if issue.location
                    else ""
                ),
            )
            for issue in report.issues
        ),
        children=tuple(_named(child, name, scope=version) for child in report.children),
    )


@dataclass(frozen=True)
class ContextDocumentUpgrade:
    """Per-document old/new truth, selected differences and exact dataset evidence."""

    name: str
    before: Report
    after: Report
    usages: Report
    dataset: Report
    changes: tuple[ContractChange, ...]
    evidence: tuple[CorpusUsageEvidence, ...]

    @property
    def report(self) -> Report:
        """Retain baseline failures alongside candidate and preservation results."""
        return combine_reports(
            self.name,
            *(
                _named(report, self.name)
                for report in (self.before, self.after, self.usages, self.dataset)
            ),
        )

    @property
    def demonstrated_break(self) -> bool:
        """A valid baseline plus an observed required failure is a counterexample."""
        return self.before.verified and any(
            issue.kind == "finding" and issue.severity == "error"
            for report in (self.after, self.usages, self.dataset)
            for issue in report.issues
        )


@dataclass(frozen=True)
class ContextCorpusUpgrade:
    """Scoped preservation evidence, distinct from new-contract truth or policy.

    ``compatible`` is true only for complete agreement, false for an observed
    counterexample against a verified baseline, and otherwise unknown. Candidate
    success alone cannot manufacture a missing or previously failing baseline.
    """

    documents: tuple[ContextDocumentUpgrade, ...]
    coverage: tuple[CorpusCoverage, ...]
    report: Report

    @property
    def candidate_report(self) -> Report:
        """Inspect candidate contract/conversion truth independently of the baseline."""
        return combine_reports(
            "corpus.candidate",
            *(_named(item.after, item.name) for item in self.documents),
        )

    @property
    def compatible(self) -> bool | None:
        """State only demonstrated scoped agreement, counterexample, or uncertainty."""
        if self.report.verified:
            return True
        if any(item.demonstrated_break for item in self.documents):
            return False
        return None


@dataclass(frozen=True)
class _Version:
    view: PreparedUsages | None
    source: PreparedSource | None
    report: Report


def _unavailable(
    scope: str, code: str, message: str, *, exhausted: bool = False
) -> Report:
    return Report(
        scope,
        requested=1,
        complete=False,
        issues=(Issue(code, message, kind="exhausted" if exhausted else "unverified"),),
    )


def _snapshot(document: object, scope: str, limits: Limits) -> object:
    if document is ABSENT:
        return _unavailable(
            scope,
            f"upgrade.corpus_{scope}_missing",
            f"No {scope} representative output was supplied",
        )
    try:
        return own_json(
            document, max_bytes=limits.max_bytes, max_depth=limits.max_depth
        )
    except InputError as error:
        return Report(
            scope,
            requested=1,
            complete=False,
            issues=(
                Issue(
                    "execution.limit_exceeded"
                    if isinstance(error, LimitExceeded)
                    else "context.input_error",
                    str(error),
                    kind="exhausted"
                    if isinstance(error, LimitExceeded)
                    else "input_error",
                ),
            ),
        )


def _version(
    document: object,
    member: ContextCorpusDocument,
    scope: str,
    loader: DocumentLoader,
    budget: Budget,
    direction: DirectionPolicy | None,
) -> _Version:
    if isinstance(document, Report):
        return _Version(None, None, document)
    view = None
    try:
        view = prepare_usages(
            document, base=member.base, document_loader=loader, budget=budget
        )
        contracts = view.check(member.expectations)
    except InputError as error:
        contracts = Report(
            "contexts",
            requested=max(1, len(member.expectations)),
            complete=False,
            issues=(
                Issue(
                    "execution.limit_exceeded"
                    if isinstance(error, LimitExceeded)
                    else "context.input_error",
                    str(error),
                    kind="exhausted"
                    if isinstance(error, LimitExceeded)
                    else "input_error",
                ),
            ),
        )
    prepared = prepare_jsonld(
        document,
        base=member.base,
        document_loader=loader,
        budget=budget,
        direction=direction,
    )
    return _Version(
        view,
        prepared.source if prepared.report.verified else None,
        combine_reports(scope, contracts, prepared.report),
    )


def _observe(view: PreparedUsages | None, path: SourcePath) -> ObservedUsage | None:
    if view is None:
        return None
    try:
        return view.observe(path)
    except InputError:
        return None


def assess_context_corpus(
    documents: Sequence[ContextCorpusDocument],
    *,
    before_loader: DocumentLoader | None,
    after_loader: DocumentLoader | None,
    branches: Sequence[CorpusBranch] = (),
    direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
) -> ContextCorpusUpgrade:
    """Run supplied output contracts, usage comparisons and exact RDF preservation.

    Each side converts to one owned dataset; the same datasets feed structural
    comparison without another RDF conversion. Version loaders have independent
    operation snapshots and share one cumulative budget. Coverage applies only
    to the explicitly declared document/path witnesses and representative outputs.
    No acceptance policy or future-data compatibility is inferred.
    """
    selected = limits or Limits()
    if not isinstance(documents, Sequence) or not isinstance(branches, Sequence):
        raise InputError("Corpus documents and branches require explicit sequences")
    if len(documents) + len(branches) > selected.max_states:
        raise LimitExceeded("Corpus declarations exceed max_states")
    corpus = tuple(documents)
    required = tuple(branches)
    if any(not isinstance(item, ContextCorpusDocument) for item in corpus) or any(
        not isinstance(item, CorpusBranch) for item in required
    ):
        raise InputError("Expected typed corpus documents and branches")
    if len({item.name for item in corpus}) != len(corpus) or len(
        {item.name for item in required}
    ) != len(required):
        raise InputError("Corpus document and branch names must be unique")
    budget = Budget(selected)
    old_resources = context_resources(before_loader, budget=budget)
    new_resources = context_resources(after_loader, budget=budget)
    results = []
    evidence: dict[CorpusWitness, CorpusUsageEvidence] = {}
    exhausted = False
    for member in corpus:
        if exhausted:
            missing = _unavailable(
                "pending",
                "execution.limit_exceeded",
                "Corpus execution stopped before this document",
                exhausted=True,
            )
            results.append(
                ContextDocumentUpgrade(
                    member.name, missing, missing, missing, missing, (), ()
                )
            )
            continue
        # Own both supplied outputs before invoking any caller-owned resource loader.
        old = _snapshot(member.before, "baseline", selected)
        new = _snapshot(
            member.before if member.after is ABSENT else member.after,
            "candidate",
            selected,
        )
        previous = _version(old, member, "baseline", old_resources, budget, direction)
        current = _version(new, member, "candidate", new_resources, budget, direction)
        witnesses = tuple(
            dict.fromkeys(
                (
                    *(
                        CorpusWitness(member.name, item.path)
                        for item in member.expectations
                    ),
                    *(
                        witness
                        for branch in required
                        for witness in branch.witnesses
                        if witness.document == member.name
                    ),
                )
            )
        )
        observed = tuple(
            CorpusUsageEvidence(
                witness,
                _observe(previous.view, witness.path),
                _observe(current.view, witness.path),
            )
            for witness in witnesses
        )
        evidence.update((item.witness, item) for item in observed)
        usages = (
            compare_context_usages(
                previous.view,
                current.view,
                tuple(item.path for item in witnesses),
                budget=budget,
            )
            if previous.view is not None and current.view is not None
            else _unavailable(
                "context_changes",
                "upgrade.corpus_comparison_unverified",
                "Both supplied usage preparations are required",
            )
        )
        dataset = (
            compare_rdf(previous.source, current.source, budget=budget)
            if previous.source is not None and current.source is not None
            else _unavailable(
                "rdf_changes",
                "upgrade.corpus_comparison_unverified",
                "Both supplied RDF preparations must complete",
            )
        )
        changes: list[ContractChange] = []
        try:
            _contract_changes(
                member.name, previous.report, current.report, budget, changes
            )
        except LimitExceeded:
            exhausted = True
        if previous.view is None or current.view is None:
            changes = [replace(change, status="uncertain") for change in changes]
        result = ContextDocumentUpgrade(
            member.name,
            previous.report,
            current.report,
            usages,
            dataset,
            tuple(changes),
            observed,
        )
        results.append(result)
        exhausted |= any(issue.kind == "exhausted" for issue in result.report.issues)
    coverage = tuple(
        CorpusCoverage(
            branch,
            tuple(
                evidence.get(witness, CorpusUsageEvidence(witness, None, None))
                for witness in branch.witnesses
            ),
        )
        for branch in required
    )
    report = combine_reports(
        "context_corpus",
        *(result.report for result in results),
        *(item.report for item in coverage),
    )
    if exhausted and not any(issue.kind == "exhausted" for issue in report.issues):
        report = combine_reports(
            "context_corpus",
            report,
            _unavailable(
                "corpus",
                "execution.limit_exceeded",
                "Corpus classification exhausted its budget",
                exhausted=True,
            ),
        )
    return ContextCorpusUpgrade(tuple(results), coverage, report)
