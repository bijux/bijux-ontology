"""Owned accepted contexts retain their required finite qualification report."""

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Limits


@dataclass(frozen=True, init=False)
class CompiledContext:
    """Reusable owned context after complete raw representative qualification.

    A failed, incomplete or unsupported compilation never exports a partial
    context. Agreement concerns the selected generated inputs; actual serializer
    output must separately retain this report through its ordinary checking API.
    """

    report: Report
    _payload: str | None = dataclass_field(repr=False)

    def __init__(
        self,
        context: object,
        report: Report,
        *,
        limits: Limits | None = None,
    ) -> None:
        if not isinstance(report, Report):
            raise InputError("A compiled context requires its qualification report")
        payload = None
        if report.verified:
            selected = limits or Limits()
            owned = own_json(
                context,
                max_bytes=selected.max_bytes,
                max_depth=selected.max_depth,
            )
            if not isinstance(owned, dict):
                raise InputError("An accepted compilation requires a context object")
            payload = json.dumps(
                owned, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        object.__setattr__(self, "report", report)
        object.__setattr__(self, "_payload", payload)

    @property
    def context(self) -> object | Absent:
        """Export a fresh accepted context, or ABSENT when qualification failed."""
        return json.loads(self._payload) if self._payload is not None else ABSENT
