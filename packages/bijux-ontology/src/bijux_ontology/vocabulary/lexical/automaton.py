"""Optional bounded dictionary automata over completed exact lexical keys."""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event
from types import MappingProxyType

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.admission import _PageBudget

from .types import LexicalIndex


@dataclass(frozen=True, init=False, eq=False)
class MentionIndex:
    """Completed failure-transition dictionary sharing its exact lexical owner."""

    _index: LexicalIndex
    _transitions: tuple[Mapping[str, int], ...]
    _failures: tuple[int, ...]
    _terminal: tuple[str | None, ...]
    _suffix: tuple[int, ...]
    _max_length: int

    def __init__(self) -> None:
        """Require bounded preparation instead of unqualified manual assembly."""
        raise InputError("Mention indexes require prepare_mention_index")


@dataclass(frozen=True)
class MentionPreparation:
    """A reusable complete dictionary, or explicit interrupted preparation."""

    index: MentionIndex | None
    report: Report


def _prepare_mentions(index: LexicalIndex, budget: Budget) -> MentionIndex:
    """Build trie and failure/output links atomically without inherited output lists."""
    budget.consume("states")
    budget.consume("bytes", 256)
    transitions: list[dict[str, int]] = [{}]
    failures = [0]
    terminal: list[str | None] = [None]
    suffix = [0]
    longest = 0
    for key in index._keys:
        longest = max(longest, len(key))
        budget.consume("work")
        if not key:
            continue
        state = 0
        for character in key:
            budget.consume("work")
            following = transitions[state].get(character)
            if following is None:
                budget.consume("states", 2)
                budget.consume("bytes", 384)
                following = len(transitions)
                transitions[state][character] = following
                transitions.append({})
                failures.append(0)
                terminal.append(None)
                suffix.append(0)
            state = following
        terminal[state] = key
    budget.consume("work", len(transitions))
    pending = deque(transitions[0].values())
    while pending:
        parent = pending.popleft()
        budget.consume("work")
        for character, child in transitions[parent].items():
            budget.consume("work")
            state = failures[parent]
            while state and character not in transitions[state]:
                budget.consume("work")
                state = failures[state]
            budget.consume("work")
            target = transitions[state].get(character, 0)
            failures[child] = target
            suffix[child] = target if terminal[target] is not None else suffix[target]
            pending.append(child)
    budget.consume("work", len(transitions))
    frozen = tuple(MappingProxyType(value) for value in transitions)
    result = object.__new__(MentionIndex)
    object.__setattr__(result, "_index", index)
    object.__setattr__(result, "_transitions", frozen)
    object.__setattr__(result, "_failures", tuple(failures))
    object.__setattr__(result, "_terminal", tuple(terminal))
    object.__setattr__(result, "_suffix", tuple(suffix))
    object.__setattr__(result, "_max_length", longest)
    budget.consume("work", 0)
    return result


def prepare_mention_index(
    index: LexicalIndex,
    *,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> MentionPreparation:
    """Prepare reusable overlapping dictionary matching without typo expansion."""
    if not isinstance(index, LexicalIndex):
        raise InputError("Mention preparation requires a prepared LexicalIndex")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Mention preparation requires Budget")
    if cancel is not None and not isinstance(cancel, Event):
        raise InputError("Mention cancellation requires threading.Event")
    selected = budget if budget is not None else Budget(index.limits)
    operation = _PageBudget(selected, cancel)
    issues = IssueCollector(selected)
    result = None
    try:
        result = _prepare_mentions(index, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="terms.lexical.prepare_mentions")
    return MentionPreparation(
        result,
        Report(
            "terms.lexical.prepare_mentions",
            len(index._keys) if result is not None else 0,
            tuple(issues),
            allow_empty=True,
            requested=len(index._keys),
            complete=result is not None,
            regime="dictionary-failure-transitions",
        ),
    )
