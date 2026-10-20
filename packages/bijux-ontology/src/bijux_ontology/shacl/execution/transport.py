"""Bounded, cancellable transport to the isolated SHACL process."""

import sys
import threading

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.execution.processes.sessions import FramedSession, WorkerGeneration
from bijux_ontology.execution.workers import execute_worker


def execute_backend(payload: bytes, *, cap: int, timeout: float | None) -> bytes:
    """Require delivered input and successful owned SHACL worker completion."""
    return execute_worker(
        "bijux_ontology.shacl.execution.worker", payload, cap=cap, timeout=timeout
    )


class SessionBackend(FramedSession):
    """One SHACL-only fixed-plan worker with its exact preparation handshake."""

    def __init__(self, preparation: bytes, *, cap: int) -> None:
        self._preparation = preparation
        self._prepared_generation: WorkerGeneration | None = None
        super().__init__(
            (
                sys.executable,
                "-I",
                "-B",
                "-m",
                "bijux_ontology.shacl.execution.worker",
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

        generation = self.ensure_generation(deadline, cancel)
        try:
            if generation is not self._prepared_generation:
                prepared = self.exchange(
                    generation, self._preparation, deadline, cancel, budget=budget
                )
                if budget is not None:
                    budget.consume("work", len(prepared) + 1)
                    budget.consume("states", 2 * len(prepared) + 8)
                    budget.consume("bytes", 64 * len(prepared) + 512)
                response = decode_json(prepared, max_bytes=self.cap)
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
                    self.reset(generation=generation)
                    return prepared
                else:
                    raise ResourceError("Expected SHACL preparation acknowledgement")
            self._prepared_generation = generation
            return self.exchange(generation, payload, deadline, cancel, budget=budget)
        except BaseException as error:
            self.reset(generation=generation, failure=error)
            raise
