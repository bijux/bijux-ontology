"""Named consumer obligations with explicit coverage and strict acceptance."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from bijux_ontology.declarations.bindings import (
    BindingIdentity,
    BindingObservation,
    ContractEvidence,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import (
    AcceptancePolicy,
    CheckError,
    Issue,
    Report,
    combine_reports,
)


@dataclass(frozen=True)
class ContractObligation:
    """A selected check; only an absent optional check is non-applicable.

    ``bindings`` counts explicitly established application bindings, independently
    of the checker's operation-defined comparison count. Neither count is inferred
    from Python model shells or permissive legacy exit status.
    """

    name: str
    check: Callable[[], Report | ContractEvidence] | None
    required: bool = True
    minimum_checked: int = 1
    bindings: int | None = None
    minimum_bindings: int = 0
    required_bindings: tuple[BindingIdentity, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("A contract obligation needs a nonempty name")
        if self.check is not None and not callable(self.check):
            raise InputError("An obligation check must be callable or absent")
        if type(self.required) is not bool:
            raise InputError("required must be a boolean")
        if type(self.required_bindings) is not tuple or any(
            not isinstance(item, BindingIdentity) for item in self.required_bindings
        ):
            raise InputError("Required bindings must be immutable explicit identities")
        if len(set(self.required_bindings)) != len(self.required_bindings):
            raise InputError("Required binding identities must be unique")
        for count in (
            self.minimum_checked,
            self.minimum_bindings,
            0 if self.bindings is None else self.bindings,
        ):
            if type(count) is not int or count < 0:
                raise InputError(
                    "Contract coverage counts must be nonnegative integers"
                )


@dataclass(frozen=True)
class ContractResult:
    """Strict policy acceptance beside untouched raw reports and aggregate truth."""

    report: Report
    raw_reports: tuple[tuple[str, Report], ...]
    policy: AcceptancePolicy
    observed_bindings: tuple[tuple[str, tuple[BindingObservation, ...]], ...] = ()

    @property
    def accepted(self) -> bool:
        """Apply strict completion policy without relabeling raw verification."""
        return self.report.exit_code(strict=True, policy=self.policy) == 0

    def require_verified(self) -> Report:
        """Return verified evidence or raise with the full unfiltered aggregate."""
        if not self.report.verified:
            raise CheckError(self.report)
        return self.report


def _named_report(report: Report, name: str) -> Report:
    prefix = "/obligations/" + name.replace("~", "~0").replace("/", "~1")
    return replace(
        report,
        scope=f"{name}:{report.scope}",
        issues=tuple(
            replace(
                issue,
                location=prefix
                + "/"
                + issue.location.replace("~", "~0").replace("/", "~1"),
            )
            for issue in report.issues
        ),
        children=tuple(_named_report(child, name) for child in report.children),
    )


def _run_obligation(
    obligation: ContractObligation,
) -> tuple[Report, Report, tuple[BindingObservation, ...]]:
    if obligation.check is None:
        absent = Report(obligation.name, allow_empty=not obligation.required)
        return absent, absent, ()
    try:
        evidence = obligation.check()
        observations = (
            evidence.bindings if isinstance(evidence, ContractEvidence) else ()
        )
        report = evidence.report if isinstance(evidence, ContractEvidence) else evidence
        if not isinstance(report, Report):
            raise InputError("An obligation must return a Report")
    except (InputError, OSError, UnicodeError) as error:
        unavailable = Report(
            obligation.name,
            requested=1,
            issues=(
                Issue(
                    "contract.unavailable",
                    str(error),
                    obligation.name,
                    kind="exhausted"
                    if isinstance(error, LimitExceeded)
                    else "input_error",
                ),
            ),
        )
        return unavailable, unavailable, ()
    coverage = []
    completed = {item.identity for item in observations if item.completed}
    for identity in obligation.required_bindings:
        if identity not in completed:
            coverage.append(
                Issue(
                    "contract.binding_missing",
                    "Required application binding was not completely checked",
                    obligation.name,
                    kind="unverified",
                    expected=repr((identity.owner, identity.path)),
                )
            )
    for label, actual, minimum in (
        ("comparisons", report.checked, obligation.minimum_checked),
        ("bindings", obligation.bindings, obligation.minimum_bindings),
    ):
        if minimum and (actual is None or actual < minimum):
            coverage.append(
                Issue(
                    "contract.coverage_missing",
                    f"Required {label} coverage was not established",
                    obligation.name,
                    kind="unverified",
                    expected=str(minimum),
                    actual=str(actual),
                )
            )
    return (
        report,
        Report(
            obligation.name,
            report.checked,
            tuple(coverage),
            allow_empty=not obligation.required,
            requested=report.requested,
            complete=report.complete and not coverage,
            regime="required-contract",
            children=(
                _named_report(report, obligation.name),
                *(_named_report(item.report, obligation.name) for item in observations),
            ),
        ),
        observations,
    )


def check_contract(
    obligations: Sequence[ContractObligation],
    *,
    policy: AcceptancePolicy | None = None,
) -> ContractResult:
    """Evaluate every named obligation with explicit coverage and strict completion.

    Optional absence is allowed; an executed optional check retains its findings.
    Approvals require IssueSelection.for_issue on the named aggregate diagnostic.
    They can change policy acceptance, never require_verified or the raw reports.
    """
    selected = tuple(obligations)
    if any(not isinstance(item, ContractObligation) for item in selected):
        raise InputError("Expected typed contract obligations")
    if len({item.name for item in selected}) != len(selected):
        raise InputError("Contract obligation names must be unique")
    acceptance = replace(policy or AcceptancePolicy(), strict=True)
    if any(item.issue is None for item in acceptance.exceptions):
        raise InputError("Required contract exceptions must identify exact issues")
    reports = tuple((item.name, *_run_obligation(item)) for item in selected)
    return ContractResult(
        combine_reports("contract", *(report for _, _, report, _ in reports)),
        tuple((name, raw) for name, raw, _, _ in reports),
        acceptance,
        tuple((name, observed) for name, _, _, observed in reports),
    )
