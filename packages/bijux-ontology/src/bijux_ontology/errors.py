"""Expected failures at owned input and execution boundaries."""

from dataclasses import dataclass, replace
from json import JSONDecodeError
from xml.parsers.expat import ExpatError


class InputError(ValueError):
    """A supplied value cannot establish the requested input contract."""

    def __init__(
        self, message: str, *, location: str = "", failure: "InputFailure | None" = None
    ) -> None:
        self.location = location
        self.failure = failure
        super().__init__(f"{location}: {message}" if location else message)


class ResourceError(InputError):
    """An explicitly required resource could not be supplied or decoded."""


class LimitExceeded(InputError):
    """Owned execution stopped at an explicit resource or work bound."""


@dataclass(frozen=True)
class SourceSpan:
    """One-based coordinates supplied by a parser; unavailable endpoints stay absent."""

    start_line: int | None = None
    start_column: int | None = None
    end_line: int | None = None
    end_column: int | None = None

    def __post_init__(self) -> None:
        """Reject invented zero/negative coordinates at the typed evidence boundary."""
        for value in (
            self.start_line,
            self.start_column,
            self.end_line,
            self.end_column,
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise InputError("Parser coordinates require positive integers")

    def to_dict(self) -> dict[str, int | None]:
        """Expose only coordinates actually supplied by the maintained parser."""
        return {
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }


@dataclass(frozen=True)
class InputFailure:
    """Failing content identity, separate from a configuration or check location."""

    document_iri: str | None
    location: str = ""
    role: str = "input"
    import_chain: tuple[str, ...] = ()
    span: SourceSpan | None = None

    def __post_init__(self) -> None:
        """Own a finite immutable chain without converting arbitrary iterators."""
        if (
            self.document_iri is not None
            and not isinstance(self.document_iri, str)
            or not isinstance(self.location, str)
            or not isinstance(self.role, str)
            or not self.role
            or type(self.import_chain) is not tuple
            or any(not isinstance(value, str) for value in self.import_chain)
            or self.span is not None
            and not isinstance(self.span, SourceSpan)
        ):
            raise InputError("Input failure requires typed source and parser evidence")

    def to_dict(self) -> dict[str, object]:
        """Return portable input evidence without inventing content coordinates."""
        return {
            "document_iri": self.document_iri,
            "location": self.location,
            "role": self.role,
            "import_chain": list(self.import_chain),
            "span": self.span.to_dict() if self.span is not None else None,
        }


def attach_input_failure(
    error: InputError,
    *,
    document_iri: str | None,
    location: str = "",
    role: str = "input",
    import_chain: tuple[str, ...] = (),
) -> InputError:
    """Enrich an existing typed failure without changing its kind or original cause."""
    if error.failure is not None:
        if error.failure.document_iri == document_iri:
            error.failure = replace(
                error.failure,
                location=error.failure.location or location,
                role=role if error.failure.role == "input" else error.failure.role,
            )
        if import_chain:
            error.failure = replace(error.failure, import_chain=import_chain)
        return error
    current: BaseException | None = error
    seen: set[int] = set()
    span = None
    while current is not None and id(current) not in seen and len(seen) < 128:
        seen.add(id(current))
        if isinstance(current, InputError) and current.failure is not None:
            error.failure = current.failure
            if import_chain:
                error.failure = replace(error.failure, import_chain=import_chain)
            if not error.location:
                error.location = error.failure.document_iri or error.failure.location
            return error
        if isinstance(current, SyntaxError):
            values = (
                current.lineno,
                current.offset,
                current.end_lineno,
                current.end_offset,
            )
            qualified = tuple(
                value if value is not None and value > 0 else None for value in values
            )
            if any(value is not None for value in qualified):
                span = SourceSpan(*qualified)
                break
        elif isinstance(current, JSONDecodeError):
            span = SourceSpan(current.lineno, current.colno)
            break
        elif isinstance(current, ExpatError):
            line = getattr(current, "lineno", None)
            offset = getattr(current, "offset", None)
            if type(line) is int and line > 0 and type(offset) is int and offset >= 0:
                span = SourceSpan(line, offset + 1)
                break
        cause = current.__cause__ or getattr(current, "cause", None)
        current = cause if isinstance(cause, BaseException) else None
    error.failure = InputFailure(document_iri, location, role, import_chain, span)
    if not error.location:
        error.location = document_iri or location
    return error
