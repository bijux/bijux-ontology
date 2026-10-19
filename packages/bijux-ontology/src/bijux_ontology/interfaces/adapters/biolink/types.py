"""Local Biolink resource identities and selected association interpretations."""

from dataclasses import dataclass

from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class BiolinkResource:
    """Explicit immutable YAML resource; names bind exact schema import tokens."""

    name: str
    version: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        """Require supplied bytes and declared identity, with no implicit default."""
        if (
            type(self.name) is not str
            or not self.name
            or type(self.version) is not str
            or not self.version
            or type(self.content) is not bytes
            or type(self.sha256) is not str
        ):
            raise InputError(
                "Biolink resources require name, version, bytes and SHA256"
            )


@dataclass(frozen=True)
class BiolinkPolicy:
    """Submitted abstract/mixin terms are explicit; native mixins stay inherited.

    The selected profile checks category/predicate/qualifier semantics, not other
    association payload slots or full LinkML instance conformance.
    """

    allow_abstract: bool = False
    allow_mixins: bool = False

    def __post_init__(self) -> None:
        """Require actual boolean policies for every submitted model element."""
        if type(self.allow_abstract) is not bool or type(self.allow_mixins) is not bool:
            raise InputError("Biolink abstract/mixin policies require booleans")


@dataclass(frozen=True)
class BiolinkQualifier:
    """One original qualifier occurrence; equal types are never silently merged."""

    type_id: str
    value: str


@dataclass(frozen=True)
class BiolinkAssociation:
    """One oriented category combination and all its ordered qualifier occurrences.

    Subject/object identifiers are retained source metadata; category compatibility
    does not prove that an external identifier exists or has a biological type.
    """

    association: str
    subject_category: str
    predicate: str
    object_category: str
    qualifiers: tuple[BiolinkQualifier, ...] = ()
    subject: str | None = None
    object: str | None = None
    location: str = ""


@dataclass(frozen=True)
class BiolinkIdentity:
    """Actual native runtime and exact caller-supplied dialect resources."""

    model_version: str
    model_sha256: str
    predicate_sha256: str
    imports: tuple[tuple[str, str], ...]
    toolkit_version: str
    linkml_version: str
    python_version: str


@dataclass(frozen=True, init=False)
class BiolinkModel:
    """Checked local resource capsule, without mutable toolkit caches or defaults.

    A native toolkit is constructed once per checked batch inside the owned
    process. This capsule owns bytes, not a persistent native toolkit session.
    """

    model: BiolinkResource | None
    predicate_map: BiolinkResource | None
    imports: tuple[BiolinkResource, ...]
    identity: BiolinkIdentity | None
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Require preparation through the actual maintained toolkit."""
        raise InputError("Use prepare_biolink with explicit local resources")

    @classmethod
    def _owned(
        cls,
        model: BiolinkResource | None,
        predicate_map: BiolinkResource | None,
        imports: tuple[BiolinkResource, ...],
        identity: BiolinkIdentity | None,
        report: Report,
    ) -> "BiolinkModel":
        """Adopt checked immutable bytes and native preparation evidence."""
        result = object.__new__(cls)
        for key, value in (
            ("model", model),
            ("predicate_map", predicate_map),
            ("imports", imports),
            ("identity", identity),
            ("report", report),
        ):
            object.__setattr__(result, key, value)
        return result


@dataclass(frozen=True)
class BiolinkInterpretation:
    """Effective native context with orientation and source qualifier order intact."""

    association: str
    subject_category: str
    predicate: str
    object_category: str
    qualified_predicate: str
    qualifiers: tuple[tuple[str, str], ...]
    constraints_json: str
    report: Report


@dataclass(frozen=True)
class BiolinkCheck:
    """Original selected combinations alongside their raw dialect-contract reports."""

    associations: tuple[BiolinkAssociation, ...]
    interpretations: tuple[BiolinkInterpretation, ...]
    identity: BiolinkIdentity | None
    report: Report
