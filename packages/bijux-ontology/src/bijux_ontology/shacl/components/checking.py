"""Isolated maintained execution of qualified authored component uses."""

import threading
import time
from typing import cast

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.execution.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.execution.session import ShaclSession
from bijux_ontology.shacl.profiles.comparison import _coverage
from bijux_ontology.shacl.profiles.resources import _capture
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract

from .types import ComponentCheck, ComponentShapes


def check_components(
    data: PreparedSource,
    components: ComponentShapes,
    *,
    trusted_sparql: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ComponentCheck:
    """Run actual authored uses under explicit trust without enabling advanced mode.

    Construction and backend preparation have their existing finite limits; this
    wrapper shares the operation budget with request execution/report retention.
    The opaque maintained engine is isolated under the remaining real deadline.
    """
    if not isinstance(data, PreparedSource) or not isinstance(
        components, ComponentShapes
    ):
        raise InputError(
            "Component checks require owned prepared data and compiled shapes"
        )
    if type(trusted_sparql) is not bool:
        raise InputError("Component SPARQL trust requires an explicit boolean")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    result = None
    children: list[Report] = []
    try:
        retain_report(components.report, operation, issues)
        children.append(components.report)
        if components.source is None or not components.report.verified:
            issues.append(
                Issue(
                    "shacl.component_unverified",
                    "Execution requires qualified compiled components",
                    kind="unverified",
                )
            )
        else:
            roots = cast(
                tuple[URIRef | BNode, ...], _capture(components.roots, operation)
            )
            requirements = cast(
                tuple[ShaclConstraintRequirement, ...],
                _capture(components.requirements, operation),
            )
            for root in roots:
                if not isinstance(root, URIRef | BNode):
                    raise InputError("Compiled roots require RDF shape resources")
                bound_node(root, operation)
            for requirement in requirements:
                if not isinstance(requirement, ShaclConstraintRequirement):
                    raise InputError(
                        "Compiled requirements need typed parameter declarations"
                    )
                for metadata in (
                    requirement.shape,
                    requirement.parameter,
                    requirement.component,
                ):
                    if metadata is not None:
                        bound_text(metadata, operation)
            if data.graph_name is not None:
                bound_node(data.graph_name, operation) if isinstance(
                    data.graph_name, URIRef | BNode
                ) else bound_text(data.graph_name, operation)
            operation.consume("resources")
            operation.consume("work")
            remaining = operation.deadline - time.monotonic()
            if remaining <= 0:
                raise LimitExceeded("Component check exceeded its deadline")
            with ShaclSession(
                components.source,
                use_shapes=roots,
                trusted_sparql=trusted_sparql,
                required_constraints=requirements,
                coverage=ShaclCoverageContract(),
                preflight=True,
                limits=operation.limits,
                timeout=remaining,
            ) as session:
                operation.consume("work")
                remaining = operation.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded("Component check exceeded its deadline")
                # Existing constructor timeout is request-relative; tighten it
                # after preparation so total preparation+execution stays bounded.
                session._timeout = remaining
                pending = session._validate(
                    data,
                    focus_nodes=(),
                    graph_name=None,
                    union=False,
                    source_usages=(),
                    scope="dataset",
                    cancel=cancel,
                    budget=operation,
                )
            retain_report(pending.report, operation, issues)
            operation.consume("states", len(pending.findings))
            result = pending
            children.append(result.report)
            if set(roots) - _coverage(result, roots, operation):
                issues.append(
                    Issue(
                        "shacl.component_unverified",
                        "Every authored use requires completed selected root coverage",
                        kind="unverified",
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ResourceError) as error:
        try:
            issues.append(
                Issue("shacl.component_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "shacl-component-check",
        int(result is not None),
        tuple(issues),
        children=tuple(children),
        requested=1,
        regime="explicit-trusted-component-execution",
    )
    return ComponentCheck(result, report)
