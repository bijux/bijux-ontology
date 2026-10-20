"""Explicit model scopes and caller-owned JSON representatives."""

import json
from dataclasses import KW_ONLY, dataclass
from dataclasses import field as dataclass_field

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.json.values import decode_json, own_json


@dataclass(frozen=True, init=False)
class ProbeSample:
    """One raw JSON input shape for a named field's alternative value branch.

    Values are owned at construction and returned independently. A sample is
    application input, not an inferred biological value or serializer promise.
    """

    field: str
    branch: int
    _payload: str = dataclass_field(repr=False)

    def __init__(self, field: str, branch: int, value: object) -> None:
        if not isinstance(field, str) or not field.strip():
            raise InputError("A probe sample requires a declared field name")
        if type(branch) is not int or branch < 0:
            raise InputError("A probe branch must be a nonnegative integer")
        payload = json.dumps(own_json(value), ensure_ascii=False, allow_nan=False)
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "_payload", payload)

    @property
    def value(self) -> object:
        """Return an independent copy of the application-authored JSON sample."""
        return json.loads(self._payload)

    def _read(self, limits: Limits) -> object:
        """Apply the current operation's smaller bounds before decoding a copy."""
        return decode_json(
            self._payload,
            max_bytes=limits.max_bytes,
            max_depth=limits.max_depth,
        )


@dataclass(frozen=True)
class ContextProbe:
    """Qualify every declared branch in one explicitly selected model/type scope.

    type_values are actual compact @type inputs and must expand to model.types.
    No type-scoped context is selected by inspecting raw context grammar. Supply
    separate probes when a model must be exercised under different type terms.
    """

    model: ModelDeclaration
    _: KW_ONLY
    type_values: tuple[str, ...] = ()
    samples: tuple[ProbeSample, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.model, ModelDeclaration):
            raise InputError("A context probe requires a ModelDeclaration")
        if not isinstance(self.type_values, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.type_values
        ):
            raise InputError("Probe type_values must be a tuple of compact type terms")
        if self.type_values and not self.model.types:
            raise InputError("Selected type terms require expected model types")
        if not isinstance(self.samples, tuple) or any(
            not isinstance(sample, ProbeSample) for sample in self.samples
        ):
            raise InputError("Expected a tuple of explicit ProbeSample values")
        fields = {item.name: item for item in self.model.fields}
        seen = set()
        for sample in self.samples:
            if sample.field not in fields or sample.branch >= len(
                fields[sample.field].values
            ):
                raise InputError("Probe sample references an undeclared field branch")
            identity = sample.field, sample.branch
            if identity in seen:
                raise InputError("A field branch permits one explicit JSON sample")
            seen.add(identity)
