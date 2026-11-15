"""What the worker registers: every activity on an instance, found rather than listed (a hand list once missed one)."""

from collections.abc import Callable


def defined(group: object) -> list[Callable]:
    """Every @activity.defn method of the instance, bound, in name order."""
    names = sorted(n for n in dir(type(group)) if hasattr(getattr(type(group), n), "__temporal_activity_definition"))
    return [getattr(group, n) for n in names]
