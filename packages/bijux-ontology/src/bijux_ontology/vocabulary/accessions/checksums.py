"""Maintained checksums on explicitly qualified exact ASCII local spellings."""

from importlib.metadata import PackageNotFoundError, version

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_text

from .types import Checksum


def valid_checksum(
    local: str, algorithm: Checksum, separators: str, budget: Budget
) -> bool | None:
    """Validate without compaction, Unicode digit coercion or identifier rewriting.

    Only the declared punctuation is omitted from the algorithm input. Grammar
    must already have succeeded. Missing/incompatible tooling is unsupported.
    """
    budget.consume("work", 4 * len(local) + len(separators) + 1)
    bound_text(local, budget)
    budget.consume("bytes", len(local))
    compact = local.translate(str.maketrans("", "", separators))
    if not compact:
        return False
    digits = compact[:-1] if algorithm == "iso7064-mod11-2" else compact
    if any(char < "0" or char > "9" for char in digits):
        return False
    if algorithm == "iso7064-mod11-2" and compact[-1] not in "0123456789X":
        return False
    if algorithm == "luhn":
        # The maintained public implementation owns a digit-reference tuple and
        # parity slices; reserve their bounded working storage before invoking it.
        budget.consume("states", len(compact))
        budget.consume("bytes", 64 + 16 * len(compact))
    try:
        release = tuple(int(part) for part in version("python-stdnum").split("."))
        if release < (2, 2) or release[0] != 2:
            return None
        if algorithm == "iso7064-mod11-2":
            from stdnum.iso7064 import mod_11_2

            return bool(mod_11_2.is_valid(compact))
        from stdnum import luhn

        return bool(luhn.is_valid(compact))
    except LimitExceeded:
        raise
    except (PackageNotFoundError, ImportError, ValueError, ArithmeticError):
        return None
