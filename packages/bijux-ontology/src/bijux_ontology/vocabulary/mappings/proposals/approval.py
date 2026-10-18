"""Explicit reviewed rows through maintained SSSOM parsing and serialization."""

import csv
import json
import time
from collections.abc import Sequence
from io import StringIO
from threading import Event
from typing import cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from ..interchange import read_sssom
from ..types import MappingRead, _iri
from .types import MappingApproval, MappingProposals

_IDENTITY = ("subject_id", "predicate_id", "object_id", "mapping_justification")


def approve_mapping_proposals(
    proposals: MappingProposals,
    approvals: Sequence[MappingApproval],
    *,
    registry: IdentifierRegistry,
    mapping_set_id: str,
    license: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> MappingRead:
    """Author only explicitly approved occurrences, with no inferred predicate.

    Every approval must reference an actual proposal in this completed result.
    Predicate, justification and qualifications are mandatory caller choices.
    The explicit registry must round-trip each endpoint, predicate and
    justification; no global prefix lookup or identity rewrite is performed.
    Exact approved values are checked again after native SSSOM processing.

    Host construction, parsing and adoption share the selected budget. Maintained
    worker internals retain the interchange API's independent process limits and
    enclosing deadline. Interchange success does not certify endpoint membership
    or semantic truth: use check_mappings with the intended consumer policy.
    """
    if not isinstance(proposals, MappingProposals) or not isinstance(
        registry, IdentifierRegistry
    ):
        raise InputError("Approval requires owned proposals and an explicit registry")
    operation = Operation(
        limits or (proposals.limits if budget is None else None),
        budget,
        timeout,
        cancel,
    )
    issues = IssueCollector(operation)
    children: list[Report] = []
    result = None
    checked = 0
    native = None
    try:
        retain_report(proposals.report, operation, issues)
        children.append(proposals.report)
        if not proposals.report.complete or proposals.report.outcome == "exhausted":
            raise InputError("Incomplete proposal generation cannot authorize mappings")
        if (
            isinstance(cast(object, approvals), str | bytes)
            or not isinstance(approvals, Sequence)
            or not approvals
        ):
            raise InputError("Approval requires a nonempty finite sequence")
        identifier, licensing = (
            _iri(mapping_set_id, operation),
            _iri(license, operation),
        )
        operation.consume("states", len(proposals.items) + len(approvals))
        operation.consume("bytes", 128 + 96 * (len(proposals.items) + len(approvals)))
        operation.consume("work", len(proposals.items))
        identities = {id(item) for item in proposals.items}
        operation.consume("states", len(registry.bindings))
        prefixes = {}
        for prefix, namespace in registry.bindings:
            bound_text(prefix, operation)
            _iri(namespace, operation)
            prefixes[prefix] = namespace
        columns = list(_IDENTITY)
        rows: list[dict[str, str]] = []
        expected: list[tuple[str, ...]] = []
        for position in range(len(approvals)):
            operation.consume("work")
            approval = approvals[position]
            if (
                not isinstance(approval, MappingApproval)
                or id(approval.proposal) not in identities
            ):
                raise InputError(
                    "Approve an actual occurrence from this proposal result"
                )
            proposal = approval.proposal
            values = tuple(
                _iri(value, operation)
                for value in (
                    proposal.source.term.iri,
                    approval.predicate,
                    proposal.target.term.iri,
                    approval.justification,
                )
            )
            operation.consume("states", 8)
            operation.consume("bytes", 384)
            row = {}
            for name, iri in zip(_IDENTITY, values, strict=True):
                operation.consume("work", (len(iri) + 1) * (len(registry.bindings) + 1))
                operation.consume("bytes", 64 + 4 * len(iri))
                compact = registry.compact_iri(iri)
                if compact == iri or registry.expand_curie(compact) != iri:
                    raise InputError(
                        "A mapping identifier needs an exact explicit CURIE binding"
                    )
                row[name] = compact
            qualifications = approval.qualifications
            if not isinstance(qualifications, tuple):
                raise InputError("Mapping qualifications require explicit finite pairs")
            operation.consume("states", 2 * len(qualifications))
            operation.consume("bytes", 128 * len(qualifications))
            for pair in qualifications:
                operation.consume("work")
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise InputError(
                        "Mapping qualifications require textual name/value pairs"
                    )
                name, value = pair
                if type(name) is not str or type(value) is not str:
                    raise InputError(
                        "Mapping qualifications require textual name/value pairs"
                    )
                bound_text(name, operation)
                bound_text(value, operation)
                if not name or name in row or not name.isidentifier():
                    raise InputError(
                        "Duplicate, reserved or invalid mapping qualification"
                    )
                row[name] = value
                operation.consume("work", (len(name) + 1) * (len(columns) + 1))
                if name not in columns:
                    operation.consume("states")
                    operation.consume("bytes", 64)
                    columns.append(name)
            rows.append(row)
            expected.append(values)
        # JSON flow scalars/maps are YAML, the maintained SSSOM metadata format.
        # Reserve escaped framing and the complete rectangular CSV before writing.
        operation.consume("work", len(rows) * len(columns))
        operation.consume("states", len(rows) * len(columns))
        estimate = 4096 + 12 * (
            len(identifier)
            + len(licensing)
            + sum(len(k) + len(v) for k, v in prefixes.items())
            + sum(map(len, columns))
            + sum(sum(len(v) for v in row.values()) for row in rows)
            + len(rows) * len(columns)
        )
        operation.consume("bytes", estimate)
        operation.consume("work", estimate)
        stream = StringIO()
        for name, metadata_value in (
            ("mapping_set_id", identifier),
            ("license", licensing),
            ("curie_map", prefixes),
        ):
            stream.write(
                "# "
                + name
                + ": "
                + json.dumps(metadata_value, ensure_ascii=True)
                + "\n"
            )
        writer = csv.DictWriter(
            stream, fieldnames=columns, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        content = stream.getvalue().encode("utf-8")
        if len(content) > operation.limits.max_bytes:
            raise LimitExceeded("Approved mapping source exceeds max_bytes")
        native = read_sssom(
            content,
            mapping_set_id=identifier,
            license=licensing,
            budget=operation,
            timeout=max(0.001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        retain_report(native.report, operation, issues)
        children.append(native.report)
        if native.mapping_set is not None and native.report.verified:
            mapping = native.mapping_set
            if len(mapping.rows) != len(expected):
                raise InputError(
                    "Native mapping rows do not cover the approved occurrences"
                )
            for mapping_row, wanted in zip(mapping.rows, expected, strict=True):
                operation.consume("work", sum(map(len, wanted)))
                if (
                    mapping_row.subject,
                    mapping_row.predicate,
                    mapping_row.object,
                    mapping_row.justification,
                ) != wanted:
                    raise InputError(
                        "Native mapping identifiers differ from explicit approval"
                    )
                checked += 1
            result = mapping
    except LimitExceeded as error:
        if native is not None:
            issues.retain_completed(native.report)
        issues.exhausted(str(error), location="mapping.proposals.approve")
    except InputError:
        try:
            issues.append(
                Issue(
                    "mapping.proposal_invalid",
                    "Explicit mapping approval could not qualify",
                    kind="input_error",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="mapping.proposals.approve")
    return MappingRead(
        result,
        Report(
            "mapping.proposals.approve",
            checked,
            tuple(issues),
            children=tuple(children),
            requested=len(approvals) if isinstance(approvals, Sequence) else 1,
            complete=result is not None,
            regime="explicit-reviewed-native-sssom",
        ),
    )
