"""Public semantic queries sharing one bounded native session lifetime."""

from collections.abc import Sequence
from threading import Event

from bijux_ontology.limits import Budget, Limits

from ...axioms.types import ClassExpression, ProjectedAxiom
from ..classes.types import ClassQueryResult
from ..entailment.types import EntailmentCheck
from ..individuals.types import IndividualQuery, IndividualRealization
from ..relationships.types import InferredContracts, InferredQuery
from .runtime import NativeSession
from .types import InferenceProjection, InferenceSelection, PreparedReasoningTheory


class ReasoningSession(NativeSession):
    """Recompute selected semantic answers within one fixed owned native theory."""

    def check_entailments(
        self,
        obligations: Sequence[ProjectedAxiom],
        *,
        theory: PreparedReasoningTheory | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> EntailmentCheck:
        """Recompute finite obligations over the current unchanged classification."""
        from ..entailment.session import check_entailments

        return check_entailments(
            self,
            obligations,
            theory=theory,
            limits=limits,
            budget=budget,
            timeout=timeout,
            cancel=cancel,
        )

    def query_inferred_contracts(
        self,
        queries: Sequence[InferredQuery],
        *,
        theory: PreparedReasoningTheory | None = None,
        max_results: int = 10_000,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> InferredContracts:
        """Reuse native named relationships and exact original assertion owners."""
        from ..relationships.session import query_inferred_contracts

        return query_inferred_contracts(
            self,
            queries,
            max_results=max_results,
            theory=theory,
            limits=limits,
            budget=budget,
            timeout=timeout,
            cancel=cancel,
        )

    def realize(
        self,
        queries: Sequence[IndividualQuery],
        *,
        max_results: int = 10_000,
        theory: PreparedReasoningTheory | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> IndividualRealization:
        """Retrieve independently qualified named individual consequences."""
        from ..individuals.checking import realize

        return realize(
            self,
            queries,
            max_results=max_results,
            theory=theory,
            limits=limits,
            budget=budget,
            timeout=timeout,
            cancel=cancel,
        )

    def query_classes(
        self,
        expression: ClassExpression,
        *,
        direct: bool = False,
        max_results: int = 10_000,
        theory: PreparedReasoningTheory | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> ClassQueryResult:
        """Retrieve actual named class groups for a supported typed expression."""
        from ..classes.checking import query_classes

        return query_classes(
            self,
            expression,
            direct=direct,
            max_results=max_results,
            theory=theory,
            limits=limits,
            budget=budget,
            timeout=timeout,
            cancel=cancel,
        )

    def project(
        self,
        selection: InferenceSelection,
        *,
        theory: PreparedReasoningTheory | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> InferenceProjection:
        """Export only selected entailed finite axioms into a separate RDF artifact."""
        from .projection import project_inferences

        operation = self._operation(limits, budget, timeout, cancel)
        return project_inferences(self, selection, expected=theory, operation=operation)
