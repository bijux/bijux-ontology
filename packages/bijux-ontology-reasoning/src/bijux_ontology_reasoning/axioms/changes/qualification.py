"""Shared qualification of native edit losses and operation failures."""

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import IssueCollector
from bijux_ontology.reports import Issue

from ..codec.decoding import ProjectionDecoder


def retain_losses(
    raw: object, decoder: ProjectionDecoder, issues: IssueCollector
) -> None:
    """Keep original-input and reread interpretation losses distinct from edits."""
    for item in decoder.records(raw):
        record = object_value(item, location="native change loss")
        kind, value = (
            decoder.text(record.get("kind")),
            decoder.text(record.get("value")),
        )
        issues.append(
            Issue(
                "owl.axiom_change_loss",
                "Native input or output has an unaccounted structural difference",
                expected=kind,
                actual=value,
                kind="unverified",
            )
        )


def retain_error(
    issues: IssueCollector, error: InputError | OSError, location: str
) -> None:
    """Preserve ordinary input/unknown failures and paid-prefix exhaustion."""
    try:
        issues.append(
            Issue(
                "owl.axiom_change_unverified"
                if isinstance(error, ResourceError | OSError)
                else "owl.axiom_change_input_error",
                str(error),
                location,
                kind="unverified"
                if isinstance(error, ResourceError | OSError)
                else "input_error",
            )
        )
    except LimitExceeded as stopped:
        issues.exhausted(str(stopped), location=location)
