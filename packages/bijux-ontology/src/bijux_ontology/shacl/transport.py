"""Bounded, cancellable transport to the isolated SHACL process."""

import sys
import threading

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.execution.processes.sessions import FramedSession
from bijux_ontology.execution.workers import execute_worker


def execute_backend(payload: bytes, *, cap: int, timeout: float | None) -> bytes:
    """Require delivered input and successful owned SHACL worker completion."""
    return execute_worker(
        "bijux_ontology.shacl.worker", payload, cap=cap, timeout=timeout
    )


class SessionBackend(FramedSession):
    """One SHACL-only fixed-plan worker with its exact preparation handshake."""

    def __init__(self, preparation: bytes, *, cap: int) -> None:
        self._preparation = preparation
        super().__init__(
            (
                sys.executable,
                "-I",
                "-B",
                "-m",
                "bijux_ontology.shacl.worker",
                str(cap),
                "--session",
            ),
            cap=cap,
            label="SHACL",
        )

    def execute(
        self,
        payload: bytes,
        *,
        deadline: float | None,
        cancel: threading.Event | None,
        budget: Budget | None = None,
    ) -> bytes:
        """Replay a fixed plan on fresh starts and exchange one independent job."""
        from bijux_ontology.json.values import decode_json

        self._interrupted(deadline, cancel)
        try:
            if self._process is None or self._process.poll() is not None:
                self.reset()
                self._start()
                prepared = self._exchange(
                    self._preparation, deadline, cancel, budget=budget
                )
                if budget is not None:
                    budget.consume("work", len(prepared) + 1)
                    budget.consume("states", 2 * len(prepared) + 8)
                    budget.consume("bytes", 64 * len(prepared) + 512)
                response = decode_json(prepared, max_bytes=self._cap)
                if not isinstance(response, dict):
                    raise ResourceError("Invalid SHACL session preparation response")
                status = response.get("status")
                if status == "ready":
                    if set(response) != {"status"}:
                        raise ResourceError(
                            "Invalid SHACL session preparation envelope"
                        )
                elif isinstance(status, str) and status in {
                    "exception",
                    "exhausted",
                    "unavailable",
                    "failure",
                    "input_error",
                    "shapes_invalid",
                }:
                    self.reset()
                    return prepared
                else:
                    raise ResourceError("Expected SHACL preparation acknowledgement")
            return self._exchange(payload, deadline, cancel, budget=budget)
        except BaseException as error:
            self.reset(failure=error)
            raise
