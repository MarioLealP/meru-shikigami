"""What would change: requests compared with what exists. Pure - runs inside the workflow, so it must stay
deterministic (no I/O, no clock, sorted output)."""

from shikigami.models import AppAction, ExistingApp
from shikigami.request_format import App


def plan(apps: dict[str, App], existing: dict[str, ExistingApp]) -> list[AppAction]:
    actions: list[AppAction] = []
    for name in sorted(apps):
        template = apps[name].spec.template
        current = existing.get(name)
        if current is None or current.status == "retired":
            actions.append(AppAction(app=name, kind="register", reasons=["requested, not in Thoth"]))
        elif current.template != template:
            reason = f"template {current.template!r} -> {template!r}"
            actions.append(AppAction(app=name, kind="update", reasons=[reason]))
    for name in sorted(set(existing) - set(apps)):
        if existing[name].status != "retired":
            actions.append(AppAction(app=name, kind="retire", reasons=["in Thoth, no request"]))
    return actions
