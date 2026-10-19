"""Shared native output projection state, references and one loss recorder."""

from typing import Literal as Status
from urllib.parse import quote

from bijux_ontology.declarations.types import (
    ModelDeclaration,
)
from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
)
from bijux_ontology.interfaces.adapters.jsonschema.types import SchemaModel
from bijux_ontology.json.paths import bound_pointer_text

from ..types import SchemaTranslation


class SchemaProjection:
    """One operation's qualified representation choices and ordered loss inventory."""

    def __init__(self, budget: Budget) -> None:
        """Retain only bounded declarations, local definitions and translation facts."""
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.items: list[SchemaTranslation] = []
        self.children: list[Report] = []
        self.retained: set[int] = set()
        self.models: dict[str, ModelDeclaration] = {}
        self.representations: dict[tuple[str, str], str] = {}
        self.definitions: dict[str, object] = {}
        self.annotations: dict[str, object] = {}
        self.mappings: list[SchemaModel] = []
        self.labels: dict[str, str] = {}
        self.iri = ""
        self.profile = ""

    def record(
        self,
        model: str,
        field: str | None,
        obligation: str,
        detail: str,
        *,
        supported: bool = True,
        status: Status["preserved", "strengthened", "weakened", "unsupported"]
        | None = None,
    ) -> None:
        """Retain each explicit obligation under the same operation's quotas."""
        self.budget.consume("states")
        for text in (model, field or "", obligation, detail):
            bound_text(text, self.budget)
        selected_status = status or ("preserved" if supported else "unsupported")
        self.items.append(
            SchemaTranslation(
                model,
                field,
                obligation,
                selected_status,
                detail,
            )
        )
        if selected_status != "preserved":
            self.issues.append(
                Issue("schema.translation_unverified", detail, model, kind="unverified")
            )

    def reference(self, identifier: str) -> str:
        """Use a bounded JSON Pointer component in the owned schema resource."""
        size = bound_pointer_text(identifier, self.budget)
        if 3 * size + 10 > self.budget.limits.max_bytes:
            raise LimitExceeded("Schema reference exceeds max_bytes")
        self.budget.consume("bytes", 3 * size + 10)
        return "#/$defs/" + quote(
            identifier.replace("~", "~0").replace("/", "~1"), safe=""
        )
