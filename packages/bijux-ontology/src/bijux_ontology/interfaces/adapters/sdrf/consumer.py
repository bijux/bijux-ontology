"""Version-qualified local providers preserve the maintained SDRF validators."""

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from bijux_ontology.errors import ResourceError
from bijux_ontology.limits import Budget

from .terminology import SdrfTerminology, _fold

SUPPORTED_VERSION = "0.1.7.dev72"
INSTALL_REQUIREMENT = "sdrf-pipelines[ontology] @ git+https://github.com/bigbio/sdrf-pipelines.git@4ba20f5778b0b52055b71bad0a7654cc60dacbe6"


@dataclass(frozen=True)
class _Runtime:
    """The exact optional consumer classes used at this concrete boundary."""

    schema: Any
    dataframe: Any
    frame: Any
    registry: Any
    validator: Any
    ontology: Any
    client: Any
    models: tuple[type, ...]
    get_validator: Any


def _runtime() -> _Runtime:
    """Import the normally installed qualified consumer and its real ontology extra."""
    try:
        installed = version("sdrf-pipelines")
        ols = import_module("sdrf_pipelines.ols.ols")
        models = import_module("sdrf_pipelines.sdrf.schemas.models")
        SchemaRegistry = import_module(
            "sdrf_pipelines.sdrf.schemas.registry"
        ).SchemaRegistry
        SchemaValidator = import_module(
            "sdrf_pipelines.sdrf.schemas.validator"
        ).SchemaValidator
        SDRFDataFrame = import_module("sdrf_pipelines.sdrf.sdrf").SDRFDataFrame
        get_validator = import_module("sdrf_pipelines.sdrf.validators").get_validator
        OLS_AVAILABLE, OlsClient = ols.OLS_AVAILABLE, ols.OlsClient
        SchemaDefinition, ColumnDefinition, ValidatorConfig = (
            models.SchemaDefinition,
            models.ColumnDefinition,
            models.ValidatorConfig,
        )
    except (ImportError, PackageNotFoundError) as error:
        raise ResourceError("Install separately: " + INSTALL_REQUIREMENT) from error
    ontology = get_validator("ontology")
    if installed != SUPPORTED_VERSION or not OLS_AVAILABLE or ontology is None:
        raise ResourceError(
            "SDRF requires qualified sdrf-pipelines 0.1.7.dev72[ontology]"
        )
    if ontology.__module__ != "sdrf_pipelines.sdrf.validators" or not hasattr(
        SchemaValidator, "_create_validator_instance"
    ):
        raise ResourceError(
            "The installed SDRF validator/provider boundary is incompatible"
        )
    return _Runtime(
        SchemaDefinition,
        SDRFDataFrame,
        import_module("pandas").DataFrame,
        SchemaRegistry,
        SchemaValidator,
        ontology,
        OlsClient,
        (SchemaDefinition, ColumnDefinition, ValidatorConfig),
        get_validator,
    )


def local_sdrf_client(
    terminology: SdrfTerminology, *, budget: Budget | None = None
) -> Any:
    """Return an actual OlsClient accepted by the unchanged OntologyValidator.

    Only exact local lookup methods are supported. No request session or global
    ontology cache is used. Callers keep ordinary consumer diagnostic policy;
    ``check_sdrf`` additionally requires local semantic completion.
    """
    runtime = _runtime()
    operation = budget if budget is not None else Budget()
    if not isinstance(terminology, SdrfTerminology):
        raise ResourceError("A local SDRF client requires prepared terminology")

    base_client: Any = runtime.client

    class LocalClient(base_client):  # type: ignore[misc]
        """Concrete maintained client subtype with finite local lookup overrides."""

        def __init__(self) -> None:
            """Avoid the consumer's default cache/download initialization."""
            super().__init__(use_cache=False)
            self.session.close()

        def search(
            self,
            term: str,
            ontology: str | None = None,
            exact: bool = True,
            use_ols_cache_only: bool = False,
            **kwargs: object,
        ) -> list[dict[str, str]]:
            """Return only exact selected labels/synonyms and unique accessions."""
            if not exact or kwargs:
                raise ResourceError(
                    "The local SDRF provider supports exact terminology search only"
                )
            result = []
            query = _fold(term, operation)
            for candidate in terminology.search(term, ontology, operation):
                for label in candidate.labels:
                    operation.consume("work")
                    if _fold(label, operation) == query:
                        operation.consume("states")
                        operation.consume(
                            "bytes",
                            256
                            + 4
                            * (
                                len(label)
                                + len(candidate.accession)
                                + len(candidate.iri)
                            ),
                        )
                        result.append(
                            {
                                "label": label,
                                "obo_id": candidate.accession,
                                "iri": str(candidate.iri),
                            }
                        )
                        break
            return result

        def labels_for_accession(
            self, accession: str, use_ols_cache_only: bool = False
        ) -> set[str] | None:
            """An unresolved or case-colliding accession cannot supply agreement."""
            term = terminology.resolve(accession, operation)
            if term is None:
                return None
            operation.consume("states", len(term.labels))
            return {_fold(label, operation) for label in term.labels}

        def is_under_parent(
            self,
            accession: str,
            parent_accession: str,
            use_ols_cache_only: bool = False,
        ) -> bool | None:
            """Qualify exact identities before the consumer's equal-parent shortcut."""
            return terminology.under_parent(accession, parent_accession, operation)

    return LocalClient()


def _parameters_supported(params: dict[str, Any]) -> bool:
    """Reject invalid known parameter shapes before consumer attribute assignment."""
    if "ontologies" in params and (
        type(params["ontologies"]) not in (list, tuple)
        or any(type(item) is not str for item in params["ontologies"])
    ):
        return False
    for name in ("parent_accession", "description"):
        if (
            name in params
            and params[name] is not None
            and type(params[name]) is not str
        ):
            return False
    for name in (
        "use_ols_cache_only",
        "recommend_nt_ac",
        "allow_not_applicable",
        "allow_not_available",
    ):
        if name in params and type(params[name]) is not bool:
            return False
    if "error_level" in params and params["error_level"] not in (
        "error",
        "warning",
        "info",
    ):
        return False
    examples = params.get("examples", [])
    return (
        type(examples) is str
        or type(examples) in (list, tuple)
        and all(type(item) is str for item in examples)
    )


def _schema_validator(runtime: _Runtime, registry: Any, client: Any) -> Any:
    """Inject a concrete client only; all original validation functions still run."""

    base_validator: Any = runtime.validator

    class LocalValidator(base_validator):  # type: ignore[misc]
        """The pinned constructor hook does not replace schema/column validation."""

        def _create_validator_instance(
            self, validator_config: Any, skip_ontology: bool = False
        ) -> Any:
            """Keep normal ontology validation and ordinary registered built-ins."""
            if validator_config.validator_name == "ontology":
                if not _parameters_supported(validator_config.params):
                    return None
                return runtime.ontology(params=validator_config.params, client=client)
            implementation = runtime.get_validator(validator_config.validator_name)
            if (
                implementation is not None
                and implementation.__module__ == "sdrf_pipelines.sdrf.validators"
            ):
                return super()._create_validator_instance(
                    validator_config, skip_ontology=skip_ontology
                )
            return None

    return LocalValidator(registry)
