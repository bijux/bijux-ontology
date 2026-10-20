"""Explicit Maven preparation of the shipped first-party OWLAPI worker."""

import argparse
import json
from pathlib import Path
from threading import Event

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.resources.maven import JavaBundle, build_maven_bundle

from ...profiles.execution import check_profile
from .bundles import LOADER_PROFILE, MAIN_CLASS, OWLAPI_VERSION, prepare_profile_backend


def build_profile_bundle(
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
    """Build supplied sources and require real positive/negative OWL2 EL checks."""
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
        """Admit the exact flat classpath before probing actual profile membership."""
        backend = prepare_profile_backend(
            java,
            bundle,
            manifest_sha256=pin,
            artifacts=build_directory / "probe",
            budget=operation,
        )
        prefix = """@prefix :<urn:bundle:> .
            @prefix owl:<http://www.w3.org/2002/07/owl#> .
            :ontology a owl:Ontology . :A a owl:Class . :B a owl:Class ."""
        for expected, body in (
            (True, ""),
            (False, ":A owl:equivalentClass [a owl:Class; owl:unionOf (:A :B)]."),
        ):
            source = prepare_rdf(
                RdfSource(
                    prefix + body, format="turtle", document_iri="urn:bundle:ontology"
                ),
                budget=operation,
            )
            result = check_profile(backend, source, profile="owl2-el", budget=operation)
            if (
                result.in_profile is not expected
                or not result.report.complete
                or (
                    result.report.outcome
                    != ("agreement" if expected else "disagreement")
                )
            ):
                raise ResourceError(
                    "Built OWLAPI bundle did not qualify the profile probe"
                )

    return build_maven_bundle(
        source,
        maven=maven,
        java=java,
        build_directory=build_directory,
        dependency_directory=dependency_directory,
        output=output,
        worker_jar="owl-profile-worker-0.1.0.jar",
        identity={
            "protocol": 1,
            "owlapi": OWLAPI_VERSION,
            "main_class": MAIN_CLASS,
            "loader_profile": LOADER_PROFILE,
        },
        verify=verify,
        operation=operation,
    )


def main() -> None:
    """Build only on explicit invocation and print its accepted content pin."""
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
    result = build_profile_bundle(**vars(args))
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
