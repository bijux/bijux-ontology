"""Explicit first-party Maven builds into caller-owned native bundle locations."""

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import bound_text
from bijux_ontology.execution.processes.transport import run_worker

from .files import capture_file


@dataclass(frozen=True)
class JavaBundle:
    """Explicit built classpath and the content pin accepted by its native probe."""

    directory: Path
    manifest_sha256: str
    log: Path


def _copy_jar(source: Path, target: Path, operation: Operation) -> str:
    """Copy closed build output while funding its finite capture and digest."""
    digest = hashlib.sha256()
    with (
        capture_file(source, operation) as (reader, remaining),
        target.open("xb") as writer,
    ):
        while remaining:
            amount = min(65536, remaining)
            operation.consume("bytes", amount)
            operation.consume("work")
            chunk = reader.read(amount)
            if len(chunk) != amount:
                raise ResourceError("Native build output changed during capture")
            writer.write(chunk)
            digest.update(chunk)
            remaining -= amount
    return digest.hexdigest()


def build_maven_bundle(
    source: Path,
    *,
    maven: Path,
    java: Path,
    build_directory: Path,
    dependency_directory: Path,
    output: Path,
    worker_jar: str,
    identity: Mapping[str, str | int],
    verify: Callable[[Path, str], None],
    operation: Operation,
) -> JavaBundle:
    """Build supplied first-party sources; never discover or install build tools.

    Maven dependency downloads and opaque compiler allocations are not metered
    Python work. The owned process has a wall-clock deadline, bounded diagnostics
    and a fixed Maven heap. Persistent build logs/dependencies/output stay under
    the three supplied roots. Failed output belongs exclusively to this call.
    """
    if os.name != "posix":
        raise ResourceError("Native Maven builds require qualified POSIX ownership")
    for path in (source, maven, java, build_directory, dependency_directory, output):
        if not isinstance(path, Path) or not path.is_absolute():
            raise InputError("Native builds require explicit absolute Paths")
        bound_text(str(path), operation)
        if any(character in str(path) for character in ('"', "\n", "\r", "\\")):
            raise InputError("Native build paths cannot contain JVM option delimiters")
    if (
        not maven.is_file()
        or not java.is_file()
        or not (java.parent / "javac").is_file()
    ):
        raise ResourceError("Supply an actual Maven executable and JDK java executable")
    if source.is_symlink() or not (source / "pom.xml").is_file():
        raise InputError("Supply the ordinary shipped Java source directory")
    if output.exists() or output.is_symlink():
        raise InputError("Native bundle output must not already exist")
    if any(
        name in os.environ
        for name in (
            "JAVA_TOOL_OPTIONS",
            "JDK_JAVA_OPTIONS",
            "_JAVA_OPTIONS",
            "MAVEN_OPTS",
            "MAVEN_ARGS",
        )
    ):
        raise ResourceError("Ambient JVM/Maven options are outside the build profile")
    for directory in (build_directory, dependency_directory):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise InputError("Native build and dependency directories must be ordinary")
        if directory.resolve().is_relative_to(output.resolve()):
            raise InputError("Bundle output cannot contain build or dependency roots")
    build_directory.mkdir(parents=True, exist_ok=True)
    dependency_directory.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="maven-", dir=build_directory) as temporary:
        job = Path(temporary)
        home, scratch = job / "home", job / "tmp"
        home.mkdir()
        scratch.mkdir()
        command = [
            "/usr/bin/env",
            f"JAVA_HOME={java.parent.parent}",
            "MAVEN_OPTS=-Xmx512m",
            (
                f'JAVA_TOOL_OPTIONS=-Duser.home="{home}" '
                f'-Djava.io.tmpdir="{scratch}" -XX:-UsePerfData'
            ),
            str(maven),
            "--batch-mode",
            "--no-transfer-progress",
            "--quiet",
            "--file",
            str(source / "pom.xml"),
            f"-Dmaven.repo.local={dependency_directory}",
            f"-Dbijux.output={job / 'classes'}",
            f"-DoutputDirectory={job / 'dependencies'}",
            "-DincludeScope=runtime",
            "package",
            "dependency:copy-dependencies",
        ]
        operation.consume("bytes", 4 * 65536)
        operation.consume("work", 65536)
        code, stopped, message = run_worker(
            command,
            timeout=operation.deadline - time.monotonic(),
            cap=65536,
            cancellation=operation.cancel,
        )
        raw_log = message.encode("utf-8")
        log = build_directory / (job.name + ".log")
        with log.open("xb") as stream:
            stream.write(raw_log)
        if stopped:
            raise LimitExceeded("Native Maven build stopped: " + stopped)
        if code != 0:
            raise ResourceError("Native Maven build failed; see " + str(log))
        operation.consume("work", 0)
        jars = sorted((job / "dependencies").glob("*.jar"))
        jars.append(job / "classes" / worker_jar)
        output.mkdir()
        try:
            entries = []
            for path in sorted(jars):
                operation.consume("resources")
                bound_text(path.name, operation)
                if path.is_symlink() or not path.is_file():
                    raise ResourceError("Native build omitted an ordinary required jar")
                digest = _copy_jar(path, output / path.name, operation)
                operation.consume("states", 2)
                operation.consume("bytes", 512)
                entries.append({"name": path.name, "sha256": digest})
            operation.consume("bytes", 4096 + 4096 * len(entries))
            operation.consume("work", 1024 + 1024 * len(entries))
            manifest = dict(identity, jars=entries)
            raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            operation.consume("bytes", len(raw))
            pin = hashlib.sha256(raw).hexdigest()
            with (output / "manifest.json").open("xb") as stream:
                stream.write(raw)
            verify(output, pin)
            operation.consume("work", 0)
        except BaseException:
            shutil.rmtree(output)
            raise
    return JavaBundle(output, pin, log)
