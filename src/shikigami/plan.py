"""Each app's pass: requests compared with what exists. Every requested app gets one on every sync (reconcile, D78:
its steps keep what exists, so a half-done or hand-broken app is repaired on the next pass). Pure - runs inside the
workflow, so it must stay deterministic (no I/O, no clock, sorted output)."""

from shikigami.models import AppAction, ExistingApp
from shikigami.request_format import App


def plan(apps: dict[str, App], existing: dict[str, ExistingApp]) -> list[AppAction]:
    actions: list[AppAction] = []
    for name in sorted(apps):
        spec = apps[name].spec
        current = existing.get(name)
        if spec.removed:
            kind, reason = "retire", "removed by its request (D83)"
        elif current is None or current.status == "retired":
            kind, reason = "register", "requested, not in Thoth"
        elif current.template != spec.template:
            kind, reason = "update", f"template {current.template!r} -> {spec.template!r}"
        else:
            kind, reason = "check", "matches Thoth, steps re-checked"
        actions.append(
            AppAction(
                app=name,
                kind=kind,
                reasons=[reason],
                template=spec.template,
                description=spec.description,
                owner=spec.owner,
            )
        )
    for name in sorted(set(existing) - set(apps)):
        if existing[name].status != "retired":
            actions.append(AppAction(app=name, kind="retire", reasons=["in Thoth, no request"]))
    return actions
