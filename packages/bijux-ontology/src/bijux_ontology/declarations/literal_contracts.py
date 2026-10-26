"""Immutable, explicitly selected obligations over opaque field literals."""

from dataclasses import InitVar, dataclass

from rdflib import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.values import decode_json, encode_json
from bijux_ontology.rdf.literal_values.json.checking import _requirements
from bijux_ontology.rdf.literal_values.json.types import (
    JsonLiteralContract,
    JsonRequirement,
)
from bijux_ontology.rdf.literal_values.markup.types import MarkupCanonicalization
from bijux_ontology.rdf.terms import bound_node


@dataclass(frozen=True, init=False)
class JsonLiteralObligation:
    """Own an existing JSON contract without retaining its mutable schema alias.

    Schemas stay inert JSON text. A fresh bounded schema is decoded only when
    invoking the existing JSON literal evaluator; no executable state is stored.
    """

    requirements: tuple[JsonRequirement, ...]
    schema_json: str | None
    dialect: str | None

    def __init__(
        self, contract: JsonLiteralContract, *, budget: Budget | None = None
    ) -> None:
        if not isinstance(contract, JsonLiteralContract):
            raise InputError("Expected an explicit JsonLiteralContract")
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("JSON literal obligation admission requires a Budget")
        allowance = budget if budget is not None else Budget()
        required = _requirements(contract, allowance)
        if contract.dialect is not None:
            bound_text(contract.dialect, allowance)
        schema = (
            encode_json(contract.schema, budget=allowance)
            if contract.schema is not None
            else None
        )
        allowance.consume("states")
        allowance.consume("bytes", 128)
        object.__setattr__(
            self, "requirements", tuple(item.requirement for item in required)
        )
        object.__setattr__(self, "schema_json", schema)
        object.__setattr__(self, "dialect", contract.dialect)

    def contract(self, budget: Budget) -> JsonLiteralContract:
        """Supply a fresh actual evaluator input under its operation allowance."""
        schema = (
            decode_json(
                self.schema_json,
                max_bytes=budget.limits.max_bytes,
                max_depth=budget.limits.max_depth,
                budget=budget,
            )
            if self.schema_json is not None
            else None
        )
        return JsonLiteralContract(self.requirements, schema, self.dialect)


@dataclass(frozen=True)
class MarkupLiteralContract:
    """Select maintained fragment inspection and optional finite XML comparison."""

    canonicalization: MarkupCanonicalization | None = None
    expected: Literal | None = None
    budget: InitVar[Budget | None] = None

    def __post_init__(self, budget: Budget | None) -> None:
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Markup contract admission requires a Budget")
        if self.canonicalization not in (None, "xml-exc-c14n-with-comments"):
            raise InputError("Unsupported markup canonicalization contract")
        if self.expected is not None:
            if not isinstance(self.expected, Literal) or self.canonicalization is None:
                raise InputError(
                    "Markup comparison needs a literal and selected canonicalization"
                )
            bound_node(self.expected, budget if budget is not None else Budget())
