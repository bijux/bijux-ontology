"""Compatibility access to shared owned-process supervision."""

from bijux_ontology.processes.transport import run_worker, stop_process

__all__ = ["run_worker", "stop_process"]
