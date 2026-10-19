"""Owned finite membership evidence, with explicit open and closed boundaries."""

from dataclasses import KW_ONLY, dataclass

from rdflib import Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import term_key


@dataclass(frozen=True)
class ValueEnumeration:
    """Known permitted/excluded terms; absence excludes only a qualified closed set."""

    values: tuple[URIRef | Literal, ...]
    _: KW_ONLY
    excluded: tuple[URIRef | Literal, ...] = ()
    closed: bool = True
    report: Report | None = None

    def __post_init__(self) -> None:
        """Require immutable finite terms and preserve the original scope report."""
        budget = Budget()
        keys = []
        for terms in (self.values, self.excluded):
            if type(terms) is not tuple or len(terms) > Limits().max_states:
                raise InputError("Enumeration evidence needs bounded term tuples")
            selected = set()
            for term in terms:
                budget.consume("work")
                budget.consume("states")
                if not isinstance(term, URIRef | Literal):
                    raise InputError(
                        "Enumeration evidence requires named IRIs or literals"
                    )
                texts: tuple[str, ...] = (str(term),)
                if isinstance(term, Literal):
                    texts += (str(term.datatype or ""), term.language or "")
                for text in texts:
                    if len(text) > budget.limits.max_bytes:
                        raise LimitExceeded("Enumeration term exceeds max_bytes")
                    try:
                        size = len(text.encode("utf-8"))
                    except UnicodeEncodeError as error:
                        raise InputError(
                            "Enumeration terms require valid Unicode"
                        ) from error
                    if size > budget.limits.max_bytes:
                        raise LimitExceeded("Enumeration term exceeds max_bytes")
                    budget.consume("bytes", size)
                if isinstance(term, URIRef):
                    absolute_iri(str(term))
                selected.add(term_key(term))
            keys.append(selected)
        if keys[0] & keys[1]:
            raise InputError("An enumeration cannot both permit and exclude a term")
        if (
            type(self.closed) is not bool
            or self.report is not None
            and not isinstance(self.report, Report)
        ):
            raise InputError("Enumeration closure and report must be explicit")

    @property
    def qualified(self) -> bool:
        """A missing term proves exclusion only under complete closed selection."""
        return self.closed and (self.report is None or self.report.verified)

    def qualification_report(self) -> Report:
        """Keep selection coverage required even when its issues happen to be empty."""
        if (
            self.report is not None
            and not self.report.verified
            and self.report.outcome != "not_applicable"
        ):
            return self.report
        if self.qualified:
            return (
                self.report
                if self.report is not None
                else Report("value.enumeration", 1)
            )
        return Report(
            "value.enumeration",
            1,
            (
                Issue(
                    "emission.value_unverified",
                    "Open or unqualified enumeration cannot establish "
                    "complete allowed-set coverage",
                    kind="unverified",
                ),
            ),
            requested=2,
            complete=False,
            children=(self.report,) if self.report is not None else (),
        )
