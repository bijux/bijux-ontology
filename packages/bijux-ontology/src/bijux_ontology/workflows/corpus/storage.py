"""Bounded SQLite record-boundary transactions and strict inert report receipts."""

import hashlib
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bijux_ontology._json import decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Report, report_from_data
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.models.persistence.codecs import json_bytes

_SCHEMA = (
    "CREATE TABLE completed (identifier TEXT PRIMARY KEY, "
    "identity TEXT NOT NULL, report BLOB NOT NULL, sha256 TEXT NOT NULL)"
)


def eligible(report: Report) -> bool:
    """Only fully completed actual agreement or disagreement is reusable."""
    return bool(
        report.complete
        and report.requested
        and report.outcome in ("agreement", "disagreement")
    )


def encode_report(report: Report, budget: Budget) -> bytes:
    """Pay report-to-dict ownership before using the maintained inert codec."""
    pending = [(report, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Checkpoint report exceeds max_depth")
        budget.consume("work", 1 + len(current.issues) + len(current.children))
        budget.consume("states", 32 + 16 * len(current.issues))
        budget.consume("bytes", 2048 + 1024 * len(current.issues))
        texts = [current.scope, current.regime, *current.families, *current.exclusions]
        for issue in current.issues:
            texts.extend(
                text
                for text in (
                    issue.code,
                    issue.message,
                    issue.location,
                    issue.expected,
                    issue.actual,
                    issue.evidence_json,
                )
                if text is not None
            )
            if issue.details is not None:
                # Typed nested location/failure records are already bounded by
                # report admission; reserve their duplicate JSON ownership too.
                budget.consume("bytes", 4096)
                texts.extend(str(part) for part in issue.details.path)
                texts.extend(
                    text
                    for text in (
                        issue.details.source,
                        issue.details.term,
                        issue.details.graph,
                        issue.details.model,
                    )
                    if text is not None
                )
                if issue.details.failure is not None:
                    raise ResourceError(
                        "Parser-failure reports are outside completed checkpoint scope"
                    )
        for text in texts:
            bound_text(text, budget)
            budget.consume("bytes", 16 * len(text) + 128)
        budget.consume("states", len(current.children))
        budget.consume("bytes", 64 + 32 * len(current.children))
        pending.extend((child, depth + 1) for child in current.children)
    return json_bytes(report.to_dict(), budget)


class Checkpoint:
    """One finite local store; no executable objects or past global verdicts."""

    def __init__(self, path: Path, max_bytes: int, max_records: int) -> None:
        """Retain caller-selected storage bounds without opening the database."""
        if type(path) is not Path and not isinstance(path, Path):
            raise InputError("Corpus checkpoints require a pathlib Path")
        if type(max_bytes) is not int or not 16_384 <= max_bytes < 2**63:
            raise InputError("Checkpoint storage requires at least 16384 bytes")
        if type(max_records) is not int or not 0 < max_records < 2**63:
            raise InputError("Checkpoint record capacity must be positive")
        self.path, self.max_bytes, self.max_records = path, max_bytes, max_records

    def _files(self) -> None:
        """Reject oversized/nonregular database and native rollback-journal inputs."""
        for suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                raise ResourceError(
                    "WAL/SHM files are outside the DELETE checkpoint profile"
                )
        for path in (self.path, self.path.with_name(self.path.name + "-journal")):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_bytes:
                raise ResourceError("Checkpoint is not a bounded regular file")

    @contextmanager
    def open(self, budget: Budget) -> Iterator[sqlite3.Connection]:
        """Use a small native cache, no ambient temporary files and bounded waits."""
        bound_text(str(self.path), budget)
        budget.consume("resources")
        budget.consume("bytes", 131_072 + 32 * len(str(self.path)) + 1024)
        budget.consume("states", 16)
        self._files()
        try:
            connection = sqlite3.connect(self.path, timeout=0.01, isolation_level=None)
        except (sqlite3.Error, ValueError) as error:
            raise ResourceError("Corpus checkpoint cannot be opened") from error
        interrupted: list[LimitExceeded] = []

        def progress() -> int:
            """Checkpoint actual native SQL work without suppressing its stop reason."""
            try:
                budget.consume("work", 100)
            except LimitExceeded as error:
                interrupted.append(error)
                return 1
            return 0

        connection.set_progress_handler(progress, 100)
        try:
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA cache_size=-64")
            connection.execute("PRAGMA mmap_size=0")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            connection.execute(f"PRAGMA max_page_count={self.max_bytes // page_size}")
            budget.consume("work", 8)
            try:
                connection.execute("BEGIN IMMEDIATE")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version == 0:
                    existing = connection.execute(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%'"
                    ).fetchone()[0]
                    if existing:
                        raise ResourceError("Unknown checkpoint schema")
                    connection.execute(_SCHEMA)
                    connection.execute("PRAGMA user_version=1")
                elif version != 1:
                    raise ResourceError("Unsupported corpus checkpoint version")
                schema = connection.execute(
                    "SELECT sql=? FROM sqlite_master "
                    "WHERE name='completed' AND type='table'",
                    (_SCHEMA,),
                ).fetchone()
                if schema is None or schema[0] != 1:
                    raise ResourceError(
                        "Checkpoint schema differs from its finite format"
                    )
                foreign = connection.execute(
                    "SELECT count(*) FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' "
                    "AND (type!='table' OR name!='completed')"
                ).fetchone()[0]
                if foreign:
                    raise ResourceError("Foreign objects are outside checkpoint format")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
            rows = int(
                connection.execute("SELECT count(*) FROM completed").fetchone()[0]
            )
            if pages * page_size > self.max_bytes or rows > self.max_records:
                raise ResourceError("Checkpoint storage inventory exceeds its bounds")
            budget.consume("work", pages + rows)
            yield connection
        except sqlite3.Error as error:
            if interrupted:
                raise interrupted[0] from error
            raise ResourceError(
                "Corpus checkpoint transaction failed: " + str(error)
            ) from error
        finally:
            connection.close()

    def read(self, identifier: str, identity: str, budget: Budget) -> Report | None:
        """Qualify exact input identity before bounded report materialization."""
        bound_text(identifier, budget)
        budget.consume("bytes", 8 * len(identifier) + 512)
        with self.open(budget) as connection:
            budget.consume("work")
            header = connection.execute(
                "SELECT identity=?,length(report),length(sha256) "
                "FROM completed WHERE identifier=?",
                (identity, identifier),
            ).fetchone()
            if header is None or header[0] != 1:
                return None
            size = header[1]
            if type(size) is not int or size <= 0 or size > budget.limits.max_bytes:
                raise ResourceError("Checkpoint report is not a bounded complete blob")
            if header[2] != 64:
                raise ResourceError("Checkpoint integrity receipt is malformed")
            budget.consume("states", size)
            budget.consume("work", size)
            budget.consume("bytes", (128 + 4 * budget.limits.max_depth) * size)
            row = connection.execute(
                "SELECT report,sha256 FROM completed WHERE identifier=? AND identity=?",
                (identifier, identity),
            ).fetchone()
            if row is None or type(row[0]) is not bytes or len(row[0]) != size:
                raise ResourceError("Checkpoint changed during qualified loading")
            raw, digest = row
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ResourceError("Checkpoint report integrity does not match")
            try:
                report = report_from_data(
                    decode_json(
                        raw,
                        max_bytes=budget.limits.max_bytes,
                        max_depth=budget.limits.max_depth,
                    )
                )
                if encode_report(report, budget) != raw or not eligible(report):
                    raise InputError("Noncanonical or incomplete saved report")
            except LimitExceeded:
                raise
            except InputError as error:
                raise ResourceError("Checkpoint report cannot be qualified") from error
            return report

    def write(
        self, identifier: str, identity: str, report: Report, budget: Budget
    ) -> None:
        """Atomically replace exactly one qualified completed record boundary."""
        if not eligible(report):
            raise InputError(
                "Only complete supported record outcomes are checkpointable"
            )
        bound_text(identifier, budget)
        budget.consume("bytes", 8 * len(identifier) + 512)
        raw = encode_report(report, budget)
        if len(raw) > budget.limits.max_bytes:
            raise LimitExceeded("Checkpoint report exceeds max_bytes")
        budget.consume("bytes", len(raw) + 256)
        budget.consume("work", 1 + len(raw) // 4096)
        digest = hashlib.sha256(raw).hexdigest()
        with self.open(budget) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                present = connection.execute(
                    "SELECT 1 FROM completed WHERE identifier=?", (identifier,)
                ).fetchone()
                rows = int(
                    connection.execute("SELECT count(*) FROM completed").fetchone()[0]
                )
                if present is None and rows >= self.max_records:
                    raise ResourceError("Checkpoint record capacity is exhausted")
                connection.execute(
                    "INSERT INTO completed VALUES(?,?,?,?) ON CONFLICT(identifier) "
                    "DO UPDATE SET identity=excluded.identity,"
                    "report=excluded.report,sha256=excluded.sha256",
                    (identifier, identity, raw, digest),
                )
                budget.consume("work")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
