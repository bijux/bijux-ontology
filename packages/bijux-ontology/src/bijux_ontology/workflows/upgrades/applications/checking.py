"""Public single-version application checking without a synthetic upgrade baseline."""

from types import MappingProxyType

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.upgrades.uses import UseIndex

from .preparation import _check_version, _preflight
from .types import ApplicationCheck, ApplicationVersion


def check_application(
    vocabulary: Vocabulary | Graph,
    version: ApplicationVersion,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ApplicationCheck:
    """Check one explicit contract bundle and corpus with one shared allowance.

    Each representative serializer executes once. Actual documents, raw child
    reports and owned RDF remain inspectable. The supplied vocabulary determines
    resource scope; no missing historical baseline or universal promise is added.
    """
    if not isinstance(version, ApplicationVersion):
        raise InputError("Expected an explicit ApplicationVersion")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Application limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    try:
        _preflight(version, operation)
        source = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, budget=operation)
        )
        operation.consume("states", len(version.cases) + len(version.outputs))
        result = _check_version(version, source, "application", operation)
        sources = {
            name: item.source
            for name, item in result.prepared.items()
            if item.source is not None
        }
        return ApplicationCheck(
            version,
            result.report,
            MappingProxyType(dict(result.parts)),
            MappingProxyType(dict(result.samples)),
            MappingProxyType(sources),
            result.uses,
        )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="application")
        return ApplicationCheck(
            version,
            Report("application", issues=tuple(issues), requested=1, complete=False),
            {},
            {},
            {},
            UseIndex((), Report("application.uses", allow_empty=True)),
        )
