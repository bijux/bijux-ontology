"""Explicit SKOS-XL label-resource views without collapsing equal literal forms."""

from dataclasses import dataclass

from rdflib import BNode, Namespace, URIRef

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection

XL = Namespace("http://www.w3.org/2008/05/skos-xl#")
ROLES = (XL.prefLabel, XL.altLabel, XL.hiddenLabel)


@dataclass(frozen=True)
class XLLabel:
    """One resource keeps every form, source assertion and supplied label link."""

    resource: URIRef | BNode
    forms: tuple[Edge, ...]
    metadata: tuple[Edge, ...]
    link: Edge | None = None


def describe_xl_label(
    view: VocabularyView, resource: URIRef | BNode, *, budget: Budget | None = None
) -> Selection[XLLabel]:
    """Describe a label independently, retaining missing-form uncertainty."""
    operation = budget if budget is not None else Budget(view.limits)
    selected = view.statements(resource, budget=operation)
    issues = IssueCollector(operation)
    list.extend(issues, selected.report.issues)
    forms = tuple(edge for edge in selected.items if edge.predicate == XL.literalForm)
    complete = selected.report.complete
    try:
        if not forms and complete:
            issues.append(
                Issue(
                    "skos.xl_form_unresolved",
                    "XL label lacks a supplied literal form",
                    str(resource),
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location=str(resource))
        complete = False
    return Selection(
        (XLLabel(resource, forms, selected.items),),
        Report(
            "skos.xl_label",
            1,
            tuple(issues),
            complete=complete,
            regime="asserted",
            families=("skos.xl",),
        ),
    )


def xl_labels(
    view: VocabularyView, owner: URIRef | BNode, *, budget: Budget | None = None
) -> Selection[XLLabel]:
    """Follow only explicit XL label roles; equal forms never merge label resources."""
    operation = budget if budget is not None else Budget(view.limits)
    links = view.statements(owner, predicates=tuple(map(str, ROLES)), budget=operation)
    issues = IssueCollector(operation)
    list.extend(issues, links.report.issues)
    labels: list[XLLabel] = []
    complete = links.report.complete
    try:
        for link in links.items:
            operation.consume("work")
            if not isinstance(link.object, URIRef | BNode):
                issues.append(
                    Issue(
                        "skos.policy.xl_label_shape",
                        "XL projection requires a resource label",
                        str(owner),
                    )
                )
                continue
            selected = describe_xl_label(view, link.object, budget=operation)
            list.extend(issues, selected.report.issues)
            complete = complete and selected.report.complete
            value = selected.items[0]
            labels.append(XLLabel(value.resource, value.forms, value.metadata, link))
    except LimitExceeded as error:
        issues.exhausted(str(error), location=str(owner))
        complete = False
    return Selection(
        tuple(labels),
        Report(
            "skos.xl_labels",
            len(links.items),
            tuple(issues),
            allow_empty=True,
            complete=complete,
            requested=len(links.items) if complete else len(links.items) + 1,
            regime="asserted",
            families=("skos.xl",),
        ),
    )
