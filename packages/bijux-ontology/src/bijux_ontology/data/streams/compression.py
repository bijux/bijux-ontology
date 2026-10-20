"""Bounded single-member gzip input for the statement-local RDF reader."""

import hashlib
import zlib
from typing import BinaryIO

from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget


class GzipReader:
    """Decode through maintained zlib without retaining a complete expansion.

    Raw input, pending compressed data and each returned expansion are bounded
    independently. EOF is published only after the trailer and raw EOF prove a
    single complete member; concatenated members and trailing bytes are excluded.
    The outer RDF reader owns expanded byte/record limits and occurrence identity.
    """

    def __init__(self, stream: BinaryIO, budget: ControlBudget, maximum: int) -> None:
        self.stream, self.budget, self.maximum = stream, budget, maximum
        budget.consume("states", 8)
        budget.consume("bytes", 262144)
        self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.pending = b""
        self.count = 0
        self.digest = hashlib.sha256()
        self.validated = False

    def _input(self, amount: int) -> bytes:
        """Observe actual raw bytes under their own allowance and cumulative cost."""
        self.budget.consume("work")
        remaining = self.maximum - self.count
        amount = min(
            amount,
            65536,
            remaining if remaining else 1,
            self.budget.remaining("work"),
            self.budget.remaining("bytes"),
        )
        if amount <= 0:
            raise LimitExceeded("Compressed RDF exhausted its input allowance")
        raw = self.stream.read(amount)
        self.count += len(raw)
        if len(raw) > amount or self.count > self.maximum:
            raise LimitExceeded("Compressed RDF exceeds max_compressed_bytes")
        self.budget.consume("work", len(raw))
        self.budget.consume("bytes", len(raw))
        self.digest.update(raw)
        return raw

    def read(self, size: int = -1) -> bytes:
        """Return one bounded expansion, checking the trailer before claiming EOF."""
        self.budget.consume("work")
        if size == 0:
            return b""
        if self.validated:
            return b""
        amount = min(65536, size if size >= 0 else 65536)
        while True:
            self.budget.consume("work")
            if self.decoder.eof:
                if self.pending or self.decoder.unused_data or self._input(1):
                    raise ResourceError("RDF streams require exactly one gzip member")
                self.validated = True
                return b""
            compressed = self.pending or self._input(65536)
            if not compressed:
                raise ResourceError("Compressed RDF ended before its gzip trailer")
            available = min(
                amount,
                self.budget.remaining("work"),
                self.budget.remaining("bytes"),
            )
            if available <= 0:
                raise LimitExceeded("Compressed RDF exhausted its expansion allowance")
            try:
                expanded = self.decoder.decompress(compressed, available)
            except zlib.error as error:
                raise ResourceError(
                    "Compressed RDF has an invalid gzip member"
                ) from error
            self.pending = self.decoder.unconsumed_tail
            if expanded:
                return expanded
