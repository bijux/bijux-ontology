"""Explicit Maven preparation of the shipped first-party Jena worker."""

import argparse
import json
from pathlib import Path
from threading import Event

from rdflib import Graph, Literal, URIRef

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.resources.maven import JavaBundle, build_maven_bundle

from .runtime import JENA_VERSION, MAIN_CLASS, prepare_jena_backend
from .shacl.checking import validate_jena_shapes


def build_jena_bundle(
    source: Path,
    *,
    maven: Path,
    java: Path,
    build_directory: Path,
    dependency_directory: Path,
    output: Path,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 600,
    cancel: Event | None = None,
) -> JavaBundle:
    """Build into explicit local roots and require real positive/negative SHACL.

    Supply the Java directory shipped in the source distribution. Dependency
    downloads are explicit build work; imports and ordinary checking never build.
    The default cumulative byte allowance includes copied and re-sealed jars.
    """
    operation = Operation(
        limits
        or (
            Limits(max_total_bytes=512_000_000, max_resources=1024)
            if budget is None
            else None
        ),
        budget,
        timeout,
        cancel,
    )

    def verify(bundle: Path, pin: str) -> None:
        """Qualify this exact build through ordinary manifest and SHACL contracts."""
        backend = prepare_jena_backend(
            java,
            bundle,
            manifest_sha256=pin,
            artifacts=build_directory / "probe",
            budget=operation,
        )
        shapes = Graph().parse(
            data="""@prefix sh:<http://www.w3.org/ns/shacl#> .
            <urn:bundle:shape> a sh:NodeShape; sh:targetNode <urn:bundle:item>;
            sh:property [sh:path <urn:bundle:value>; sh:minCount 1].""",
            format="turtle",
        )
        for expected in (True, False):
            data = Graph()
            if expected:
                data.add(
                    (
                        URIRef("urn:bundle:item"),
                        URIRef("urn:bundle:value"),
                        Literal("ok"),
                    )
                )
            result = validate_jena_shapes(backend, data, shapes, budget=operation)
            if (
                result.conforms is not expected
                or not result.report.complete
                or (
                    result.report.outcome
                    != ("agreement" if expected else "disagreement")
                )
            ):
                raise ResourceError("Built Jena bundle did not qualify the SHACL probe")

    return build_maven_bundle(
        source,
        maven=maven,
        java=java,
        build_directory=build_directory,
        dependency_directory=dependency_directory,
        output=output,
        worker_jar="jena-worker-0.1.0.jar",
        identity={"protocol": 1, "jena": JENA_VERSION, "main_class": MAIN_CLASS},
        verify=verify,
        operation=operation,
    )


def main() -> None:
    """Build only when explicitly invoked, printing the resulting manifest pin."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "source",
        "maven",
        "java",
        "build-directory",
        "dependency-directory",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = build_jena_bundle(**vars(args))
    print(
        json.dumps(
            {
                "bundle": str(result.directory),
                "manifest_sha256": result.manifest_sha256,
                "log": str(result.log),
            }
        )
    )


if __name__ == "__main__":
    main()
