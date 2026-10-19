"""Fund one terminal decoder without renewing worker or semantic-work allowance."""

import json
from threading import Event

import pytest

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, Limits
from bijux_ontology.execution.receipts import TerminalResponse


@pytest.mark.parametrize("completion", ("complete", "malformed", "missing"))
def test_disjoint_terminal_costs_pay_only_known_host_work(completion: str) -> None:
    limits = Limits(
        max_total_bytes=65536,
        max_work=4096,
        max_states=512,
        max_resources=8,
        max_triples=8,
        max_issues=1,
    )
    owner = Budget(limits)
    cancellation = Event()
    operation = Operation(limits, owner, 30, cancellation)
    terminal = TerminalResponse(operation, 4096)
    grants = {kind: terminal.available(kind) for kind in BUDGET_KINDS}
    # A terminal decoder creates no Issue and cannot steal a native finding slot.
    assert grants["issues"] == 1
    native = DelegatedAllowance(operation, grants)
    payload = b'{"request":true}'
    terminal.admit(payload)
    cancellation.set()
    if completion == "complete":
        raw = json.dumps({"consumed": grants}, separators=(",", ":")).encode()
        response = terminal.decode(raw)
        assert isinstance(response, dict)
        native.settle(response["consumed"])
        with pytest.raises(InputError):
            terminal.decode(raw)
    elif completion == "malformed":
        with pytest.raises(InputError):
            terminal.decode(b'{"broken":')
    terminal.close()
    if completion != "complete":
        native.forfeit()
    observed = dict(owner._counts)
    terminal.close()
    if completion != "complete":
        native.forfeit()
    assert owner._counts == observed
    assert all(owner.remaining(kind) >= 0 for kind in BUDGET_KINDS)
    assert owner.remaining("bytes") > 0
    assert owner.remaining("work") > 0
    if completion == "missing":
        assert observed["bytes"] == grants["bytes"] + 4 * len(payload) + 512
        assert observed["work"] == grants["work"] + len(payload) + 1
        assert observed["states"] == grants["states"]
    elif completion == "malformed":
        # Decoder admission/work happened, but unused maximum capacity was not paid.
        assert grants["bytes"] < observed["bytes"] < limits.max_total_bytes
        assert grants["work"] < observed["work"] < limits.max_work


def test_terminal_request_cannot_exceed_its_observed_funded_cap() -> None:
    owner = Budget(Limits(max_total_bytes=65536, max_work=4096, max_states=512))
    terminal = TerminalResponse(Operation(None, owner, 30, None), 4096)
    with pytest.raises(LimitExceeded):
        terminal.admit(b"x" * (terminal.cap + 1))
    terminal.close()
    assert not any(owner._counts.values())
