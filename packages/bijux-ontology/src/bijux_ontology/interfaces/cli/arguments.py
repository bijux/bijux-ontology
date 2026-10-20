"""Installed command help follows the supported checking operations."""

import argparse
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from bijux_ontology.execution.limits import Limits


@dataclass(frozen=True)
class VocabularyInput:
    """One RDF selection, independent of parser state and presentation policy."""

    ontology: Path
    format: str | None
    base: str | None
    graph: str | None
    union: bool
    lookup: tuple[Path, ...]
    lookup_format: str | None
    sources: Path | None


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse one explicit check, without executing configuration or loading inputs."""
    parser = argparse.ArgumentParser(
        prog="bijux-ontology",
        description="Explicit RDF, JSON-LD and model checks; no general OWL reasoning.",
    )
    parser.add_argument(
        "--version", action="version", version=version("bijux-ontology")
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for name in (
        "vocabulary",
        "context",
        "models",
        "ontology",
        "project",
        "acquire",
        "inspect",
        "skos",
        "transform",
        "explain",
        "upgrade",
        "shex",
    ):
        action = actions.add_parser(
            name,
            help={
                "vocabulary": "Check explicit term inventory and selected coverage",
                "context": "Check actual JSON-LD usages selected by source paths",
                "models": "Check explicit application model declarations",
                "ontology": "Check selected ontology encoding rules",
                "project": "Run explicit checks from a local JSON configuration",
                "acquire": "Prepare explicitly selected HTTP resources for offline use",
                "inspect": "Inspect exact terms and multilingual search evidence",
                "skos": "Check SKOS integrity and explicit publication rules",
                "transform": "Write a checked RDF candidate without editing inputs",
                "explain": "Explain a registered diagnostic and corrective example",
                "upgrade": "Assess versions against supplied application contracts",
                "shex": "Validate explicit node-to-shape bindings with supplied ShExJ",
            }[name],
        )
        action.epilog = {
            "vocabulary": (
                "Example: bijux-ontology vocabulary terms.ttl inventory.json "
                '--json; inventory: ["https://example.org/Record"]'
            ),
            "context": (
                "Example: bijux-ontology context document.json expectations.json; "
                "expectation: "
                '[{"path":["title"],"predicate":"https://example.org/title"}]'
            ),
            "models": (
                "Example: bijux-ontology models terms.ttl models.json; model: "
                '[{"identifier":"record","types":["https://example.org/Record"],"fields":[]}]'
            ),
            "ontology": (
                "Example: bijux-ontology ontology terms.ttl --family references; "
                "structural assertions, never general OWL consistency."
            ),
            "project": (
                "Example: bijux-ontology project checks.json --explain. "
                'Minimal checks.json: {"version": 1, "rdf": {"terms": '
                '{"path": "ontology.ttl"}}, "checks": [{"name": "encoding", '
                '"kind": "ontology", "source": "terms"}]} '
                "Paths resolve against checks.json, not the current directory."
            ),
            "acquire": (
                "Example: bijux-ontology acquire project-directory "
                "--request ontology.ttl https://example.org/ontology "
                "--origin https://example.org --destination resources.zip; "
                "ordinary project checks remain offline."
            ),
            "inspect": (
                "Example: bijux-ontology inspect terms.ttl --search colour "
                "--language en-GB --json"
            ),
            "skos": (
                "Example: bijux-ontology skos terms.ttl --rule label-integrity "
                "--rule language-coverage --language en"
            ),
            "transform": (
                "Example: bijux-ontology transform terms.ttl --operation remap "
                "--mapping replacements.json --destination candidate.nt "
                "--to-format nt; mapping: {oldIRI: newIRI}"
            ),
            "explain": "Example: bijux-ontology explain vocabulary.term_missing --json",
            "upgrade": (
                "Example: bijux-ontology upgrade before.ttl after.ttl "
                "--declarations models.json --json; the verdict covers "
                "only the supplied application contracts."
            ),
            "shex": (
                "Example: bijux-ontology shex data.ttl schema.json "
                "--schema-iri urn:schema:example --binding urn:record:A urn:shape:S "
                "--json; requires bijux-ontology[shex]."
            ),
        }[name]
        if name == "explain":
            action.add_argument("code", help="Exact stable diagnostic code")
        elif name == "acquire":
            action.add_argument(
                "source", type=Path, help="Local project bundle skeleton"
            )
            action.add_argument(
                "--request",
                nargs=2,
                action="append",
                required=True,
                metavar=("MEMBER", "URL"),
            )
            action.add_argument(
                "--origin",
                action="append",
                required=True,
                help="Exact allowed HTTP(S) origin",
            )
            action.add_argument(
                "--destination",
                type=Path,
                required=True,
                help="New offline ZIP bundle path",
            )
            action.add_argument("--max-redirects", type=int, default=5)
            action.add_argument("--max-requests", type=int, default=100)
            action.add_argument("--max-compressed-bytes", type=int, default=5_000_000)
        elif name == "project":
            action.add_argument("configuration", type=Path)
            action.add_argument(
                "--execution",
                choices=("serial", "parallel"),
                default="serial",
                help="Choose execution without changing semantic job definitions",
            )
            action.add_argument("--workers", type=int, default=2)
            action.add_argument("--job-timeout", type=float, default=30.0)
            action.add_argument(
                "--execution-artifacts",
                type=Path,
                default=Path.cwd() / "artifacts" / "project-checks",
                help="Owned capture and worker scratch root for parallel execution",
            )
            action.add_argument(
                "--bundle",
                action="store_true",
                help="Read a portable directory or ZIP resource bundle offline",
            )
            action.add_argument(
                "--explain",
                action="store_true",
                help="Show resolved configuration without running checks",
            )
        elif name == "context":
            action.add_argument(
                "document", type=Path, help="JSON-LD input; '-' reads stdin"
            )
            action.add_argument(
                "expectations",
                type=Path,
                help="JSON array of selected usage expectations",
            )
            action.add_argument(
                "--allow-drop",
                action="append",
                default=[],
                help="Exact observed property name outside preservation scope",
            )
        else:
            action.add_argument(
                "ontology",
                type=Path,
                help="Local RDF source; '-' requires --format and --base",
            )
            if name in ("vocabulary", "models"):
                action.add_argument(
                    "inventory" if name == "vocabulary" else "declarations", type=Path
                )
            action.add_argument(
                "--format",
                help="Explicit RDF syntax, including extensionless snapshots",
            )
            action.add_argument(
                "--graph", help="Explicit named graph IRI or blank-node label"
            )
            action.add_argument(
                "--union",
                action="store_true",
                help="Intentionally check the dataset union",
            )
            action.add_argument("--lookup", action="append", type=Path, default=[])
            action.add_argument("--lookup-format")
        if name not in ("project", "explain", "acquire"):
            action.add_argument(
                "--sources", type=Path, help="Logical URL to local snapshot source map"
            )
            action.add_argument(
                "--base", help="Logical document base, separate from retrieval filename"
            )
        action.add_argument(
            "--strict",
            action="store_true",
            help="Reject unverified comparisons without changing their truth",
        )
        action.add_argument(
            "--minimum-severity",
            choices=("info", "warning", "error"),
            help="Policy threshold; diagnostics and verification stay unchanged",
        )
        action.add_argument(
            "--permit",
            nargs=2,
            action="append",
            default=[],
            metavar=("CODE", "LOCATION"),
            help="Permit one exact finding identity; never waive an input failure",
        )
        presentation = action.add_mutually_exclusive_group()
        presentation.add_argument(
            "--json", action="store_true", help="Print complete structured results"
        )
        presentation.add_argument(
            "--sarif",
            action="store_true",
            help="Export native results as local SARIF 2.1.0",
        )
        action.add_argument(
            "--artifact-root",
            type=Path,
            help="SARIF base for relative artifact references",
        )
        action.add_argument(
            "--output", type=Path, help="Write output without overwriting an input"
        )
        action.add_argument(
            "--display-limit",
            type=int,
            help="Limit human rows only; all findings still determine status",
        )
        action.add_argument(
            "--timeout",
            type=float,
            help="Interrupt the owned check worker after this many seconds",
        )
        for field in Limits.__dataclass_fields__:
            action.add_argument("--" + field.replace("_", "-"), type=int, default=None)
        if name == "vocabulary":
            action.add_argument("--namespace", action="append", default=[])
            action.add_argument("--exclude", action="append", default=[])
            action.add_argument("--coverage", action="store_true")
            action.add_argument("--coverage-term", action="append", default=[])
            action.add_argument("--coverage-kind", action="append", default=[])
            action.add_argument(
                "--coverage-scope", choices=("local", "closure"), default="local"
            )
            action.add_argument("--references", action="store_true")
            action.add_argument("--lint", action="store_true")
            action.add_argument("--allow-empty", action="store_true")
        if name in ("vocabulary", "ontology", "models"):
            action.add_argument(
                "--regime",
                choices=("rdf", "owl-dl", "application")
                if name == "models"
                else ("rdf", "owl-dl"),
                default="application" if name == "models" else "rdf",
            )
        if name == "inspect":
            selection = action.add_mutually_exclusive_group()
            selection.add_argument("--term", help="Exact absolute term IRI")
            selection.add_argument("--search", help="Text or identifier query")
            action.add_argument(
                "--scope", action="append", choices=("local", "imports", "lookup")
            )
            action.add_argument("--language", action="append", default=[])
        if name == "skos":
            action.add_argument("--rule", action="append")
            action.add_argument(
                "--profile", choices=("core-integrity", "mapping-integrity")
            )
            action.add_argument(
                "--hierarchy-basis",
                choices=("asserted", "effective"),
                default="effective",
            )
            action.add_argument("--concept", action="append", default=[])
            action.add_argument("--scheme", action="append", default=[])
            action.add_argument("--language", action="append", default=[])
            action.add_argument("--language-fallback", action="append", default=[])
            action.add_argument("--definition-predicate", action="append")
            action.add_argument("--closed-targets", action="store_true")
            action.add_argument("--notation-collection", action="append", default=[])
            action.add_argument("--replacement-predicate", action="append", default=[])
            action.add_argument(
                "--scope", action="append", choices=("local", "imports", "lookup")
            )
        if name == "shex":
            action.add_argument("schema", type=Path, help="Supplied local ShExJ schema")
            action.add_argument("--schema-iri", required=True)
            action.add_argument(
                "--binding",
                nargs=2,
                action="append",
                default=[],
                metavar=("FOCUS_IRI", "SHAPE_IRI"),
                help="Require this exact named node-to-shape obligation",
            )
            action.add_argument(
                "--schema-import",
                nargs=2,
                action="append",
                default=[],
                metavar=("DOCUMENT_IRI", "PATH"),
                help="Resolve an authored import from an explicit local file",
            )
        if name == "upgrade":
            action.add_argument("after", type=Path)
            action.add_argument("--inventory", type=Path)
            action.add_argument("--declarations", type=Path)
            action.add_argument("--after-format")
            action.add_argument("--after-base")
            action.add_argument("--after-sources", type=Path)
            action.add_argument("--namespace", action="append", default=[])
            action.add_argument(
                "--scope", action="append", choices=("local", "imports", "lookup")
            )
            action.add_argument("--replacement-predicate", action="append", default=[])
            action.add_argument("--context-before", type=Path)
            action.add_argument("--context-after", type=Path)
            action.add_argument("--expectations", type=Path)
            action.add_argument("--context-sources-before", type=Path)
            action.add_argument("--context-sources-after", type=Path)
            action.add_argument("--context-base-before")
            action.add_argument("--context-base-after")
        if name == "transform":
            action.add_argument(
                "--operation",
                required=True,
                choices=("extract", "remap", "merge", "convert"),
            )
            action.add_argument(
                "--destination",
                type=Path,
                required=True,
                help="New RDF artifact path; existing files are protected",
            )
            action.add_argument(
                "--to-format",
                required=True,
                choices=("turtle", "nt", "xml", "trig", "nquads", "json-ld"),
            )
            action.add_argument("--term", action="append", default=[])
            action.add_argument("--predicate", action="append", default=[])
            action.add_argument("--mapping", type=Path)
            action.add_argument(
                "--merge-source", action="append", type=Path, default=[]
            )
            action.add_argument("--without-blank-closure", action="store_true")
            action.add_argument(
                "--ignore-empty-graphs",
                action="store_true",
                help="Explicitly exclude empty graph membership",
            )
            action.add_argument(
                "--recheck-ontology",
                action="store_true",
                help="Run selected structural checks before and after",
            )
        if name == "ontology":
            action.add_argument(
                "--family",
                action="append",
                default=None,
                help=(
                    "references, declarations, headers, expressions, annotations, "
                    "reserved, metadata or deprecated"
                ),
            )
            action.add_argument("--include-lookup", action="store_true")
            action.add_argument("--local-only", action="store_true")
            action.add_argument("--require-annotation", action="append", default=[])
            action.add_argument("--language", action="append", default=[])
            action.add_argument("--used-term", action="append", default=[])
            action.add_argument("--replacement-predicate", action="append", default=[])
    options = parser.parse_args(argv)
    if options.artifact_root is not None and not options.sarif:
        parser.error("--artifact-root requires --sarif")
    if options.sarif and getattr(options, "explain", False):
        parser.error("--sarif cannot be combined with --explain")
    return options


def vocabulary_input(options: argparse.Namespace) -> VocabularyInput:
    """Freeze the RDF selection at the command parsing boundary."""
    return VocabularyInput(
        ontology=options.ontology,
        format=options.format,
        base=options.base,
        graph=options.graph,
        union=options.union,
        lookup=tuple(options.lookup),
        lookup_format=options.lookup_format,
        sources=options.sources,
    )
