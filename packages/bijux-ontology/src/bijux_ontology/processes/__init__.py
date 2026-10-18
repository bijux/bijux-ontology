"""Owned subprocess creation and deterministic descendant cleanup."""

from .ownership import spawn_owned_process, stop_owned_process

__all__ = ["spawn_owned_process", "stop_owned_process"]
