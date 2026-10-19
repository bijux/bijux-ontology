"""Bounded supplied bytes and existing portable bundle-member acquisition."""

import hashlib
import json
from collections.abc import Sequence
from importlib.resources.abc import Traversable
from pathlib import Path

from bijux_ontology._json import _decode_json, own_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.archive import bundle_reader, resource_path
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.vocabulary.accessions.types import AccessionRule
from bijux_ontology.vocabulary.registry.types import _prefix

from .translation import qualify_override, translate
from .types import BioregistryOverride, BioregistrySnapshot, _text


def read_bioregistry(
    payload: bytes,
    *,
    revision: str,
    sha256: str,
    prefixes: Sequence[str],
    overrides: Sequence[BioregistryOverride] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> BioregistrySnapshot:
    """Read an explicitly selected subset with byte-pinned local policy.

    Top-level terminal-$1 URI formats and permitted aliases have exact namespace
    semantics. Arbitrary source patterns, nested imported-registry precedence,
    redundant-prefix normalization and provider templates are not translated.
    Existing AccessionRule overrides state a separate application policy, never
    equivalence to those source rules. All selected source fields remain intact.
    No network lookup, RDF declaration or identifier-existence claim occurs.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Snapshot limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    owned_payload = b""
    owned_revision = owned_sha = ""
    selected: list[str] = []
    local: list[BioregistryOverride] = []
    records_json = "{}"
    unsupported: list[tuple[str, str]] = []
    uninterpreted: list[tuple[str, str]] = []
    registry = None
    rules: tuple[AccessionRule, ...] = ()
    requested = 1
    checked = 0
    try:
        owned_revision = _text(revision, operation)
        owned_sha = _text(sha256, operation)
        if not owned_revision.strip():
            raise InputError("A snapshot requires an explicit revision label")
        if len(owned_sha) != 64 or any(
            char not in "0123456789abcdef" for char in owned_sha
        ):
            raise InputError("Snapshot SHA256 requires 64 lowercase hexadecimal digits")
        if type(payload) is not bytes:
            raise InputError("A snapshot requires exact supplied bytes")
        if len(payload) > operation.limits.max_bytes:
            raise LimitExceeded("Snapshot exceeds max_bytes")
        operation.consume("resources")
        # Decoder, owned JSON copy and selected-record encoding each traverse
        # bounded input; reserve their byte/work capacity before those stages.
        operation.consume("work", 4 * len(payload) + 1)
        operation.consume("bytes", 5 * len(payload))
        if hashlib.sha256(payload).hexdigest() != owned_sha:
            raise InputError("Supplied snapshot does not match its expected SHA256")
        owned_payload = payload
        values = capture_items(prefixes, str, operation)
        requested = len(values)
        if not values:
            raise InputError("Select at least one source prefix")
        seen: set[str] = set()
        for value in values:
            prefix = _prefix(_text(value, operation))
            if prefix in seen:
                raise InputError("Source prefixes must be distinct")
            seen.add(prefix)
            selected.append(prefix)
        by_prefix = {}
        for override in capture_items(overrides, BioregistryOverride, operation):
            captured = qualify_override(override, operation)
            if captured.prefix not in seen or captured.prefix in by_prefix:
                raise InputError(
                    "Local overrides require distinct selected source prefixes"
                )
            by_prefix[captured.prefix] = captured
            local.append(captured)
        decoded = _decode_json(
            payload,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
            budget=operation,
        )
        if not isinstance(decoded, dict):
            raise InputError("A Bioregistry snapshot requires a prefix-keyed object")
        records: dict[str, dict[str, object]] = {}
        for prefix in selected:
            operation.consume("work")
            record = decoded.get(prefix)
            if not isinstance(record, dict):
                raise InputError("Every selected prefix requires an object record")
            records[prefix] = record
        # The shared JSON boundary computes compact UTF-8 size before its copy;
        # the following maintained encoder emits that same bounded representation.
        own_json(
            records,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        records_json = json.dumps(
            records, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        registry, rules = translate(
            records, by_prefix, operation, issues, unsupported, uninterpreted
        )
        checked = len(records)
    except LimitExceeded:
        issues.exhausted("Stopped at the configured snapshot preparation bound")
        registry, rules = None, ()
    except (InputError, IndexError, ValueError, UnicodeError):
        registry, rules = None, ()
        try:
            issues.append(
                Issue(
                    "adapter.bioregistry_input_error",
                    "Snapshot bytes, selected records or local policies "
                    "are invalid or conflicting",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining a snapshot input failure")
    report = Report(
        "adapter.bioregistry",
        checked=checked,
        requested=requested,
        issues=tuple(issues),
        regime="supplied-bioregistry-local-policy",
        complete=not any(issue.kind in ("unverified", "exhausted") for issue in issues),
    )
    return BioregistrySnapshot._from_owned(
        payload=owned_payload,
        revision=owned_revision,
        sha256=owned_sha,
        prefixes=tuple(selected),
        registry=registry,
        rules=rules,
        overrides=tuple(local),
        unsupported=tuple(unsupported),
        uninterpreted=tuple(uninterpreted),
        report=report,
        records_json=records_json,
    )


def read_bioregistry_bundle(
    source: Path | Traversable,
    member: str,
    *,
    revision: str,
    sha256: str,
    prefixes: Sequence[str],
    overrides: Sequence[BioregistryOverride] = (),
    limits: Limits | None = None,
) -> BioregistrySnapshot:
    """Read one explicit portable member using the existing offline bundle reader.

    Bundle metadata/member acquisition has its ordinary bounded-reader limits;
    snapshot interpretation uses its own limits. This is not a cumulative bundle
    plus parser budget or a new executable project configuration dialect.
    """
    operation = Budget(limits)
    member = _text(member, operation)
    resource_path(member)
    try:
        with bundle_reader(source, limits=operation.limits) as read:
            payload = read(member)
    except (InputError, ResourceError, LimitExceeded):
        raise
    return read_bioregistry(
        payload,
        revision=revision,
        sha256=sha256,
        prefixes=prefixes,
        overrides=overrides,
        limits=operation.limits,
    )
